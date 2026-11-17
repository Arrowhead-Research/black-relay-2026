"""MQTT publisher. WRITE-ONLY toward the consumer broker: it never subscribes, so it can never receive a
command topic. The consumer broker's ACL additionally denies every non-bridge user any write."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import ssl

import aiomqtt

from .. import config as C
from .state import OutState
from ..normalizer.store import Reader

log = logging.getLogger("br2.mqtt")


def ha_discovery(prefix: str, topic_prefix: str, z: dict) -> tuple[str, str]:
    dc = {"contact": "opening", "motion": "motion", "glass_break": "vibration"}.get(z["kind"])
    on = "open" if z["kind"] == "contact" else "active"
    cfg = {"name": z["name"], "unique_id": f"blackrelay_{z['id']}",
           "state_topic": f"{topic_prefix}/zones/{z['id']}/state",
           "value_template": "{{ 'ON' if value_json.state == '" + on + "' else 'OFF' }}",
           "payload_on": "ON", "payload_off": "OFF",
           "availability_topic": f"{topic_prefix}/bridge/status",
           "availability_template": "{{ 'online' if value_json.status in ['online','degraded'] else 'offline' }}",
           "json_attributes_topic": f"{topic_prefix}/zones/{z['id']}/state",
           "device": {"identifiers": ["blackrelay"], "name": "BlackRelay", "manufacturer": "BlackRelay"}}
    if dc:
        cfg["device_class"] = dc
    return f"{prefix}/binary_sensor/blackrelay_{z['id']}/config", json.dumps(cfg, sort_keys=True)


class PublishedSet:
    """Durable record of every retained topic this bridge has created on the broker (state topics + HA discovery).
    Lets a restarted outbound clear ghosts (zones removed while it was down) WITHOUT subscribing to the broker.
    Ids are added (and saved) BEFORE the first publish and removed AFTER the clearing publish succeeded, so a crash
    at any point leaves a superset of what exists on the broker."""

    def __init__(self, path: str):
        self.path = path
        self.d: dict[str, set[str]] = {"zones": set(), "disc": set(), "parts": set()}
        try:
            raw = json.load(open(path))
            for k in self.d:
                self.d[k] = set(str(x) for x in raw.get(k, []))
        except (OSError, ValueError, AttributeError):
            pass

    def save(self) -> None:
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump({k: sorted(v) for k, v in self.d.items()}, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except OSError as e:
            log.warning("cannot save published set: %s", e)

    def add(self, kind: str, ident: str) -> None:
        if ident not in self.d[kind]:
            self.d[kind].add(ident)
            self.save()

    def discard(self, kind: str, ident: str) -> None:
        if ident in self.d[kind]:
            self.d[kind].discard(ident)
            self.save()


class ReplayPointer:
    """Highest event seq published to the broker (acknowledged QoS1 publish). Saved after EVERY message, tied to the
    database identity so a recreated database is replayed from its start. fsync every 32 messages and at flush end:
    a process crash never loses a saved value; a power cut may repeat up to 32 already-delivered events (same
    seq/id: consumers dedupe by seq)."""

    def __init__(self, path: str):
        self.path = path
        self.seq = 0
        self.db_id: str | None = None
        self._unsynced = 0
        try:
            d = json.load(open(path))
            self.seq, self.db_id = int(d["seq"]), d.get("db_id")
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def save(self, seq: int, db_id: str | None, sync: bool = False) -> None:
        self.seq, self.db_id = seq, db_id
        self._unsynced += 1
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump({"seq": seq, "db_id": db_id}, f)
                if sync or self._unsynced >= 32:
                    f.flush()
                    os.fsync(f.fileno())
                    self._unsynced = 0
            os.replace(tmp, self.path)
        except OSError as e:
            log.warning("cannot save replay pointer: %s", e)


class MqttPublisher:
    def __init__(self, cfg, state: OutState, reader: Reader):
        self.cfg, self.st, self.reader = cfg, state, reader
        m = cfg.mqtt
        if not m.get("tls", True) and not m.get("insecure_dev"):
            raise SystemExit("mqtt.tls is false: refusing plaintext MQTT (set mqtt.insecure_dev: true for a throw-away "
                             "development broker only)")
        if m.get("tls", True) and not m.get("ca_file") and not m.get("insecure_dev"):
            raise SystemExit("mqtt.ca_file is not set: refusing to fall back to the system CA store for the broker "
                             "(set mqtt.ca_file, or mqtt.insecure_dev: true for a throw-away development broker only)")
        self.prefix = m["topic_prefix"].rstrip("/")
        self.ha = bool(m.get("ha_discovery"))
        self.ha_prefix = m.get("ha_discovery_prefix", "homeassistant")
        self._disc: dict[str, str] = {}
        sd = C.out_state_dir(cfg)
        os.makedirs(sd, exist_ok=True)
        self.seq_file = os.path.join(sd, "outbound.mqtt_seq")
        self.pub_set = PublishedSet(os.path.join(sd, "outbound.published.json"))
        self.ptr = ReplayPointer(self.seq_file)
        self.published_seq: int | None = None

    def _tls(self):
        m = self.cfg.mqtt
        if not m.get("tls", True):
            return None
        ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=m.get("ca_file"))
        ctx.check_hostname = True
        ctx.verify_mode = ssl.CERT_REQUIRED
        ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT   # private CAs often lack KeyUsage; chain+hostname still verified
        return ctx

    def _password(self) -> str | None:
        pf = self.cfg.mqtt.get("password_file")
        if pf and os.path.exists(pf):
            return open(pf).read().strip()
        return None

    async def _pub(self, c, topic, payload, retain=False) -> None:
        await c.publish(topic, payload, qos=1, retain=retain)
        self.st.counters["mqtt_published"] += 1

    async def run(self) -> None:
        m = self.cfg.mqtt
        t = self.cfg.timers
        delay = float(t["backoff_initial_s"])
        while True:
            try:
                client = aiomqtt.Client(
                    hostname=m["host"], port=int(m["port"]), username=m.get("username"),
                    password=self._password(), tls_context=self._tls(), identifier=m["client_id"],
                    keepalive=30, clean_session=True,
                    will=aiomqtt.Will(f"{self.prefix}/bridge/status", json.dumps({"status": "offline"}), qos=1, retain=True))
                async with client as c:
                    self.st.mqtt_connected = True
                    delay = float(t["backoff_initial_s"])
                    self._disc.clear()
                    await self._loop(c)
            except aiomqtt.MqttError as e:
                log.warning("mqtt down: %s", e)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("mqtt publisher error")
            self.st.mqtt_connected = False
            self.st.counters["mqtt_reconnects"] += 1
            await asyncio.sleep(delay)
            delay = min(delay * 2, float(t["backoff_max_s"]))

    async def clear_ghosts(self, c) -> None:
        """Retained topics we created earlier for ids that no longer exist (removed while we were down)."""
        P = self.prefix
        for zid in sorted(self.pub_set.d["zones"] - set(self.st.zones)):
            await self._pub(c, f"{P}/zones/{zid}/state", b"", True)
            self.pub_set.discard("zones", zid)
        for zid in sorted(self.pub_set.d["disc"] - (set(self.st.zones) if self.ha else set())):
            await self._pub(c, f"{self.ha_prefix}/binary_sensor/blackrelay_{zid}/config", b"", True)
            self.pub_set.discard("disc", zid)
        for pid in sorted(self.pub_set.d["parts"] - set(self.st.parts)):
            await self._pub(c, f"{P}/partitions/{pid}/state", b"", True)
            self.pub_set.discard("parts", pid)

    async def _loop(self, c) -> None:
        P = self.prefix
        while not self.st.have_dump:                 # never judge "ghost" before we know the real set
            ev = self.st.changed
            if self.st.have_dump:
                break
            try:
                await asyncio.wait_for(ev.wait(), 5)
            except asyncio.TimeoutError:
                pass
        await self.clear_ghosts(c)
        self.st.mark_all_dirty()
        while True:
            ev = self.st.changed
            zs, rz, ps, rp, sd = self.st.take_dirty()
            try:
                for zid in sorted(zs):
                    z = self.st.zones.get(zid)
                    if not z:
                        continue
                    self.pub_set.add("zones", zid)
                    await self._pub(c, f"{P}/zones/{zid}/state", json.dumps(z), True)
                    if self.ha:
                        topic, body = ha_discovery(self.ha_prefix, P, z)
                        if self._disc.get(zid) != body:
                            self.pub_set.add("disc", zid)
                            await self._pub(c, topic, body, True)
                            self._disc[zid] = body
                for zid in sorted(rz):
                    await self._pub(c, f"{P}/zones/{zid}/state", b"", True)
                    self.pub_set.discard("zones", zid)
                    if self.ha:
                        await self._pub(c, f"{self.ha_prefix}/binary_sensor/blackrelay_{zid}/config", b"", True)
                        self.pub_set.discard("disc", zid)
                        self._disc.pop(zid, None)
                for pid in sorted(ps):
                    p = self.st.parts.get(pid)
                    if p:
                        self.pub_set.add("parts", pid)
                        await self._pub(c, f"{P}/partitions/{pid}/state", json.dumps(p), True)
                for pid in sorted(rp):
                    await self._pub(c, f"{P}/partitions/{pid}/state", b"", True)
                    self.pub_set.discard("parts", pid)
                await self._flush_events(c)
                if sd:
                    hv = self.st.health_view()
                    await self._pub(c, f"{P}/bridge/status", json.dumps({"status": hv["status"]}), True)
            except BaseException:
                # put work back so the next connection republishes it
                self.st.dirty_zones |= zs
                self.st.removed_zones |= rz
                self.st.dirty_parts |= ps
                self.st.removed_parts |= rp
                self.st.status_dirty = self.st.status_dirty or sd
                raise
            try:
                await asyncio.wait_for(ev.wait(), 5)
            except asyncio.TimeoutError:
                pass

    async def _flush_events(self, c) -> None:
        """Replay committed events not yet published, paging through ALL of them (no cap, including events committed
        before this service ever ran). Pointer saved per message."""
        did = self.reader.db_id()
        if self.published_seq is None:
            self.published_seq = self.ptr.seq if (self.ptr.db_id in (None, did) or did is None) else 0
        while True:
            head, floor = self.reader.last_seq(), self.reader.floor()
            if self.published_seq > head:
                self.published_seq = head                 # database was reset/restored behind us
            if self.published_seq < floor - 1:
                self.published_seq = floor - 1            # pruned while we were away: nothing older exists
            evs = self.reader.events_after(self.published_seq, 500)
            if not evs:
                break
            for e in evs:
                await self._pub(c, f"{self.prefix}/events", json.dumps(e), False)
                self.published_seq = e["seq"]
                self.ptr.save(e["seq"], did)
            self.ptr.save(self.published_seq, did, sync=True)
