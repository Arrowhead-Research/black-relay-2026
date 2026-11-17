"""Control4 adapter (fallback): JSON-over-TLS, certificate pinned by SHA-256, strictly read-only.

Outbound guard: the only bytes this process ever writes to the panel are produced by `build_info_summary()`
via GuardedC4Writer.send(); any other action (ARMING, ALARM, INFO/SECURE_ARM, ...) raises OneWayViolation
before reaching the socket. The adapter is a warm standby: it stays connected and keeps its own shadow
picture fresh; the normalizer decides when that picture becomes the published one.
"""
from __future__ import annotations

import asyncio
import codecs
import hashlib
import json
import logging
import os
import re
import ssl
import time
import uuid
from typing import Any

from . import beacon
from .. import config as C
from .base import AdapterBase, OneWayViolation

log = logging.getLogger("br2.control4")


def norm_fp(s: str | None) -> str | None:
    if not s:
        return None
    return re.sub(r"[^0-9a-f]", "", str(s).lower()) or None


def build_info_summary(token: str) -> dict:
    return {"action": "INFO", "info_type": "SUMMARY", "token": token, "nonce": "qolsys", "source": "C4",
            "version": 0}


MAX_C4_BUF = 1 << 20          # 1 MiB without a complete object => something is wrong; resync instead of growing


class C4Framer:
    """Incremental splitter for the panel's stream of concatenated JSON objects that survives garbage.

    * bytes before the first '{' are discarded;
    * an object is delimited by brace matching that is aware of strings/escapes, then parsed with json.loads;
    * a delimited candidate that does not parse (corruption inside it) costs ONE character: the scan restarts at
      the next '{', so valid objects nested inside / following a damaged one are still found;
    * a candidate that never closes and exceeds MAX_C4_BUF is abandoned the same way (no unbounded buffer, no
      permanent wedge behind a stray '{').
    Counters: .dropped_bytes / .resyncs for diagnostics."""

    def __init__(self, max_buf: int = MAX_C4_BUF):
        self.buf = ""
        self.max_buf = max_buf
        self.dropped_bytes = 0
        self.resyncs = 0

    def feed(self, text: str) -> list[dict]:
        self.buf += text
        out: list[dict] = []
        while True:
            i = self.buf.find("{")
            if i < 0:
                self.dropped_bytes += len(self.buf)
                self.buf = ""
                return out
            if i:
                self.dropped_bytes += i
                self.resyncs += 1
                self.buf = self.buf[i:]
            end = self._match(self.buf)
            if end is None:
                if len(self.buf) > self.max_buf:
                    self.resyncs += 1
                    self.dropped_bytes += 1
                    self.buf = self.buf[1:]
                    continue
                return out                       # incomplete: wait for more bytes
            try:
                obj = json.loads(self.buf[:end])
            except ValueError:
                self.resyncs += 1
                self.dropped_bytes += 1
                self.buf = self.buf[1:]
                continue
            self.buf = self.buf[end:]
            if isinstance(obj, dict):
                out.append(obj)

    @staticmethod
    def _match(b: str) -> int | None:
        depth, in_str, esc = 0, False, False
        for n, ch in enumerate(b):
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return n + 1
        return None


C4_KEYS = frozenset(("action", "info_type", "token", "nonce", "source", "version"))
C4_MAX_TOKEN = 1024


def _no_dup_keys(pairs: list) -> dict:
    out: dict = {}
    for k, v in pairs:
        if k in out:
            raise ValueError(f"duplicate JSON key {k!r}")
        out[k] = v
    return out


class GuardedC4Writer:
    """Sole writer to the Control4 socket. The ONLY frame it emits is the INFO/SUMMARY request: it is serialized once,
    the serialized text is parsed back and checked for an exact key set and exact value types (as the IQ guard does for
    its envelopes), and exactly those checked bytes are written. Extra keys, nested objects, other values, duplicate
    keys: OneWayViolation before anything reaches the socket."""

    def __init__(self, writer: asyncio.StreamWriter, on_violation=None):
        self._w = writer
        self._on_violation = on_violation or (lambda: None)
        self.sent: list[str] = []

    def _violation(self, why: str) -> None:
        self._on_violation()
        log.error("ONE-WAY GUARD blocked control4 write: %s", why)
        raise OneWayViolation(f"control4 write not allowed: {why}")

    def check(self, msg: Any) -> str:
        """Validate `msg`; returns the exact text that may be written (one line, newline-terminated)."""
        try:
            text = json.dumps(msg, allow_nan=False)
            obj = json.loads(text, object_pairs_hook=_no_dup_keys)
        except (TypeError, ValueError) as e:
            self._violation(f"not a plain JSON object ({type(e).__name__})")
        if not isinstance(obj, dict):
            self._violation("payload is not an object")
        if set(obj) != C4_KEYS:
            self._violation(f"key set differs from the allow-list (extra={sorted(set(obj) - C4_KEYS)} "
                            f"missing={sorted(C4_KEYS - set(obj))})")
        if obj["action"] != "INFO" or obj["info_type"] != "SUMMARY":
            self._violation(f"action/info_type {obj['action']!r}/{obj['info_type']!r}")
        tok = obj["token"]
        if not isinstance(tok, str) or not tok or len(tok) > C4_MAX_TOKEN or not tok.isprintable():
            self._violation("token must be a short printable string")
        if obj["nonce"] != "qolsys" or obj["source"] != "C4":
            self._violation("nonce/source differ from the fixed values")
        if type(obj["version"]) is not int or obj["version"] != 0:
            self._violation("version must be the integer 0")
        return text + "\n"

    async def send(self, msg: dict) -> None:
        line = self.check(msg)
        self._w.write(line.encode())
        await self._w.drain()
        self.sent.append("INFO/SUMMARY")


def zone_from_c4(z: dict, partition_id: Any = None) -> dict | None:
    zid = z.get("zone_id", z.get("id"))
    try:
        zid = int(str(zid).strip())
    except (TypeError, ValueError):
        return None
    o: dict[str, Any] = {"panel_zone_id": zid}
    for s, d in (("name", "name"), ("type", "type"), ("group", "group"), ("status", "status")):
        if s in z:
            o[d] = z[s]
    pid = z.get("partition_id", partition_id)
    if pid is not None:
        try:
            o["partition_id"] = int(pid)
        except (TypeError, ValueError):
            pass
    o["raw"] = {k: z[k] for k in ("type", "group", "status", "state", "zone_type", "zone_physical_type",
                                   "zone_alarm_type") if k in z}
    return o


def parse_summary(m: dict) -> tuple[dict[int, dict], dict[int, dict]]:
    zones, parts, _ = parse_summary_ex(m, None)
    return zones, parts


def parse_summary_ex(m: dict, prev_zones: dict[int, dict] | None) -> tuple[dict[int, dict], dict[int, dict], bool]:
    """Like parse_summary, plus completeness. A partition that comes WITHOUT a zone_list (key absent / not a list) says
    nothing about its zones: the ones we already hold for that partition are carried over and the summary is reported
    incomplete, instead of being read as "this partition has no zones"."""
    zones: dict[int, dict] = {}
    parts: dict[int, dict] = {}
    complete = isinstance(m.get("partition_list"), list)
    carry: set[int] = set()
    for p in m.get("partition_list") or []:
        try:
            pid = int(p.get("partition_id"))
        except (TypeError, ValueError):
            continue
        parts[pid] = {"partition_id": pid, "name": p.get("name") or f"Partition {pid}", "status": p.get("status")}
        if not isinstance(p.get("zone_list"), list):
            carry.add(pid)
            complete = False
            continue
        for z in p.get("zone_list") or []:
            o = zone_from_c4(z, pid)
            if o:
                zones[o["panel_zone_id"]] = o
    if not complete and prev_zones:
        whole = not isinstance(m.get("partition_list"), list)
        for zid, z in prev_zones.items():
            if zid not in zones and (whole or z.get("partition_id") in carry):
                zones[zid] = z
    return zones, parts, complete


class Control4Adapter(AdapterBase):
    name = "control4"

    def __init__(self, cfg, ipc_dir=None):
        super().__init__(cfg, ipc_dir)
        self.c = cfg.c4
        self.w: GuardedC4Writer | None = None
        self.fallback_after = float(cfg.timers["fallback_after_s"])
        self.holddown = float(cfg.timers.get("failback_holddown_s", 3))
        self.gated = bool(cfg.iq_enabled)           # sole-source mode (iq disabled): no gate
        self.beacon_path = os.path.join(C.beacon_dir(), beacon.NAME)
        self.beacon = beacon.BeaconReader(self.beacon_path)       # read-only consumer (volume is mounted ro)
        self.zones_complete = True
        self._summary_due = False
        self._last_summary_req = 0.0

    def gate_open(self) -> bool:
        """Cold standby: the panel is contacted only once the primary has been down for fallback_after_s
        (measured on OUR monotonic clock from what we observe in the beacon; see beacon.py)."""
        if not self.gated:
            return True
        self.beacon.poll()
        return self.beacon.primary_down_for() >= self.fallback_after

    def gate_should_close(self) -> bool:
        """Primary has been healthy for a few seconds: release the fallback panel connection."""
        if not self.gated:
            return False
        self.beacon.poll()
        return self.beacon.primary_up_for() >= self.holddown

    def on_violation(self) -> None:
        self.stats["violations"] += 1

    def _token(self) -> str:
        return open(self.c["token_file"]).read().strip()

    async def session(self) -> bool:
        while not self.gate_open():
            await asyncio.sleep(0.25)
        pin = norm_fp(self.c.get("cert_sha256"))
        if not pin:
            log.error("control4.cert_sha256 not set: refusing to connect")
            await asyncio.sleep(max(self.backoff_max, 1))
            raise ConnectionRefusedError("no pinned fingerprint")
        token = self._token()
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE   # trust is established ONLY by the SHA-256 pin below
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        try:
            ctx.set_ciphers("DEFAULT:@SECLEVEL=0")
        except ssl.SSLError:
            pass
        r, w = await asyncio.wait_for(asyncio.open_connection(self.c["host"], int(self.c.get("port") or 12345),
                                                              ssl=ctx), 15)
        try:
            der = w.get_extra_info("ssl_object").getpeercert(binary_form=True)
            fp = hashlib.sha256(der).hexdigest() if der else ""
            if fp != pin:
                raise ssl.SSLError(f"control4 certificate fingerprint mismatch ({fp[:12]}...)")
            self.w = GuardedC4Writer(w, self.on_violation)
            self.up = False
            return await self._run(r, token)
        finally:
            w.close()
            try:
                await asyncio.wait_for(w.wait_closed(), 2)
            except Exception:  # noqa: BLE001
                pass

    async def _run(self, r: asyncio.StreamReader, token: str) -> bool:
        assert self.w
        framer = C4Framer()
        await self.w.send(build_info_summary(token))
        pending_probe: float | None = time.monotonic()  # initial SUMMARY request is also our first probe
        self.last_panel_msg = time.monotonic()
        probe_at = self.silence_probe_s
        dec = codecs.getincrementaldecoder("utf-8")("replace")
        while True:
            try:
                chunk = await asyncio.wait_for(r.read(65536), 0.5)
                if not chunk:
                    raise ConnectionError("control4 peer closed")
                self.panel_message()
                for obj in framer.feed(dec.decode(chunk)):
                    if await self._on_event(obj):
                        pending_probe = None
                await self.note_seen()
            except asyncio.TimeoutError:
                pass
            if self.up and self.gate_should_close():
                log.info("primary healthy again: releasing control4 connection")
                return True
            now = time.monotonic()
            if self._summary_due and pending_probe is None and now - self._last_summary_req >= 5.0:
                self._summary_due, self._last_summary_req = False, now
                await self.w.send(build_info_summary(token))
                pending_probe = now
            if pending_probe is not None and now - pending_probe > self.probe_timeout_s:
                self.stats["probe_failures"] += 1
                raise ConnectionError("control4 probe timed out")
            if pending_probe is None and now - self.last_panel_msg >= probe_at:
                self.stats["probes"] += 1
                await self.w.send(build_info_summary(token))
                pending_probe = now

    async def _on_event(self, m: dict) -> bool:
        """Returns True when the message proves liveness of a probe (SUMMARY)."""
        ev = m.get("event")
        if ev == "INFO" and m.get("info_type") == "SUMMARY":
            self.zones, self.parts, self.zones_complete = parse_summary_ex(m, self.zones)
            self.up = True
            await self.emit_snapshot()
            return True
        if not self.up:
            return False
        if ev == "ZONE_EVENT":
            t = str(m.get("zone_event_type", "")).strip()
            z = m.get("zone") or {}
            o = zone_from_c4(z)
            if not o:
                return False
            zid = o["panel_zone_id"]
            cur = self.zones.get(zid)
            if cur is None:
                # an event for a zone our SUMMARY does not contain: never invent it; ask for a fresh SUMMARY
                # (the same read-only request as the probe) and let it say what exists
                self._summary_due = True
                return False
            raw = {**cur.get("raw", {}), **o.get("raw", {})}
            cur.update(o)
            cur["raw"] = raw
            await self.emit(t="zone", zone={**o, "raw": raw})
        elif ev == "ARMING":
            await self._part(m.get("partition_id"), {"status": m.get("arming_type"), "alarm_type": None})
        elif ev == "ALARM":
            at = (m.get("alarm_type") or "").strip()
            await self._part(m.get("partition_id"), {"status": "ALARM", "alarm_type": at.lower() or None})
        return False

    async def _part(self, pid: Any, delta: dict) -> None:
        try:
            pid = int(pid)
        except (TypeError, ValueError):
            return
        self.parts.setdefault(pid, {"partition_id": pid}).update(delta)
        await self.emit(t="partition", partition={"partition_id": pid, **delta})
