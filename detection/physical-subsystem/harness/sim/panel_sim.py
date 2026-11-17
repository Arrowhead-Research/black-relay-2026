"""Qolsys IQ Panel 5 simulator: IQ Remote (MQTT/TLS via real mosquitto) + Control4 (TLS JSON) with one shared truth,
a scenario API and a full inbound audit log.  See PANEL_PROTOCOL.md."""
from __future__ import annotations
import asyncio, collections, json, os, ssl, tempfile, threading, time
from dataclasses import dataclass, field
from pathlib import Path

import paho.mqtt.client as mqtt

from lib import certs as C
from lib.mosq import MosqProc, free_port, wait_port
from sim import protocol as P


# ----------------------------------------------------------------------------------------------- audit
class Audit:
    def __init__(self, path: Path | None = None):
        self.lock = threading.Lock(); self.entries: list[dict] = []; self.path = path

    def add(self, side, kind, verdict, reason, raw):
        e = {"t": time.time(), "side": side, "kind": kind, "verdict": verdict, "reason": reason,
             "raw": raw if isinstance(raw, str) else raw.decode("utf-8", "replace")[:2000]}
        with self.lock:
            self.entries.append(e)
            if self.path:
                with open(self.path, "a") as f: f.write(json.dumps(e) + "\n")
        return e

    def violations(self):
        with self.lock: return [e for e in self.entries if e["verdict"] == "violation"]

    def count(self, side=None, reason=None, verdict=None):
        with self.lock:
            return sum(1 for e in self.entries if (side is None or e["side"] == side)
                       and (reason is None or reason in e["reason"]) and (verdict is None or e["verdict"] == verdict))

    def since(self, idx): 
        with self.lock: return list(self.entries[idx:])

    def __len__(self):
        with self.lock: return len(self.entries)


# ----------------------------------------------------------------------------------------------- truth
@dataclass
class Zone:
    zone_id: int
    name: str
    sensortype: str = "Door_Window"
    status: str = "Closed"
    partition_id: int = 0
    battery: str = "Normal"
    group: str = "entryexitdelay"
    time_ms: str = field(default_factory=P.now_ms)

    @property
    def short_id(self): return str(7000000 + self.zone_id)

    def row(self):
        return {"_id": str(self.zone_id), "zoneid": str(self.zone_id), "sensorname": self.name,
                "sensortype": self.sensortype, "sensorgroup": self.group, "sensorstatus": self.status,
                "sensorstate": "0", "partition_id": str(self.partition_id), "battery_status": self.battery,
                "time": self.time_ms, "current_capability": "POWERG", "shortID": self.short_id,
                "latestdBm": "-60", "averagedBm": "-62", "zone_type": "1", "zone_physical_type": "1",
                "zone_alarm_type": "1", "zone_supervised": "1", "signal_source": "POWERG", "extras": "{}",
                "updated_date": self.time_ms, "created_date": self.time_ms}


@dataclass
class Partition:
    pid: int
    name: str = "Main"
    status: str = "DISARM"      # IQ SYSTEM_STATUS value
    alarm_state: str = "None"   # None|Delay|Alarm
    alarm_types: list = field(default_factory=list)


class PanelState:
    def __init__(self):
        self.lock = threading.RLock()
        self.zones: dict[int, Zone] = {}
        self.partitions: dict[int, Partition] = {0: Partition(0)}

    @classmethod
    def default(cls):
        s = cls()
        for z in (Zone(12, "Front door", "Door_Window", "Closed"),
                  Zone(13, "Motion 13", "Motion", "Idle", group="awayinstantmotion"),
                  Zone(14, "Living glass", "GlassBreak", "Idle", group="glassbreak"),
                  Zone(15, "Hall PIR", "Panel Motion", "Idle", group="awayinstantmotion"),
                  Zone(16, "Kitchen smoke", "SmokeDetector", "Closed", group="smoke_heat"),
                  Zone(40, "Ignored thing", "Door_Window", "Closed")):
            s.zones[z.zone_id] = z
        return s

    def clone_zones(self):
        with self.lock: return {k: Zone(**vars(v)) for k, v in self.zones.items()}

    # full-DB snapshot as IQ `syncdatabase` returns it
    def fulldb(self):
        with self.lock:
            sens = [z.row() for z in sorted(self.zones.values(), key=lambda z: z.zone_id)]
            parts, sets, states, alarmed, pg = [], [], [], [], []
            for p in self.partitions.values():
                parts.append({"_id": str(p.pid), "partition_id": str(p.pid), "name": p.name, "devices": ""})
                sets += [{"_id": f"s{p.pid}a", "partition_id": str(p.pid), "name": "SYSTEM_STATUS", "value": p.status},
                         {"_id": f"s{p.pid}b", "partition_id": str(p.pid), "name": "SYSTEM_STATUS_CHANGED_TIME", "value": P.now_ms()}]
                states.append({"_id": f"st{p.pid}", "partition_id": str(p.pid), "name": "ALARM_STATE", "value": p.alarm_state, "extraparams": ""})
                for i, t in enumerate(p.alarm_types):
                    alarmed.append({"_id": f"a{p.pid}{i}", "partition_id": str(p.pid), "sgroup": t, "zone_id": "", "action": "", "silenced": "false"})
            sets.append({"_id": "p0", "partition_id": "0", "name": "AC_STATUS", "value": "Connected"})
            for z in self.zones.values():
                pg.append({"_id": z.short_id, "shortID": z.short_id, "longID": "L" + z.short_id, "link_quality": "5", "link_status": "Connected"})
            return [{"uri": P.SENSOR_URI, "resultSet": sens}, {"uri": P.PARTITION_URI, "resultSet": parts},
                    {"uri": P.SETTINGS_URI, "resultSet": sets}, {"uri": P.STATE_URI, "resultSet": states},
                    {"uri": P.ALARMED_URI, "resultSet": alarmed}, {"uri": P.POWERG_URI, "resultSet": pg},
                    {"uri": P.IQREMOTE_URI, "resultSet": []}]

    def c4_zone(self, z: Zone):
        return {"id": str(z.zone_id), "type": z.sensortype, "name": z.name, "group": z.group, "status": z.status,
                "state": "0", "zone_id": z.zone_id, "zone_physical_type": 1, "zone_alarm_type": 1, "zone_type": 1,
                "partition_id": z.partition_id}

    def c4_summary(self):
        with self.lock:
            pl = []
            for p in self.partitions.values():
                st = P.ARMING_IQ_TO_C4.get(p.status, p.status)
                pl.append({"partition_id": p.pid, "name": p.name, "status": st, "secure_arm": False,
                           "zone_list": [self.c4_zone(z) for z in sorted(self.zones.values(), key=lambda z: z.zone_id)
                                         if z.partition_id == p.pid]})
            return {"event": "INFO", "info_type": "SUMMARY", "partition_list": pl, "nonce": "qolsys", "requestID": P.rid()}

    # expected contract view (before config overrides)
    def expected_zone(self, z: Zone):
        return {"panel_zone_id": z.zone_id, "kind": P.KIND_BY_SENSORTYPE.get(z.sensortype, "other"),
                "state": P.STATE_BY_STATUS.get(z.status, "unknown"), "name": z.name, "partition": z.partition_id,
                "battery_low": z.battery not in ("", "Normal")}


# ----------------------------------------------------------------------------------------------- IQ side
class IQSide:
    def __init__(self, sim: "PanelSim"):
        self.sim, self.pki = sim, sim.pki
        self.port, self.loop_port = free_port(), free_port()
        self.dir = sim.workdir / "iq"; self.dir.mkdir(parents=True, exist_ok=True)
        self.proc = None; self.cert_mode = "trusted"
        self.up = False; self.frozen = False; self.muted = False
        self.sessions = 0; self.commands = collections.Counter()
        self._mine = collections.Counter(); self._mine_lock = threading.Lock()
        self._evn = 0
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="sim-panel-logic")
        self.client.on_connect = lambda c, u, f, rc, p=None: c.subscribe("#", 0)
        self.client.on_message = self._on_message
        self.client.reconnect_delay_set(0.1, 0.5)
        self._write_certs("trusted")

    # ---- certs / process
    def _write_certs(self, mode):
        ca = self.pki.sim_ca if mode == "trusted" else self.pki.rogue_ca
        c, k = C.issue(ca, "Qolsys IQ Panel 5 MQTT")
        C.write_pair(self.dir, "server", c, k)
        self.server_fp = C.fingerprint_sha256(c); self.cert_mode = mode
        conf = f"""user root
persistence false
log_dest file {self.dir / 'mosquitto.log'}
log_type error
log_type warning
log_type notice
connection_messages true
per_listener_settings true
listener {self.port} 127.0.0.1
allow_anonymous true
cafile {self.pki.root / 'sim-ca.crt'}
certfile {self.dir / 'server.crt'}
keyfile {self.dir / 'server.key'}
require_certificate true
use_identity_as_username false
listener {self.loop_port} 127.0.0.1
allow_anonymous true
"""
        (self.dir / "mosquitto.conf").write_text(conf)
        if self.proc is None:
            self.proc = MosqProc(self.dir / "mosquitto.conf", self.dir / "mosquitto.log")

    def start(self):
        self.proc.start([self.port, self.loop_port]); self.up = True; self.frozen = False
        if not getattr(self, "_loop_started", False):
            self.client.connect_async("127.0.0.1", self.loop_port, 30); self.client.loop_start(); self._loop_started = True
        end = time.time() + 5
        while not self.client.is_connected() and time.time() < end: time.sleep(0.02)
        time.sleep(0.15)  # let '#' subscription land before any bridge connects

    def close(self):
        self.proc.kill()
        if getattr(self, "_loop_started", False):
            self.client.loop_stop(); self.client.disconnect()
        self.up = False

    # ---- scenario controls
    def outage(self):
        """Refuse connections (kill the broker; established sessions are reset)."""
        self.up = False; self.proc.kill()

    def restore(self):
        if not self.proc.alive(): self.start()
        else: self.unstall()

    def drop(self):
        """Drop every established connection but keep accepting new ones."""
        self.proc.kill(); self.start()

    def stall(self):
        """TCP stays open (kernel completes handshakes into the backlog) but nothing is ever sent or answered."""
        self.frozen = True; self.proc.freeze()

    def unstall(self):
        if self.frozen: self.proc.thaw(); self.frozen = False

    def mute(self, on=True):
        """App-level silence: broker + MQTT keepalive keep working, the 'panel' neither answers commands nor emits events."""
        self.muted = on

    def set_cert(self, mode="trusted"):
        """mode: 'trusted' (new cert from the sim CA = rotation) | 'rogue' (cert from an untrusted CA). Restarts the broker."""
        was_up = self.up
        self.proc.kill(); self.up = False
        self._write_certs(mode)
        if was_up: self.start()

    # ---- IO
    def _can_emit(self): return self.up and not self.frozen and not self.muted and self.proc.alive()

    def _pub(self, topic, obj):
        payload = json.dumps(obj).encode()
        with self._mine_lock: self._mine[(topic, payload)] += 1
        self.client.publish(topic, payload, 0)

    def _on_message(self, c, u, msg):
        key = (msg.topic, bytes(msg.payload))
        with self._mine_lock:
            if self._mine.get(key, 0) > 0:
                self._mine[key] -= 1; return
        audit = self.sim.audit
        verdict, reason = P.classify_iq(msg.topic, msg.payload)
        audit.add("iq", msg.topic, verdict, reason, msg.payload)
        if msg.topic != "mastermeid" or verdict != "allowed": 
            # still answer violations of mastermeid type so a misbehaving bridge keeps behaving "normally"
            if msg.topic != "mastermeid": return
        try: cmd = json.loads(msg.payload)
        except Exception: return
        en = cmd.get("eventName"); self.commands[en] += 1
        if self.muted or self.frozen: return
        resp = {"requestID": cmd.get("requestID"), "responseStatus": "success"}
        if en == "connect_v204":
            self.sessions += 1
            resp.update({"master_imei": "SIM-IMEI-0001", "primary_product_type": "IQP5", "eventName": "connect_v204"})
        elif en == "syncdatabase":
            resp["fulldbdata"] = self.sim.state.fulldb()
        elif en == "database":
            resp["resultSet"] = []
        topic = cmd.get("responseTopic") or "response_" + str(cmd.get("remoteMacAddress"))
        self._pub(topic, resp)

    def emit(self, ev: dict):
        if self._can_emit(): self._pub("iq2meid", ev)

    # event builders
    def ev_zone_update(self, z: Zone, extra=None):
        self._evn += 1
        cv = {"zoneid": str(z.zone_id), "sensorstatus": z.status, "time": z.time_ms, "battery_status": z.battery,
              "sensorname": z.name, "partition_id": str(z.partition_id)}
        cv.update(extra or {})
        sel = [str(z.zone_id)] if self._evn % 2 else f"[{z.zone_id}]"   # fw 4.6.1 list / fw 4.4.1 string
        return {"eventName": "dbChanged", "dbOperation": "update", "uri": P.SENSOR_URI, "contentValues": cv,
                "selection": "zoneid=?", "selectionArgs": sel}


# ----------------------------------------------------------------------------------------------- C4 side
class C4Side:
    def __init__(self, sim: "PanelSim", token="c4-token-0123456789abcdef"):
        self.sim, self.token = sim, token
        self.port = free_port()
        self.dir = sim.workdir / "c4"; self.dir.mkdir(parents=True, exist_ok=True)
        self.token_file = self.dir / "token"; self.token_file.write_text(token + "\n")
        self.up = False; self.muted = False; self.frozen_conns = False
        self.conns: set = set(); self.requests = 0; self.bad_token = 0; self.accepted = 0; self.tls_fail = 0
        self.loop = asyncio.new_event_loop(); self.server = None
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True); self.thread.start()
        self._new_cert()

    def _new_cert(self):
        cert, key = C.self_signed("Qolsys Panel C4")
        self.cert_pem, self.key_pem = C.cert_pem(cert), C.key_pem(key)
        self.fingerprint = C.fingerprint_sha256(cert)
        self.cert_file, self.key_file = self.dir / "panel.crt", self.dir / "panel.key"
        self.cert_file.write_bytes(self.cert_pem); self.key_file.write_bytes(self.key_pem)

    def _run(self, coro, timeout=10):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    # ---- controls
    def start(self): self._run(self._start()); self.up = True
    def outage(self): self._run(self._stop()); self.up = False
    def restore(self): 
        if not self.up: self.start()
    def drop(self): self._run(self._drop())
    def mute(self, on=True): self.muted = on
    stall = lambda self: self.mute(True)
    unstall = lambda self: self.mute(False)
    def rotate_cert(self):
        """New self-signed cert => new fingerprint (a bridge pinned to the old one must refuse). Drops sessions."""
        was = self.up
        if was: self.outage()
        self._new_cert()
        if was: self.start()

    def close(self):
        try: self.outage()
        except Exception: pass
        self.loop.call_soon_threadsafe(self.loop.stop)

    async def _start(self):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(self.cert_file, self.key_file)
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", self.port, ssl=ctx, reuse_address=True)

    async def _stop(self):
        if self.server: self.server.close(); self.server = None
        await self._drop()

    async def _drop(self):
        for w in list(self.conns):
            try: w.transport.abort()
            except Exception: pass
        self.conns.clear()

    async def _handle(self, reader, writer):
        self.accepted += 1; self.conns.add(writer)
        dec = json.JSONDecoder(); buf = ""; got_bytes = False
        try:
            while True:
                data = await reader.read(65536)
                if not data: break
                got_bytes = True
                text = data.decode("utf-8", "replace")
                buf += text
                while buf.strip():
                    buf = buf.lstrip()
                    if buf[0] != "{":
                        self.sim.audit.add("c4", "garbage", "violation", "non-JSON bytes sent to panel", buf[:200]); buf = ""; break
                    try:
                        obj, end = dec.raw_decode(buf)
                    except json.JSONDecodeError:
                        if len(buf) > 1_000_000:
                            self.sim.audit.add("c4", "garbage", "violation", "oversized/unparsable JSON", buf[:200]); buf = ""
                        break
                    raw, buf = buf[:end], buf[end:]
                    await self._on_action(obj, raw, writer)
        except (ConnectionError, ssl.SSLError, asyncio.IncompleteReadError):
            pass
        finally:
            self.conns.discard(writer)
            try: writer.transport.abort()
            except Exception: pass

    async def _on_action(self, obj, raw, writer):
        self.requests += 1
        verdict, reason = P.classify_c4(obj)
        self.sim.audit.add("c4", "action", verdict, reason, raw)
        if self.muted: return
        if obj.get("token") != self.token:
            self.bad_token += 1
            await self._send(writer, {"event": "ERROR", "error_type": "INVALID_TOKEN", "description": "Invalid token",
                                      "partition_id": 0, "version": 0, "requestID": P.rid()})
            writer.transport.abort(); return
        await self._send(writer, {"event": "ACK", "requestID": P.rid(), "version": 0})
        if obj.get("action") == "INFO" and obj.get("info_type") == "SUMMARY":
            await self._send(writer, self.sim.state.c4_summary() | {"version": 0})

    async def _send(self, writer, obj):
        try:
            writer.write((json.dumps(obj) + "\n").encode()); await writer.drain()
        except Exception: pass

    def emit(self, ev: dict):
        if not self.up or self.muted: return
        async def go():
            for w in list(self.conns): await self._send(w, ev)
        asyncio.run_coroutine_threadsafe(go(), self.loop)

    def _env(self, **kw): return {"version": 0, "requestID": P.rid(), **kw}


# ----------------------------------------------------------------------------------------------- facade
class PanelSim:
    """sim = PanelSim(workdir, pki); sim.start(); sim.set_zone(12, 'Open'); sim.iq.outage(); ..."""

    def __init__(self, workdir: Path, pki: C.Pki, state: PanelState | None = None):
        self.workdir = Path(workdir); self.workdir.mkdir(parents=True, exist_ok=True)
        self.pki = pki; self.state = state or PanelState.default()
        self.audit = Audit(self.workdir / "audit.jsonl")
        self.iq = IQSide(self); self.c4 = C4Side(self)

    def start(self): self.iq.start(); self.c4.start(); return self
    def close(self): self.iq.close(); self.c4.close()

    # ---- scenario API (truth changes + event fan-out; a side that is down/stalled/muted loses the event) ----
    def set_zone(self, zone_id: int, status: str | None = None, *, battery=None, name=None, emit=True):
        with self.state.lock:
            z = self.state.zones[zone_id]
            if status is not None: z.status = status
            if battery is not None: z.battery = battery
            if name is not None: z.name = name
            z.time_ms = P.now_ms()
        if emit:
            self.iq.emit(self.iq.ev_zone_update(z))
            kind = "ZONE_ACTIVE" if z.status in ("Open", "Active", "Activated", "Tampered") and battery is None and name is None else "ZONE_UPDATE"
            zone = {"status": z.status, "zone_id": z.zone_id} if kind == "ZONE_ACTIVE" else self.state.c4_zone(z)
            self.c4.emit(self.c4._env(event="ZONE_EVENT", zone_event_type=kind, zone=zone))

    def add_zone(self, zone_id, name, sensortype="Door_Window", status="Closed", partition_id=0, emit=True):
        with self.state.lock:
            z = Zone(zone_id, name, sensortype, status, partition_id); self.state.zones[zone_id] = z
        if emit:
            self.iq.emit({"eventName": "dbChanged", "dbOperation": "insert", "uri": P.SENSOR_URI, "contentValues": z.row()})
            self.c4.emit(self.c4._env(event="ZONE_EVENT", zone_event_type=" ZONE_ADD", zone=self.state.c4_zone(z)))

    def remove_zone(self, zone_id, emit=True):
        with self.state.lock: self.state.zones.pop(zone_id)
        if emit:   # NOTE: Control4 has no remove event (see PANEL_PROTOCOL.md); only a later SUMMARY reveals it
            self.iq.emit({"eventName": "dbChanged", "dbOperation": "delete", "uri": P.SENSOR_URI,
                          "selection": "zoneid=?", "selectionArgs": [str(zone_id)]})

    def set_arming(self, pid: int, status: str, emit=True):
        """status: DISARM | ARM-STAY | ARM-AWAY | ARM-NIGHT | ARM-AWAY-EXIT-DELAY | ..."""
        with self.state.lock:
            p = self.state.partitions[pid]; p.status = status
            if status == "DISARM": p.alarm_state = "None"; p.alarm_types = []
        if emit:
            self.iq.emit({"eventName": "dbChanged", "dbOperation": "update", "uri": P.SETTINGS_URI,
                          "contentValues": {"name": "SYSTEM_STATUS", "value": status, "partition_id": str(pid)},
                          "selection": "name=? AND partition_id=?", "selectionArgs": ["SYSTEM_STATUS", str(pid)]})
            self.c4.emit(self.c4._env(event="ARMING", arming_type=P.ARMING_IQ_TO_C4.get(status, status), partition_id=pid))

    def set_alarm(self, pid: int, active=True, alarm_type="glassbreak", emit=True):
        with self.state.lock:
            p = self.state.partitions[pid]
            p.alarm_state = "Alarm" if active else "None"; p.alarm_types = [alarm_type] if active else []
        if emit:
            self.iq.emit({"eventName": "dbChanged", "dbOperation": "update", "uri": P.STATE_URI,
                          "contentValues": {"name": "ALARM_STATE", "value": p.alarm_state, "partition_id": str(pid)},
                          "selection": "name=? AND partition_id=?", "selectionArgs": ["ALARM_STATE", str(pid)]})
            if active:
                self.iq.emit({"eventName": "dbChanged", "dbOperation": "insert", "uri": P.ALARMED_URI,
                              "contentValues": {"partition_id": str(pid), "sgroup": alarm_type, "zone_id": "", "action": ""}})
                self.c4.emit(self.c4._env(event="ALARM", alarm_type="", partition_id=pid))

    def diverge_while_down(self, fn):
        """Run fn(sim) with all events suppressed (state changes the bridge never sees as events)."""
        saved = self.iq.muted, self.c4.muted
        self.iq.muted = self.c4.muted = True
        try: fn(self)
        finally: self.iq.muted, self.c4.muted = saved

    # convenience for tests
    @property
    def violations(self): return self.audit.violations()
    def zone_status(self, zid): return self.state.zones[zid].status
