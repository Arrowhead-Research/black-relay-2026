"""Field-verification commands: `br2 snapshot` and `br2 watch`, plus the read-only panel session they (and the
post-pairing check of `br2 pair`) share.

Every byte this module sends to the panel goes through iq_remote.GuardedPanel, the same allow-list the adapter uses
(connect_v204, pingevent, pair_status_request, syncdatabase; exact key sets). It has no other write path, never
prints or logs payloads from our side, and never touches key material beyond handing the file paths to ssl.
"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
import ssl
import time
from datetime import datetime, timezone
from typing import Any, Callable

import aiomqtt

from .adapters import iq_remote as IQ
from .keyperm import require_private_key, KeyPermissionError
from .pin import PanelPin, PinMismatch, PinUnreadable

log = logging.getLogger("br2.field")

HANDSHAKE_TIMEOUT_S = 30.0


class FieldError(Exception):
    """A problem the operator can act on; printed without a traceback."""


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def iq_settings(cfg, panel_ip: str | None = None, pki_dir: str | None = None, plugin_ip: str | None = None,
                port: int | None = None, ca_file: str | None = None) -> dict:
    """Connection settings from config with command-line overrides applied."""
    iq = cfg.iq
    out = {"panel_ip": panel_ip or iq.get("panel_ip"), "port": int(port or iq.get("panel_port") or 8883),
           "pki_dir": pki_dir or iq.get("pki_dir"), "plugin_ip": plugin_ip or iq.get("plugin_ip"),
           "ca_file": ca_file or iq.get("ca_file"), "spki_preset": iq.get("panel_spki_sha256"),
           "pin_accept_rotation": bool(iq.get("pin_accept_rotation")),
           "pin_mode": iq.get("pin_mode") or "required", "owner": iq.get("owner")}
    if not out["panel_ip"]:
        raise FieldError("no panel address: set iq_remote.panel_ip in the config or pass --panel-ip")
    if not out["pki_dir"]:
        raise FieldError("no pki dir: set iq_remote.pki_dir in the config or pass --pki-dir")
    return out


class PanelSession:
    """One read-only IQ Remote session: TLS+MQTT connect, the four allow-listed handshake messages, full database
    sync, then (optionally) live `iq2meid` events handed to `on_event`. Use as `async with`."""

    def __init__(self, settings: dict, on_event: Callable[[dict], Any] | None = None):
        self.s = settings
        self.on_event = on_event
        self.guard: IQ.GuardedPanel | None = None
        self.client: aiomqtt.Client | None = None
        self.tls: dict = {}
        self.zones: dict[int, dict] = {}
        self.parts: dict[int, dict] = {}
        self.mac = ""
        self.plugin_ip = ""
        self._ping: dict = {}
        self._listener: asyncio.Task | None = None
        self._loading = True
        self._buffer: list[dict] = []

    async def __aenter__(self) -> "PanelSession":
        s = self.s
        try:
            id12, key, secure, ca = IQ.find_pki(s["pki_dir"], s.get("ca_file"))
        except FileNotFoundError as e:
            raise FieldError(f"no usable pairing material in {s['pki_dir']} ({e.args[0] if e.args else e}); "
                             "run `br2 pair` first") from e
        try:
            require_private_key(key, "IQ Remote pairing key")
        except KeyPermissionError as e:
            raise FieldError(str(e)) from e
        self.mac = IQ.fmt_mac(id12)
        try:
            self.pin = PanelPin(s["pki_dir"], s.get("spki_preset"), bool(s.get("pin_accept_rotation")),
                                source=s.get("pin_source", "snapshot"), mode=s.get("pin_mode") or "required",
                                owner=s.get("owner"))
        except ValueError as e:
            raise FieldError(str(e)) from e
        ctx = IQ.tls_context(key, secure, ca, self.pin)
        self.plugin_ip = s.get("plugin_ip") or IQ.detect_ip(s["panel_ip"])
        remote_id = "qolsys-controller-" + secrets.token_hex(6)
        self.client = aiomqtt.Client(hostname=str(s["panel_ip"]), port=int(s["port"]), tls_context=ctx,
                                     tls_insecure=True, clean_session=True, keepalive=30, identifier=remote_id)
        self.guard = IQ.GuardedPanel(self.client, self.mac)
        IQ.harden_paho(self.client, self.guard)
        self.pin.last_mismatch = None
        self.pin.last_problem = None
        try:
            await asyncio.wait_for(self.client.__aenter__(), 15)
        except Exception as e:  # noqa: BLE001
            if self.pin.last_problem in ("pin_unreadable", "pin_missing"):
                raise FieldError(
                    ("PANEL PIN UNREADABLE: the pin file exists but cannot be read and validated (see the log line above"
                     "). Nothing was sent. Fix its owner/mode, or verify the panel and re-record it with `br2 repin --yes`."
                     if self.pin.last_problem == "pin_unreadable" else
                     "NO PANEL PIN RECORDED (iq_remote.pin_mode is 'required'). Nothing was sent. Run `br2 pair` (it records "
                     "the pin while you are at the panel) or `br2 repin --yes`, then retry.")) from e
            if self.pin.last_mismatch is not None:
                m = self.pin.last_mismatch
                raise FieldError(
                    "PANEL CERTIFICATE PIN MISMATCH: the certificate at the panel address is not the one pinned "
                    f"(presented {m['presented']}, pinned {m['expected']}). Nothing was sent. If the panel's "
                    "certificate was changed on purpose, run `br2 repin --yes`; otherwise treat this as an "
                    "impostor on your network.") from e
            if not isinstance(e, (aiomqtt.MqttError, OSError, asyncio.TimeoutError)):
                raise
            raise FieldError(f"cannot connect to the panel at {s['panel_ip']}:{s['port']}: {e!r}. Check the address, "
                             "that the panel is on the LAN and that outbound TCP 8883 is allowed") from e
        self.tls = IQ.tls_info(self.client)
        try:
            await self.guard.subscribe_allowed()
            self._listener = asyncio.create_task(self._listen())
            await asyncio.sleep(IQ.SETTLE_S)
            g = self.guard
            self._ping = IQ.ping_payload(self.mac, self.plugin_ip)
            await g.request("connect_v204", IQ.connect_payload(self.mac, self.plugin_ip, remote_id), HANDSHAKE_TIMEOUT_S)
            await g.request("pingevent", self._ping, HANDSHAKE_TIMEOUT_S)
            await g.request("pair_status_request", None, HANDSHAKE_TIMEOUT_S)
            await self.sync()
            self._loading = False
            for obj in self._buffer:
                await self._dispatch(obj)
            self._buffer = []
        except asyncio.TimeoutError as e:
            await self.__aexit__(None, None, None)
            raise FieldError("the panel did not answer within 30 s (is this client paired? "
                             "a panel that does not know our certificate stays silent)") from e
        except BaseException:
            await self.__aexit__(None, None, None)
            raise
        return self

    async def __aexit__(self, *exc) -> None:
        if self._listener:
            self._listener.cancel()
            await asyncio.gather(self._listener, return_exceptions=True)
            self._listener = None
        if self.client is not None:
            try:
                await asyncio.wait_for(self.client.__aexit__(None, None, None), 3)
            except Exception:  # noqa: BLE001
                pass

    async def sync(self) -> None:
        r = await self.guard.request("syncdatabase", None, HANDSHAKE_TIMEOUT_S)
        self.zones, self.parts = IQ.build_model(r.get("fulldbdata") or [])

    async def ping(self) -> None:
        await self.guard.request("pingevent", self._ping, HANDSHAKE_TIMEOUT_S)

    async def _listen(self) -> None:
        async for m in self.client.messages:
            try:
                obj = json.loads(m.payload.decode())
            except Exception:  # noqa: BLE001
                continue
            if not isinstance(obj, dict):
                continue
            topic = str(m.topic)
            if topic == self.guard.resp_topic:
                self.guard.deliver_response(obj)
            elif topic == "iq2meid":
                if self._loading:
                    if len(self._buffer) < 100000:
                        self._buffer.append(obj)
                else:
                    await self._dispatch(obj)

    async def _dispatch(self, obj: dict) -> None:
        if self.on_event is not None:
            res = self.on_event(obj)
            if asyncio.iscoroutine(res):
                await res


# ------------------------------------------------------------------ snapshot
def zone_row(o: dict) -> dict:
    return {"id": o.get("panel_zone_id"), "name": o.get("name"), "sensor_type": o.get("type"),
            "sensor_group": o.get("group"), "status": o.get("status"), "battery_status": o.get("battery"),
            "partition_id": o.get("partition_id"), "raw": o.get("raw", {})}


def build_snapshot(zones: dict[int, dict], parts: dict[int, dict], tls: dict, panel_ip: str) -> dict:
    return {"captured_at": utc_now(), "panel": panel_ip, "tls": tls,
            "partitions": [parts[k] for k in sorted(parts)],
            "zones": [zone_row(zones[k]) for k in sorted(zones)]}


def tls_line(tls: dict) -> str:
    return f"TLS to the panel: version={tls.get('version')} cipher={tls.get('cipher')} bits={tls.get('secret_bits')}"


async def run_snapshot(settings: dict, out=None, err=None) -> int:
    import sys
    out, err = out or sys.stdout, err or sys.stderr
    print(f"Connecting to panel {settings['panel_ip']}:{settings['port']} ...", file=err, flush=True)
    async with PanelSession(settings) as sess:
        print(tls_line(sess.tls), file=err, flush=True)
        snap = build_snapshot(sess.zones, sess.parts, sess.tls, str(settings["panel_ip"]))
    print(json.dumps(snap, indent=2, ensure_ascii=False), file=out)
    print(f"{len(snap['partitions'])} partition(s), {len(snap['zones'])} zone(s).", file=err)
    return 0


# ------------------------------------------------------------------ watch
class WatchModel(IQ.IQRemoteAdapter):
    """Reuses the adapter's database-event parser, so what `watch` prints is exactly what the bridge would see.
    No adapter machinery (IPC, supervision) is started: __init__ of the base classes is deliberately not called."""

    def __init__(self, zones: dict, parts: dict, sink: Callable[[dict], None]):  # noqa: super().__init__ intentionally skipped
        self.zones, self.parts, self.up = zones, parts, True
        self._resync = asyncio.Event()
        self._sink = sink

    async def emit(self, **m) -> None:
        self._sink(m)


def _event_line(m: dict) -> dict:
    t = m.get("t")
    if t == "zone":
        z = m["zone"]
        return {"ts": utc_now(), "event": "zone", "id": z.get("panel_zone_id"), "name": z.get("name"),
                "status": z.get("status"), "battery": z.get("battery"), "partition_id": z.get("partition_id")}
    if t == "zone_removed":
        return {"ts": utc_now(), "event": "zone_removed", "id": m.get("panel_zone_id")}
    if t == "partition":
        return {"ts": utc_now(), "event": "partition", **m["partition"]}
    return {"ts": utc_now(), "event": str(t)}


async def run_watch(settings: dict, minutes: float, out=None, err=None) -> int:
    import sys
    out, err = out or sys.stdout, err or sys.stderr
    if minutes <= 0:
        raise FieldError("--minutes must be greater than 0")
    print(f"Connecting to panel {settings['panel_ip']}:{settings['port']} ...", file=err, flush=True)
    count = 0
    seen: dict[int, tuple] = {}

    def sink(m: dict) -> None:
        nonlocal count
        line = _event_line(m)
        if line["event"] == "zone":                      # print only real changes
            key = (line["status"], line["battery"], line["name"], line["partition_id"])
            if seen.get(line["id"]) == key:
                return
            seen[line["id"]] = key
        count += 1
        print(json.dumps(line, ensure_ascii=False), file=out, flush=True)

    model: WatchModel | None = None

    async def on_event(obj: dict) -> None:
        if model is not None:
            await model.on_iq2meid(obj)

    async with PanelSession(settings, on_event=on_event) as sess:
        for zid, o in sess.zones.items():
            seen[zid] = (o.get("status"), o.get("battery"), o.get("name"), o.get("partition_id"))
        model = WatchModel(sess.zones, sess.parts, sink)
        print(tls_line(sess.tls), file=err, flush=True)
        print(f"Connected: {len(sess.zones)} zone(s). Printing changes for {minutes:g} minute(s); Ctrl-C to stop early.",
              file=err, flush=True)
        end = time.monotonic() + minutes * 60
        next_ping = time.monotonic() + IQ.LIB_PING_PERIOD_S
        while time.monotonic() < end:
            try:
                await asyncio.wait_for(model._resync.wait(), min(1.0, max(0.05, end - time.monotonic())))
                model._resync.clear()
                before = {k: dict(v) for k, v in sess.zones.items()}
                await sess.sync()
                model.zones, model.parts = sess.zones, sess.parts
                for zid in sorted(set(before) | set(sess.zones)):
                    if zid not in sess.zones:
                        sink({"t": "zone_removed", "panel_zone_id": zid})
                    elif before.get(zid, {}).get("status") != sess.zones[zid].get("status"):
                        sink({"t": "zone", "zone": sess.zones[zid]})
            except asyncio.TimeoutError:
                pass
            if time.monotonic() >= next_ping:
                next_ping = time.monotonic() + IQ.LIB_PING_PERIOD_S
                await sess.ping()
    print(f"Stopped. {count} change(s) printed.", file=err)
    return 0


# ------------------------------------------------------------------ repin
async def fetch_panel_cert_der(settings: dict, timeout: float = 15.0) -> bytes:
    """TLS handshake with the panel (CA-verified, our client certificate presented) and return its leaf certificate.
    NO application bytes are sent: this is how `br2 repin` learns the new certificate."""
    try:
        id12, key, secure, ca = IQ.find_pki(settings["pki_dir"], settings.get("ca_file"))
    except FileNotFoundError as e:
        raise FieldError(f"no usable pairing material in {settings['pki_dir']} ({e.args[0] if e.args else e}); "
                         "run `br2 pair` first") from e
    try:
        require_private_key(key, "IQ Remote pairing key")
    except KeyPermissionError as e:
        raise FieldError(str(e)) from e
    ctx = IQ.tls_context(key, secure, ca)                      # no pin: that is the point
    try:
        r, w = await asyncio.wait_for(asyncio.open_connection(str(settings["panel_ip"]), int(settings["port"]), ssl=ctx),
                                      timeout)
    except Exception as e:  # noqa: BLE001
        raise FieldError(f"cannot complete a TLS handshake with the panel at {settings['panel_ip']}:{settings['port']}: "
                         f"{e!r} (the certificate must still chain to the panel CA)") from e
    try:
        der = w.get_extra_info("ssl_object").getpeercert(binary_form=True)
    finally:
        w.close()
    if not der:
        raise FieldError("the panel presented no certificate")
    return der


async def run_repin(settings: dict, yes: bool = False, out=None, err=None) -> int:
    """Deliberately accept the panel's current certificate as the pin. Prints old and new fingerprints; changes
    nothing unless `yes`."""
    import sys
    from .pin import fmt, spki_sha256
    out, err = out or sys.stdout, err or sys.stderr
    pin = PanelPin(settings["pki_dir"], settings.get("spki_preset"), owner=settings.get("owner"))
    der = await fetch_panel_cert_der(settings)
    new = spki_sha256(der)
    try:
        old = pin.stored()
        old_txt = fmt(old)
    except PinUnreadable as e:                  # a damaged pin file is exactly what repin is for
        old, old_txt = None, f"(UNREADABLE: {e})"
    print(f"panel {settings['panel_ip']}:{settings['port']}", file=err)
    print(f"  old pin (file): {old_txt}", file=err)
    if pin.preset:
        print(f"  config preset : {fmt(pin.preset)}  (iq_remote.panel_spki_sha256)", file=err)
    print(f"  new (presented): {fmt(new)}", file=err)
    if old == new and not pin.preset:
        print("The pin already matches the certificate the panel presents. Nothing to do.", file=err)
        return 0
    if not yes:
        print("Nothing changed. Only accept a new certificate if you know why the panel's certificate changed "
              "(a factory reset or a firmware update); an unexpected change can be an impostor on your LAN. "
              "Re-run with --yes to record the new pin.", file=err)
        return 2
    if not pin.persist(new, "repin"):
        raise FieldError(f"cannot write {pin.path}: is the pki dir read-only? (run this on the host)")
    print(f"Pinned {pin.path} (mode 0600).", file=err)
    if pin.preset and pin.preset != new:
        print("WARNING: config iq_remote.panel_spki_sha256 is set and takes precedence over the file: the bridge will "
              "keep refusing the panel until you update that value to the new fingerprint above.", file=err)
        return 4
    print("Restart the iq-remote service so it reconnects.", file=err)
    return 0
