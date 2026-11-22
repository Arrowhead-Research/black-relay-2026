"""IQ Remote adapter (primary): speaks the panel's local MQTT protocol, strictly read-only.

Outbound guard
  The only object that touches the panel's MQTT client is GuardedPanel. It
    * publishes ONLY to topic `mastermeid`, ONLY envelopes whose eventName is in ALLOWED_EVENTS
      (connect_v204, pingevent, pair_status_request, syncdatabase), never any IPC-transaction (ipcRequest) payload,
      never a `database` insert/update/delete;
    * subscribes ONLY to `iq2meid` and `response_<mac>`.
  Anything else raises OneWayViolation (counted, logged) *before* bytes reach the socket. The qolsys_controller
  command builders (arm/disarm/IPC-transaction) are not imported.
"""
from __future__ import annotations

import asyncio
import glob
import json
import logging
import os
import re
import secrets
import socket
import ssl
import time
import uuid
from typing import Any

import aiomqtt

from . import beacon
from .. import config as C
from .base import AdapterBase, OneWayViolation
from ..pin import PanelPin, PinMismatch, pinned_socket_class, pki_folders
from ..keyperm import require_private_key

log = logging.getLogger("br2.iq_remote")

PUB_TOPIC = "mastermeid"
SUB_TOPICS_STATIC = ("iq2meid",)

# ---- exact wire shapes. The bridge may publish exactly these four envelopes, with exactly these keys (the shapes
# the qolsys_controller library itself uses for its session handshake, keep-alive and database read). Any other
# event name, any missing key, any extra key, or any envelope key that is not the fixed value => OneWayViolation.
ENVELOPE_KEYS = ("requestID", "responseTopic", "eventName", "remoteMacAddress")
BATT_KEYS = ("current_battery_status", "remote_panel_battery_percentage", "remote_panel_battery_temperature",
             "remote_panel_battery_status", "remote_panel_battery_scale", "remote_panel_battery_voltage",
             "remote_panel_battery_present", "remote_panel_battery_technology", "remote_panel_battery_level",
             "remote_panel_battery_health", "remote_panel_plugged")
CONNECT_KEYS = ("ipAddress", "pairing_request", "macAddress", "remoteClientID", "softwareVersion", "productType",
                "bssid", "lastUpdateChecksum", "dealerIconsCheckSum", "remote_feature_support_version",
                "dhcpInfo") + BATT_KEYS
PING_KEYS = ("remote_panel_status", "macAddress", "ipAddress") + BATT_KEYS
EXACT_KEYS: dict[str, frozenset] = {
    "connect_v204": frozenset(ENVELOPE_KEYS + CONNECT_KEYS),
    "pingevent": frozenset(ENVELOPE_KEYS + PING_KEYS),
    "pair_status_request": frozenset(ENVELOPE_KEYS),
    "syncdatabase": frozenset(ENVELOPE_KEYS),
}
ALLOWED_EVENTS = frozenset(EXACT_KEYS)
_SCALARS = (str, int, bool)
SETTLE_S = float(os.environ.get("BR2_SETTLE_S", "2.0"))        # library: sleep(2) between subscribe and session init
HANDSHAKE_TIMEOUT_S = 30.0                                      # library: mqtt_command_timeout = 30
LIB_PING_PERIOD_S = 300.0                                       # library: unconditional pingevent every 300 s

URI_SENSOR = "content://com.qolsys.qolsysprovider.SensorContentProvider/sensor"
URI_PARTITION = "content://com.qolsys.qolsysprovider.PartitionContentProvider/partition"
URI_SETTINGS = "content://com.qolsys.qolsysprovider.QolsysSettingsProvider/qolsyssettings"
URI_STATE = "content://com.qolsys.qolsysprovider.StateContentProvider/state"
URI_ALARMED = "content://com.qolsys.qolsysprovider.AlarmedSensorProvider/alarmedsensor"


def _no_duplicate_keys(pairs: list) -> dict:
    """json object_pairs_hook: a repeated key means two parsers can read the same bytes differently (this guard
    last-wins, another reader first-wins), so the guard refuses the payload outright."""
    out: dict = {}
    for k, v in pairs:
        if k in out:
            raise ValueError(f"duplicate JSON key {k!r}")
        out[k] = v
    return out


class _Wire:
    """Sole holder of the raw MQTT client; every publish/subscribe is re-validated here (not only in GuardedPanel's
    own methods), so code that reaches for `guard._client` still cannot emit a forbidden frame."""

    def __init__(self, client: Any, check_pub, check_sub):
        self._raw = client
        self._check_pub, self._check_sub = check_pub, check_sub

    async def publish(self, topic: str, payload: Any = None, qos: int = 0, retain: bool = False, **kw) -> Any:
        self._check_pub(topic, payload, qos, retain)
        return await self._raw.publish(topic, payload, qos=qos, retain=retain, **kw)

    async def subscribe(self, topic: Any, qos: int = 0, **kw) -> Any:
        self._check_sub(topic)
        return await self._raw.subscribe(topic, qos=qos, **kw)

    async def __aenter__(self):  # pragma: no cover
        return await self._raw.__aenter__()

    async def __aexit__(self, *a):  # pragma: no cover
        return await self._raw.__aexit__(*a)


class GuardedPanel:
    """Sole holder of the panel MQTT client. Everything else calls request()."""

    def __init__(self, client: Any, mac: str, on_violation=None):
        self.mac = mac
        self.resp_topic = f"response_{mac}"
        self._client = _Wire(client, self.check_publish, self._check_subscribe)
        self._pending: dict[str, asyncio.Future] = {}
        self._on_violation = on_violation or (lambda: None)
        self.sent: list[tuple[str, str]] = []  # audit trail (topic, eventName)

    # ---- guards
    def check_publish(self, topic: str, payload: Any, qos: int = 0, retain: bool = False) -> dict:
        try:
            if isinstance(payload, (bytes, bytearray)):
                payload = bytes(payload).decode()
            obj = payload if isinstance(payload, dict) else json.loads(payload, object_pairs_hook=_no_duplicate_keys)
        except Exception as e:  # noqa: BLE001
            self._violation(f"unparseable publish to {topic}" + (" (duplicate keys)" if "duplicate JSON key" in str(e) else ""))
        if topic != PUB_TOPIC:
            self._violation(f"publish to forbidden topic {topic}")
        if qos != 0 or retain:
            self._violation("publish with qos/retain different from the library's")
        if not isinstance(obj, dict):
            self._violation("publish payload is not an object")
        ev = obj.get("eventName")
        keys = EXACT_KEYS.get(ev) if isinstance(ev, str) else None
        if keys is None:
            self._violation(f"forbidden eventName {ev!r}")
        if set(obj) != keys:
            extra, missing = sorted(set(obj) - keys), sorted(keys - set(obj))
            self._violation(f"{ev}: key set differs from the allow-list (extra={extra} missing={missing})")
        if any(not isinstance(v, _SCALARS) for v in obj.values()):
            self._violation(f"{ev}: non-scalar value")
        if obj["responseTopic"] != self.resp_topic or obj["remoteMacAddress"] != self.mac:
            self._violation(f"{ev}: envelope identity differs from ours")
        if not isinstance(obj["requestID"], str) or not re.fullmatch(r"[0-9a-f\-]{36}", obj["requestID"]):
            self._violation(f"{ev}: bad requestID")
        if ev in ("connect_v204", "pingevent") and obj["macAddress"] != self.mac:
            self._violation(f"{ev}: macAddress differs from ours")
        if ev == "connect_v204" and obj["pairing_request"] is not True:
            self._violation("connect_v204: pairing_request must be true")
        if ev == "pingevent" and obj["remote_panel_status"] != "Active":
            self._violation("pingevent: remote_panel_status")
        return obj

    def _violation(self, why: str) -> None:
        self._on_violation()
        log.error("ONE-WAY GUARD blocked panel write: %s", why)
        raise OneWayViolation(why)

    def _check_subscribe(self, topic: Any) -> None:
        names = [topic] if isinstance(topic, str) else [t[0] if isinstance(t, (tuple, list)) else t for t in topic]
        for n in names:
            if n not in (*SUB_TOPICS_STATIC, self.resp_topic):
                self._violation(f"subscribe to {n}")

    async def publish(self, topic: str, payload: Any) -> None:
        obj = self.check_publish(topic, payload)
        await self._client.publish(topic, json.dumps(obj), qos=0)
        self.sent.append((topic, obj["eventName"]))

    async def subscribe_allowed(self) -> None:
        for t in (*SUB_TOPICS_STATIC, self.resp_topic):
            await self._client.subscribe(t, qos=0)

    async def subscribe(self, topic: str) -> None:
        await self._client.subscribe(topic, qos=0)

    async def request(self, event: str, fields: dict | None = None, timeout: float = HANDSHAKE_TIMEOUT_S) -> dict:
        rid = str(uuid.uuid4())
        env = {"requestID": rid, "responseTopic": self.resp_topic, "eventName": event,
               "remoteMacAddress": self.mac}
        for k, v in (fields or {}).items():
            if k in ENVELOPE_KEYS:
                self._violation(f"{event}: caller tried to set envelope key {k}")
            env[k] = v
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        try:
            await self.publish(PUB_TOPIC, env)
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(rid, None)

    def deliver_response(self, obj: Any) -> None:
        """Hostile/garbled responses must never raise into the listener."""
        if not isinstance(obj, dict):
            return
        rid = obj.get("requestID")
        if not isinstance(rid, str):
            return
        f = self._pending.get(rid)
        if f and not f.done():
            f.set_result(obj)


def harden_paho(client: Any, guard: GuardedPanel) -> None:
    """Transport-level enforcement: wrap the paho client's publish/subscribe inside the aiomqtt client so that even
    a direct `aiomqtt_client.publish(...)` elsewhere in the process is validated by the same allow-list."""
    raw = getattr(client, "_client", None)
    if raw is None:
        return
    o_pub, o_sub = raw.publish, raw.subscribe

    def pub(topic, payload=None, qos=0, retain=False, *a, **kw):
        guard.check_publish(topic, payload, qos, retain)
        return o_pub(topic, payload, qos, retain, *a, **kw)

    def sub(topic, *a, **kw):
        guard._check_subscribe(topic)
        return o_sub(topic, *a, **kw)
    raw.publish, raw.subscribe = pub, sub


# ------------------------------------------------------------------ PKI / TLS
def find_pki(pki_dir: str, ca_override: str | None) -> tuple[str, str, str, str]:
    """Return (id12, key, secure, ca). Layout: <pki_dir>/<12hex>/<id>.{key,secure,qolsys}, else
    <pki_dir>/pki/<12hex>/ (the qolsys-controller library layout), else flat."""
    base = str(pki_folders(pki_dir)[1][0]) if pki_folders(pki_dir)[1] else pki_dir
    keys = glob.glob(os.path.join(base, "*.key"))
    if not keys:
        raise FileNotFoundError(f"no pairing key in {pki_dir}")
    stem = os.path.basename(keys[0])[:-4]
    secure = os.path.join(base, stem + ".secure")
    ca = ca_override or os.path.join(base, stem + ".qolsys")
    for p in (secure, ca):
        if not os.path.exists(p):
            raise FileNotFoundError(p)
    return stem.lower(), keys[0], secure, ca


def fmt_mac(id12: str) -> str:
    return ":".join(id12[i:i + 2] for i in range(0, 12, 2))


def tls_context(key: str, secure: str, ca: str, pin: Any = None) -> ssl.SSLContext:
    """Exactly the qolsys_controller library's context: create_default_context(SERVER_AUTH, cafile=<panel CA>)
    (=> verify_flags has VERIFY_X509_PARTIAL_CHAIN; only the panel CA is trusted), SECLEVEL=0 ciphers, TLS>=1.2,
    hostname check off (we connect by IP; the panel cert has no matching SAN), STRICT X.509 relaxed because the panel
    CA lacks a critical BasicConstraints, our pairing client certificate presented.
    With `pin` (a br2.pin.PanelPin) the server certificate's SPKI must also match the pin, checked inside the handshake
    (before any MQTT byte is written): the panel CA signs every paired device's certificate too, so a CA check alone
    would accept any of them as "the panel"."""
    ctx = ssl.create_default_context(purpose=ssl.Purpose.SERVER_AUTH, cafile=ca)
    ctx.set_ciphers("DEFAULT:@SECLEVEL=0")
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
    ctx.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
    ctx.load_cert_chain(certfile=secure, keyfile=key)
    if pin is not None:
        ctx.sslsocket_class = pinned_socket_class(pin)
    return ctx


def detect_ip(panel_ip: str) -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect((panel_ip, 9))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


# ------------------------------------------------------------------ DB -> observations
def _int(v: Any, d: int = 0) -> int:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return d


class BadZoneId(ValueError):
    """A zone row carries a zone id that is not a plain non-negative integer: it must never be coerced (to 0 or to
    anything else); the caller rejects the row, counts it and asks for a re-read."""


_MAX_ZID = 2 ** 31


def _zid(v: Any) -> int:
    t = str(v).strip() if isinstance(v, (str, int)) and not isinstance(v, bool) else ""
    if not (t.isascii() and t.isdigit() and int(t) < _MAX_ZID):
        raise BadZoneId(repr(v)[:40])
    return int(t)


def zone_obs(row: dict) -> dict | None:
    """Observation for one sensor-table row. None: the row has no zone id at all. Raises BadZoneId: it has one that is
    not a plain non-negative integer."""
    zid = row.get("zoneid", row.get("zone_id"))
    if zid in (None, ""):
        return None
    zid = _zid(zid)
    raw_keys = ("sensorstatus", "sensortype", "sensorgroup", "battery_status", "sensorstate", "time",
                "current_capability", "shortID", "zone_type", "zone_physical_type")
    o: dict[str, Any] = {"panel_zone_id": zid, "raw": {k: row[k] for k in raw_keys if k in row}}
    for src, dst in (("sensorname", "name"), ("sensortype", "type"), ("sensorgroup", "group"),
                     ("sensorstatus", "status"), ("battery_status", "battery")):
        if src in row:
            o[dst] = row[src]
    if "partition_id" in row and row["partition_id"] not in (None, ""):
        o["partition_id"] = _int(row["partition_id"])
    return o


def has_sensor_table(fulldb: list[dict]) -> bool:
    return any(isinstance(t, dict) and t.get("uri") == URI_SENSOR for t in fulldb or [])


def build_model(fulldb: list[dict], on_bad_zone=None) -> tuple[dict[int, dict], dict[int, dict]]:
    tables: dict[str, list[dict]] = {}
    for t in fulldb or []:
        tables.setdefault(t.get("uri", ""), []).extend(t.get("resultSet") or [])
    zones: dict[int, dict] = {}
    for r in tables.get(URI_SENSOR, []):
        try:
            o = zone_obs(r)
        except BadZoneId:
            if on_bad_zone:
                on_bad_zone()
            continue
        if o:
            zones[o["panel_zone_id"]] = o
    parts: dict[int, dict] = {}
    for r in tables.get(URI_PARTITION, []):
        pid = _int(r.get("partition_id"))
        parts[pid] = {"partition_id": pid, "name": r.get("name") or f"Partition {pid}"}
    for r in tables.get(URI_SETTINGS, []):
        if r.get("name") == "SYSTEM_STATUS":
            pid = _int(r.get("partition_id"))
            parts.setdefault(pid, {"partition_id": pid})["status"] = r.get("value")
    for r in tables.get(URI_STATE, []):
        if r.get("name") == "ALARM_STATE":
            pid = _int(r.get("partition_id"))
            parts.setdefault(pid, {"partition_id": pid})["alarm_state"] = r.get("value")
    for r in tables.get(URI_ALARMED, []):
        pid = _int(r.get("partition_id"))
        if pid in parts and r.get("sgroup"):
            prev = parts[pid].get("alarm_type")
            parts[pid]["alarm_type"] = r["sgroup"] if not prev else prev
    return zones, parts


def sel_args(msg: dict) -> list[str]:
    a = msg.get("selectionArgs")
    if isinstance(a, str):
        try:
            a = json.loads(a)
        except Exception:  # noqa: BLE001
            a = [x.strip() for x in a.strip("[]").split(",") if x.strip()]
    return [str(x) for x in (a or [])]


# ------------------------------------------------------------------ exact payload bodies (shared with br2 pair/snapshot/watch)
def batt_fields() -> dict:
    return {"current_battery_status": "Normal", "remote_panel_battery_percentage": 100,
            "remote_panel_battery_temperature": 430, "remote_panel_battery_status": 3,
            "remote_panel_battery_scale": 100, "remote_panel_battery_voltage": 4102,
            "remote_panel_battery_present": True, "remote_panel_battery_technology": "",
            "remote_panel_battery_level": 100, "remote_panel_battery_health": 2, "remote_panel_plugged": 1}


def connect_payload(mac: str, plugin_ip: str, remote_client_id: str) -> dict:
    return {"ipAddress": plugin_ip, "pairing_request": True, "macAddress": mac,
            "remoteClientID": remote_client_id, "softwareVersion": "4.4.1",
            "productType": "tab07_rk68", "bssid": "", "lastUpdateChecksum": "2132501716",
            "dealerIconsCheckSum": "", "remote_feature_support_version": "1",
            "dhcpInfo": json.dumps({"ipaddress": "", "gateway": "", "netmask": "", "dns1": "", "dns2": "",
                                    "dhcpServer": "", "leaseDuration": ""}), **batt_fields()}


def ping_payload(mac: str, plugin_ip: str) -> dict:
    return {"remote_panel_status": "Active", "macAddress": mac, "ipAddress": plugin_ip, **batt_fields()}


def tls_info(client: Any) -> dict:
    """Negotiated TLS version and cipher of the live panel connection (field evidence). Reads the socket only."""
    try:
        sock = client._client.socket()          # paho: the SSLSocket of the established connection
        cipher = sock.cipher()
        return {"version": sock.version(), "cipher": cipher[0] if cipher else None,
                "secret_bits": cipher[2] if cipher else None}
    except Exception:  # noqa: BLE001
        return {"version": None, "cipher": None, "secret_bits": None}


class IQRemoteAdapter(AdapterBase):
    name = "iq_remote"

    def __init__(self, cfg, ipc_dir=None):
        super().__init__(cfg, ipc_dir)
        self.iqc = cfg.iq
        self.guard: GuardedPanel | None = None
        self._loading = False
        self._buffer: list[dict] = []
        self._resync = asyncio.Event()
        self.zones_complete = True
        # like the library: ONE remoteClientID per process, used both as MQTT client id and in connect_v204
        self.remote_client_id = "qolsys-controller-" + secrets.token_hex(6)
        try:
            self.pin = PanelPin(self.iqc["pki_dir"], self.iqc.get("panel_spki_sha256"),
                                bool(self.iqc.get("pin_accept_rotation")), mode=self.iqc.get("pin_mode") or "required",
                                owner=self.iqc.get("owner"))
        except ValueError as e:
            raise SystemExit(str(e))
        self.beacon = beacon.BeaconWriter(os.path.join(C.beacon_dir(), beacon.NAME))

    def on_violation(self) -> None:
        self.stats["violations"] += 1

    async def _beacon_loop(self) -> None:
        while True:
            try:
                self.beacon.write(bool(self.up))
            except OSError as e:
                log.warning("beacon write failed: %s", e)
            await asyncio.sleep(0.25)

    async def run(self) -> None:
        banner = self.pin.startup_banner()
        if banner:
            log.warning("\n%s", banner)
        bt = asyncio.create_task(self._beacon_loop())
        try:
            await super().run()
        finally:
            bt.cancel()

    # ---- session
    async def session(self) -> bool:
        id12, key, secure, ca = find_pki(self.iqc["pki_dir"], self.iqc.get("ca_file"))
        require_private_key(key, "IQ Remote pairing key")
        mac = fmt_mac(id12)
        ctx = tls_context(key, secure, ca, self.pin)
        plugin_ip = self.iqc.get("plugin_ip") or detect_ip(self.iqc["panel_ip"])
        client = aiomqtt.Client(hostname=str(self.iqc["panel_ip"]), port=int(self.iqc.get("panel_port") or 8883),
                                tls_context=ctx, tls_insecure=True, clean_session=True, keepalive=30,
                                identifier=self.remote_client_id)
        g = GuardedPanel(client, mac, self.on_violation)
        harden_paho(client, g)
        self.pin.last_mismatch = None
        self.pin.last_problem = None
        try:
            await asyncio.wait_for(client.__aenter__(), 15)
        except Exception as e:  # noqa: BLE001
            if self.pin.last_problem is not None:          # pin_mismatch | pin_unreadable | pin_missing
                self.stats["pin_mismatches"] = self.pin.mismatches
                self.stats["pin_refusals"] = self.stats.get("pin_refusals", 0) + 1
                self.down_reason = self.pin.last_problem
                raise (e if isinstance(e, PinMismatch) else PinMismatch(str(e))) from e
            raise
        try:
            self.guard = g
            await g.subscribe_allowed()
            self._loading, self._buffer = True, []
            listener = asyncio.create_task(self._listen(client, g))
            body = asyncio.create_task(self._body(g, mac, plugin_ip))
            done, pending = await asyncio.wait({listener, body}, return_when=asyncio.FIRST_COMPLETED)
            for p in pending:
                p.cancel()
            for d in done:
                d.result()  # raise
            return self.up
        finally:
            try:
                await asyncio.wait_for(client.__aexit__(None, None, None), 3)
            except Exception:  # noqa: BLE001
                pass

    async def _listen(self, client, g: GuardedPanel) -> None:
        async for m in client.messages:
            self.panel_message()
            try:
                obj = json.loads(m.payload.decode())
            except Exception:  # noqa: BLE001
                continue
            if not isinstance(obj, dict):
                continue
            topic = str(m.topic)
            try:
                await self.note_seen()
                if topic == g.resp_topic:
                    g.deliver_response(obj)
                elif topic == "iq2meid":
                    if self._loading:
                        if len(self._buffer) < 100000:
                            self._buffer.append(obj)
                    else:
                        await self.on_iq2meid(obj)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001  - one hostile/garbled message must not kill the listener
                log.exception("ignoring message that failed processing on %s", topic)

    @staticmethod
    def _batt() -> dict:
        return batt_fields()

    def build_connect(self, mac: str, plugin_ip: str) -> dict:
        return connect_payload(mac, plugin_ip, self.remote_client_id)

    @staticmethod
    def build_ping(mac: str, plugin_ip: str) -> dict:
        return ping_payload(mac, plugin_ip)

    async def _body(self, g: GuardedPanel, mac: str, plugin_ip: str) -> None:
        ht = HANDSHAKE_TIMEOUT_S
        await asyncio.sleep(SETTLE_S)             # library: sleep(2) before the session handshake
        await g.request("connect_v204", self.build_connect(mac, plugin_ip), ht)
        ping = self.build_ping(mac, plugin_ip)
        await g.request("pingevent", ping, ht)
        await g.request("pair_status_request", None, ht)
        await self._sync(g, ht)
        self.up = True
        self._loading = False
        self.last_panel_msg = time.monotonic()
        for obj in self._buffer:  # replay anything that arrived during load on top of the loaded model
            await self.on_iq2meid(obj, emit=False)
        self._buffer = []
        await self.emit_snapshot()
        next_lib_ping = time.monotonic() + LIB_PING_PERIOD_S
        while True:
            try:
                await asyncio.wait_for(self._resync.wait(), 0.5)
                self._resync.clear()
                await self._sync(g, ht)
                await self.emit_snapshot()
            except asyncio.TimeoutError:
                pass
            now = time.monotonic()
            if now >= next_lib_ping:                # unconditional keep-alive, like the library's ping task
                next_lib_ping = now + LIB_PING_PERIOD_S
                try:
                    await g.request("pingevent", ping, ht)
                except asyncio.TimeoutError:
                    self.stats["probe_failures"] += 1
                    raise ConnectionError("panel did not answer pingevent")
            elif now - self.last_panel_msg >= self.silence_probe_s:   # silence probe (same read-only ping)
                self.stats["probes"] += 1
                try:
                    await g.request("pingevent", ping, self.probe_timeout_s)
                except asyncio.TimeoutError:
                    self.stats["probe_failures"] += 1
                    raise ConnectionError("panel silent: probe timed out")

    async def _sync(self, g: GuardedPanel, timeout: float) -> None:
        r = await g.request("syncdatabase", None, timeout)
        self.apply_sync(r.get("fulldbdata") or [])

    def apply_sync(self, fulldb: list) -> None:
        """Replace the model from a syncdatabase answer. An answer WITHOUT the sensor table at all (as opposed to an
        empty one) is partial: keep the zones we hold and tell the normalizer so, instead of reporting them deleted."""
        zones, parts = build_model(fulldb, on_bad_zone=self._bad_zone_id)
        self.zones_complete = has_sensor_table(fulldb) or not self.zones
        if not self.zones_complete:
            self.stats["partial_syncs"] = self.stats.get("partial_syncs", 0) + 1
            zones = dict(self.zones)
        self.zones, self.parts = zones, parts

    def _bad_zone_id(self) -> None:
        """A zone id that is not a plain integer was seen: the row is dropped, counted (`bad_zone_ids`, metric
        blackrelay_adapter_bad_zone_ids_total) and one coalesced re-read (read-only syncdatabase) is requested."""
        self.stats["bad_zone_ids"] = self.stats.get("bad_zone_ids", 0) + 1
        self._resync.set()
        log.warning("%s: a zone row with a non-numeric zone id was rejected; resync requested", self.name)

    # ---- live updates
    async def on_iq2meid(self, d: dict, emit: bool = True) -> None:
        if d.get("eventName") != "dbChanged":
            return
        op, uri = d.get("dbOperation"), d.get("uri")
        cv = d.get("contentValues") or {}
        if isinstance(cv, str):
            try:
                cv = json.loads(cv)
            except Exception:  # noqa: BLE001
                cv = {}
        if uri == URI_SENSOR:
            if op in ("update", "insert"):
                if "zoneid" not in cv:
                    a = sel_args(d)
                    if a:
                        cv = {**cv, "zoneid": a[0]}
                try:
                    o = zone_obs(cv)
                except BadZoneId:
                    self._bad_zone_id()
                    return
                if not o:
                    if op == "insert":
                        # a new row we cannot place (no zoneid): do not guess and do not ignore it, re-read the
                        # database (read-only syncdatabase; the event is coalesced with any other pending resync)
                        self._resync.set()
                    return
                zid = o["panel_zone_id"]
                if op == "insert":
                    self.zones[zid] = o
                elif zid not in self.zones:
                    # an update for a zone we do not hold (raced a delete, or we missed the insert): do not invent it,
                    # re-read the database (read-only syncdatabase) and let the snapshot say what exists
                    self._resync.set()
                    return
                else:
                    cur = self.zones[zid]
                    raw = {**cur.get("raw", {}), **o.get("raw", {})}
                    cur.update(o)
                    cur["raw"] = raw
                if emit and self.up:
                    # the panel's update carries only the changed columns: emit the MERGED zone (name, battery,
                    # partition... carried forward), never the bare delta
                    z = {**self.zones[zid], "raw": dict(self.zones[zid].get("raw", {}))}
                    if op == "insert":
                        await self.emit(t="zone", zone=z, new=True)
                    else:
                        await self.emit(t="zone", zone=z)
            elif op == "delete":
                zid_s = cv.get("zoneid") or (sel_args(d) or [None])[0]
                if zid_s is None:
                    self._resync.set()  # ambiguous delete: re-read (read-only syncdatabase)
                    return
                try:
                    zid = _zid(zid_s)
                except BadZoneId:
                    self._bad_zone_id()
                    return
                self.zones.pop(zid, None)
                if emit and self.up:
                    await self.emit(t="zone_removed", panel_zone_id=zid)
        elif uri == URI_SETTINGS and cv.get("name") == "SYSTEM_STATUS":
            await self._part(_int(cv.get("partition_id")), {"status": cv.get("value")}, emit)
        elif uri == URI_STATE and cv.get("name") == "ALARM_STATE" and op in ("update", "insert"):
            await self._part(_int(cv.get("partition_id")), {"alarm_state": cv.get("value")}, emit)
        elif uri == URI_ALARMED:
            pid = _int(cv.get("partition_id"))
            if op == "insert" and cv.get("sgroup"):
                await self._part(pid, {"alarm_type": cv["sgroup"]}, emit)
            elif op == "delete":
                await self._part(pid, {"alarm_type": None}, emit)
        elif uri == URI_PARTITION and op in ("update", "insert") and cv.get("name"):
            await self._part(_int(cv.get("partition_id")), {"name": cv["name"]}, emit)

    async def _part(self, pid: int, delta: dict, emit: bool) -> None:
        self.parts.setdefault(pid, {"partition_id": pid}).update(delta)
        if emit and self.up:
            await self.emit(t="partition", partition={"partition_id": pid, **delta})
