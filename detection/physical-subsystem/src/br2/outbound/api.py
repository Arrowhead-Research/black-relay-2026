"""REST + SSE API (HTTPS) and Prometheus metrics. Read-only: only GET/HEAD routes exist and a middleware
rejects every other method with 405 before routing."""
from __future__ import annotations

import asyncio
import collections
import hashlib
import hmac
import json
import logging
import ssl
import time

from aiohttp import web
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from .state import OutState
from ..normalizer.store import Reader

log = logging.getLogger("br2.api")
import os
HB_SECONDS = float(os.environ.get("BR2_SSE_HEARTBEAT_S", "2.0"))   # short: lets half-closed clients notice dead streams quickly
KEY_NAME = web.RequestKey("key_name", str)
MAX_SSE = 256            # total open streams (api.sse_max_total)
MAX_SSE_PER_KEY = 64      # per API key, so one key holder cannot exhaust the pool (api.sse_max_per_key)
SSE_WRITE_TIMEOUT_S = 5.0
SSE_IDLE_DROP_S = float(os.environ.get("BR2_SSE_IDLE_DROP_S", "30"))   # api.sse_idle_drop_s: no successful delivery for N s => drop
MAX_SEQ = 2 ** 63 - 1          # SQLite INTEGER range: anything beyond it is not a valid cursor


def _bound_send_buffers(transport) -> None:
    """Small kernel send buffer + low asyncio high-water mark for an SSE stream, so a peer that stops reading is
    noticed after ~tens of KB instead of megabytes."""
    if transport is None:
        return
    try:
        sock = transport.get_extra_info("socket")
        if sock is not None:
            import socket as _s
            sock.setsockopt(_s.SOL_SOCKET, _s.SO_SNDBUF, SSE_SNDBUF)
        transport.set_write_buffer_limits(high=SSE_SNDBUF, low=SSE_SNDBUF // 4)
    except Exception:  # noqa: BLE001
        pass


def _unflushed(transport) -> int:
    """Bytes accepted by us but not yet acknowledged by the peer (asyncio buffer + kernel send queue)."""
    if transport is None:
        return 0
    n = 0
    try:
        n += transport.get_write_buffer_size()
    except Exception:  # noqa: BLE001
        pass
    try:
        sock = transport.get_extra_info("socket")
        if sock is not None:
            import fcntl, struct, termios
            n += struct.unpack("i", fcntl.ioctl(sock.fileno(), termios.TIOCOUTQ, b"\0\0\0\0"))[0]
    except Exception:  # noqa: BLE001
        pass
    return n


MAX_FAIL_IPS = 4096                 # LRU bound of the per-IP auth-failure table
MAX_PARALLEL_VERIFY = 2             # at most this many argon2 verifications running at once
SSE_PROBE_BYTES = int(os.environ.get("BR2_SSE_PROBE_BYTES", "32768"))     # padding comment sent every probe interval
SSE_PROBE_INTERVAL_S = float(os.environ.get("BR2_SSE_PROBE_INTERVAL_S", "30"))
SSE_SNDBUF = 16384


KEY_ID_LABEL = b"br2-api-key-id-v1"


def key_id(presented: str) -> str:
    """Fast, non-secret lookup handle of an API key: HMAC-SHA256 (fixed label as the HMAC key) of the key, first 16 hex
    digits. It is stored next to the argon2 hash (`hash-key` prints both) so that authentication can find the ONE
    record a presented key could belong to without running argon2 over every record. A key that matches no key_id is
    rejected without any argon2 work."""
    return hmac.new(KEY_ID_LABEL, presented.encode(), hashlib.sha256).hexdigest()[:16]


class KeyStore:
    """API keys. A record is {name, argon2_hash, key_id}; `key_id` is REQUIRED on every record (a record without it
    makes the constructor raise: re-run `hash-key`). A presented key is argon2-verified only against the record(s)
    with its key_id, so valid keys never queue behind garbage and garbage costs nothing. The per-IP failure limiter
    applies only to FAILURES (a key_id that matches but does not verify)."""

    def __init__(self, keys: list[dict], rate_per_min: int, max_fail_ips: int = MAX_FAIL_IPS):
        self.keys = [(k.get("name", "key"), k.get("argon2_hash", "")) for k in keys or []]
        self.by_id: dict[str, list[tuple[str, str]]] = {}
        for k in keys or []:
            rec = (k.get("name", "key"), k.get("argon2_hash", ""))
            kid = str(k.get("key_id") or "").strip().lower()
            if not kid:
                raise ValueError(f"api key {rec[0]!r} has no key_id: refusing to start. Re-run `hash-key` "
                                 "(docker compose run --rm bridge hash-key) and put BOTH key_id and argon2_hash "
                                 "of every key under api.keys in config.yaml")
            self.by_id.setdefault(kid, []).append(rec)
        self.bad_keys = 0
        self.ph = PasswordHasher()
        self.ok: dict[str, str] = {}           # sha256(presented) -> name (only after an argon2 success)
        self.rate = rate_per_min
        self.hits: dict[str, collections.deque] = {}
        self.fails: "collections.OrderedDict[str, collections.deque]" = collections.OrderedDict()
        self.max_fail_ips = max_fail_ips
        self._pending: dict[str, asyncio.Future] = {}
        self._sem: asyncio.Semaphore | None = None

    def _verify_sync(self, presented: str, records: list[tuple[str, str]]) -> str | None:
        for name, h in records:
            try:
                if self.ph.verify(h, presented):
                    return name
            except (VerificationError, InvalidHashError):
                continue
            except Exception:  # noqa: BLE001
                continue
        return None

    def _fail_row(self, ip: str, now: float) -> collections.deque:
        f = self.fails.get(ip)
        if f is None:
            f = self.fails[ip] = collections.deque()
            while len(self.fails) > self.max_fail_ips:
                self.fails.popitem(last=False)              # LRU: the least recently seen address goes
        else:
            self.fails.move_to_end(ip)
        while f and now - f[0] > 60:
            f.popleft()
        return f

    async def _argon(self, presented: str, records: list[tuple[str, str]]) -> str | None:
        if self._sem is None:
            self._sem = asyncio.Semaphore(MAX_PARALLEL_VERIFY)
        async with self._sem:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, self._verify_sync, presented, records)

    async def _verify_shared(self, d: str, presented: str, cands, ip: str, now: float) -> str | None:
        """Runs once per distinct key (as a task nobody can cancel except by shutdown): bookkeeping happens here."""
        try:
            name = await self._argon(presented, cands)
        finally:
            self._pending.pop(d, None)
        if name:
            self.ok[d] = name
        else:                                               # a matching key_id that does not verify: a FAILURE
            self.bad_keys += 1
            self._fail_row(ip, now).append(now)
        return name

    async def authenticate(self, presented: str | None, ip: str) -> str | None:
        if not presented:
            return None
        d = hashlib.sha256(presented.encode()).hexdigest()
        for known, name in self.ok.items():
            if hmac.compare_digest(known, d):
                return name
        shared = self._pending.get(d)
        if shared is None:
            now = time.monotonic()
            cands = self.by_id.get(key_id(presented))
            if not cands:
                self._fail_row(ip, now).append(now)         # garbage: no argon2 work at all, just recorded
                return None
            # A key that carries a known key_id: verify it against exactly those records. NOT subject to the per-IP
            # failure limiter (failed attempts from this address, e.g. garbage, must not lock a valid key out).
            shared = asyncio.ensure_future(self._verify_shared(d, presented, cands, ip, now))
            self._pending[d] = shared
        # The verification is its own task: a waiter that is cancelled (first or not) cancels only its own wait, never
        # the shared result the other waiters receive. (A burst of first requests costs ONE verification.)
        return await asyncio.shield(shared)

    def allow(self, name: str) -> bool:
        q = self.hits.setdefault(name, collections.deque())
        now = time.monotonic()
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= self.rate:
            return False
        q.append(now)
        return True


def jresp(obj, status=200) -> web.Response:
    return web.Response(text=json.dumps(obj), status=status, content_type="application/json")


def make_app(st: OutState, reader: Reader, keys: KeyStore, max_sse: int = MAX_SSE,
             max_sse_per_key: int = MAX_SSE_PER_KEY, sse_idle_drop_s: float | None = None,
             sse_probe_bytes: int | None = None, sse_probe_interval_s: float | None = None,
             sse_write_timeout_s: float | None = None) -> web.Application:
    idle_drop = SSE_IDLE_DROP_S if sse_idle_drop_s is None else float(sse_idle_drop_s)
    probe_bytes = SSE_PROBE_BYTES if sse_probe_bytes is None else int(sse_probe_bytes)
    probe_every = SSE_PROBE_INTERVAL_S if sse_probe_interval_s is None else float(sse_probe_interval_s)
    write_timeout = SSE_WRITE_TIMEOUT_S if sse_write_timeout_s is None else float(sse_write_timeout_s)

    @web.middleware
    async def guard(request: web.Request, handler):
        st.counters["api_requests"] += 1
        if request.method not in ("GET", "HEAD"):
            return web.Response(status=405, headers={"Allow": "GET, HEAD"}, text="read-only API")
        if request.path == "/v1/health":
            return await handler(request)
        tok = None
        a = request.headers.get("Authorization", "")
        if a.lower().startswith("bearer "):
            tok = a[7:].strip()
        tok = tok or request.headers.get("X-API-Key")
        name = await keys.authenticate(tok, request.remote or "?")
        if not name:
            st.counters["api_auth_failures"] += 1
            return web.Response(status=401, headers={"WWW-Authenticate": "Bearer"}, text="unauthorized")
        request[KEY_NAME] = name
        if not keys.allow(name):
            st.counters["api_rate_limited"] += 1
            return web.Response(status=429, headers={"Retry-After": "10"}, text="rate limited")
        return await handler(request)

    async def health(_):
        return jresp(st.health_view())

    def _zones(request):
        out = list(st.zones.values())
        k = request.query.get("kind")
        p = request.query.get("partition")
        if k:
            out = [z for z in out if z["kind"] == k]
        if p is not None and p != "":
            ps = {str(p), str(p).replace("partition-", "")}
            out = [z for z in out if str(z["partition_id"]) in ps]
        return sorted(out, key=lambda z: z["panel_zone_id"])

    async def zones(request):
        return jresp(_zones(request))

    async def zone(request):
        zid = request.match_info["id"]
        z = st.zones.get(zid if zid.startswith("zone-") else f"zone-{zid}")
        return jresp(z) if z else jresp({"error": "not found"}, 404)

    async def partitions(_):
        return jresp(sorted(st.parts.values(), key=lambda p: p["id"]))

    async def events(request):
        try:
            after = int(request.query.get("after", "0"))
            limit = min(max(int(request.query.get("limit", "100")), 1), 1000)
        except ValueError:
            return jresp({"error": "bad after/limit"}, 400)
        if not 0 <= after <= MAX_SEQ:
            return jresp({"error": "after out of range"}, 400)
        r = jresp(reader.events_after(after, limit))
        r.headers["X-BR-Floor"] = str(reader.floor())
        r.headers["X-BR-Head"] = str(reader.last_seq())
        return r

    sse_by_key: dict[str, int] = {}

    async def stream(request):
        kname = request.get(KEY_NAME, "?")
        # caps are checked AND taken before any await, so concurrent openers cannot overshoot them
        if st.counters["sse_clients"] >= max_sse or sse_by_key.get(kname, 0) >= max_sse_per_key:
            return web.Response(status=503, headers={"Retry-After": "5"}, text="too many streams")
        lid = request.headers.get("Last-Event-ID") or request.query.get("last_event_id")
        if lid is None and request.query.get("after") is not None:
            lid = request.query["after"]
        try:
            cursor = int(lid) if lid not in (None, "") else None
        except ValueError:
            return jresp({"error": "bad Last-Event-ID"}, 400)
        if cursor is not None and not 0 <= cursor <= MAX_SEQ:
            return jresp({"error": "Last-Event-ID out of range"}, 400)
        st.counters["sse_clients"] += 1
        sse_by_key[kname] = sse_by_key.get(kname, 0) + 1
        try:
            resp = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache",
                                               "X-Accel-Buffering": "no", "Connection": "keep-alive"})
            await resp.prepare(request)
            _bound_send_buffers(request.transport)
            head = max(reader.last_seq(), 0)
            if cursor is None:
                cursor = head

            last_write = time.monotonic()
            last_progress = last_write      # last moment the peer was seen to take bytes from us
            last_probe = last_write
            sent_total = 0                  # bytes handed to the transport so far
            taken = 0                       # bytes the peer has taken so far (sent_total - still unflushed)

            # A client that completed the request and never reads is NOT visible through "the write succeeded": small
            # writes land in the kernel buffers and the peer's kernel acks them. So liveness is judged by the buffers
            # themselves: (1) our send buffers are small (_bound_send_buffers) so a stalled peer backs up quickly;
            # (2) every `probe_every` seconds we send a padding comment of `probe_bytes`, which a reading client drains
            # at once and a non-reading one cannot absorb for long: once its receive window is full, the write buffer
            # stays non-empty; (3) a drain that does not finish within `write_timeout`, or a write buffer that has not
            # been empty for `idle_drop` seconds, drops the stream.
            # Progress is measured, not inferred from the buffer size: a client that reads steadily but slowly during a
            # long replay always has SOME bytes buffered, so "buffer empty" would drop it. What counts is that the
            # amount the peer has taken (bytes sent minus bytes still unflushed) keeps GROWING.
            def note_progress() -> None:
                nonlocal last_progress, taken
                t = sent_total - _unflushed(request.transport)
                if t > taken:
                    taken, last_progress = t, time.monotonic()
                elif _unflushed(request.transport) == 0:
                    last_progress = time.monotonic()        # nothing pending: nothing the peer owes us

            async def w(b: str):
                nonlocal last_write, sent_total
                data = b.encode()
                note_progress()
                sent_total += len(data)                         # handed over: counts as sent while the write is blocked
                wt = asyncio.ensure_future(resp.write(data))
                t0 = time.monotonic()
                slice_s = max(0.05, min(0.25, (idle_drop / 4) if idle_drop > 0 else 0.25))
                try:
                    # a write that blocks on a full buffer is watched in slices, so that "no progress for idle_drop" is
                    # noticed while it is blocked, not only after it returns
                    while True:
                        done, _ = await asyncio.wait({wt}, timeout=slice_s)
                        note_progress()
                        if done:
                            wt.result()
                            break
                        now = time.monotonic()
                        if now - t0 > write_timeout:            # slow consumer => dropped
                            st.counters["sse_idle_dropped"] += 1
                            raise asyncio.TimeoutError("sse write blocked for %.0fs" % write_timeout)
                        if idle_drop > 0 and now - last_progress > idle_drop:
                            st.counters["sse_idle_dropped"] += 1
                            raise asyncio.TimeoutError("sse client idle: no delivery for %.0fs" % idle_drop)
                finally:
                    if not wt.done():
                        wt.cancel()
                note_progress()
                last_write = time.monotonic()
                if idle_drop > 0 and last_write - last_progress > idle_drop:
                    st.counters["sse_idle_dropped"] += 1
                    raise asyncio.TimeoutError("sse client idle: no delivery for %.0fs" % idle_drop)
            try:
                await w("retry: 3000\n: connected\n\n")
                floor = reader.floor()
                reason = None
                if cursor > head:
                    reason, cursor = "ahead_of_head", head
                elif cursor < floor - 1:
                    reason, cursor = "pruned", floor - 1
                if reason:
                    # the client's position does not exist in this log: tell it explicitly (it must re-read REST state)
                    await w("id: %d\nevent: reset\ndata: %s\n\n" % (
                        cursor, json.dumps({"reason": reason, "floor": floor, "head": head})))
                while True:
                    ev = st.changed                                 # grab BEFORE reading the DB: no lost wakeup
                    evs = reader.events_after(cursor, 200)
                    for e in evs:
                        await w(f"id: {e['seq']}\nevent: {e['type']}\ndata: {json.dumps(e)}\n\n")
                        cursor = e["seq"]
                        st.counters["sse_events_sent"] += 1
                    if evs:
                        continue
                    idle = time.monotonic() - last_write
                    if probe_bytes > 0 and time.monotonic() - last_probe >= probe_every:
                        last_probe = time.monotonic()
                        await w(": " + "." * probe_bytes + "\n\n")  # flow-control probe (see w())
                        continue
                    if idle >= HB_SECONDS:                          # heartbeat is time-based: unrelated wakeups
                        await w(": hb\n\n")                         # (health ticks) must not starve it
                        continue
                    try:
                        await asyncio.wait_for(ev.wait(), HB_SECONDS - idle)
                    except asyncio.TimeoutError:
                        pass
            except (asyncio.CancelledError, ConnectionResetError, asyncio.TimeoutError, ConnectionError):
                pass
            return resp
        finally:
            st.counters["sse_clients"] -= 1
            sse_by_key[kname] = max(0, sse_by_key.get(kname, 1) - 1)

    app = web.Application(middlewares=[guard])
    app.router.add_get("/v1/health", health)
    app.router.add_get("/v1/zones", zones)
    app.router.add_get("/v1/zones/{id}", zone)
    app.router.add_get("/v1/partitions", partitions)
    app.router.add_get("/v1/events", events)
    app.router.add_get("/v1/stream", stream)
    return app


def render_metrics(st: OutState) -> str:
    h = st.health or {}
    c = h.get("counters", {})
    L: list[str] = []

    def m(name, typ, helptxt, samples):
        L.append(f"# HELP {name} {helptxt}")
        L.append(f"# TYPE {name} {typ}")
        for lab, v in samples:
            L.append(f"{name}{lab} {v}")
    hv = st.health_view()
    m("blackrelay_up", "gauge", "Outbound service alive", [("", 1)])
    m("blackrelay_status", "gauge", "Bridge status (1 for current)",
      [(f'{{status="{s}"}}', 1 if hv["status"] == s else 0) for s in ("online", "degraded", "offline")])
    m("blackrelay_source_link_up", "gauge", "Panel adapter link state",
      [(f'{{source="{s}"}}', 1 if v == "up" else 0) for s, v in hv["links"].items()])
    m("blackrelay_active_source", "gauge", "Active data source",
      [(f'{{source="{s}"}}', 1 if hv["active_source"] == s else 0) for s in ("iq_remote", "control4")])
    m("blackrelay_zones", "gauge", "Zones in cache", [("", hv["zones"])])
    m("blackrelay_stale_zones", "gauge", "Zones flagged stale", [("", hv["stale_zones"])])
    m("blackrelay_partitions", "gauge", "Partitions in cache", [("", hv["partitions"])])
    m("blackrelay_last_seq", "counter", "Last event sequence number", [("", hv["last_seq"])])
    m("blackrelay_seconds_since_panel_message", "gauge", "Seconds since any panel message",
      [("", hv["seconds_since_panel_message"] if hv["seconds_since_panel_message"] is not None else -1)])
    m("blackrelay_normalizer_healthy", "gauge", "Normalizer ticker/persistence healthy",
      [("", 1 if (hv.get("normalizer") or {}).get("healthy") else 0)])
    m("blackrelay_ipc_normalizer_connected", "gauge", "Outbound connected to normalizer", [("", 1 if st.ipc_up else 0)])
    m("blackrelay_mqtt_connected", "gauge", "Publisher connected to broker", [("", 1 if st.mqtt_connected else 0)])
    m("blackrelay_mqtt_published_total", "counter", "MQTT publishes", [("", st.counters["mqtt_published"])])
    m("blackrelay_mqtt_reconnects_total", "counter", "MQTT reconnects", [("", st.counters["mqtt_reconnects"])])
    fm = st.fusion.metrics() if st.fusion is not None else None
    if fm is not None:
        m("physical_fusion_connected", "gauge", "Team broker connection up", [("", fm["connected"])])
        m("physical_fusion_outbox_depth", "gauge", "Messages waiting in the fusion outbox", [("", fm["depth"])])
        m("physical_fusion_outbox_bytes", "gauge", "Bytes held by the fusion outbox", [("", fm["bytes"])])
        m("physical_fusion_outbox_oldest_age_seconds", "gauge", "Age of the oldest queued message",
          [("", round(fm["oldest_age_s"], 3))])
        m("physical_fusion_outbox_dropped_total", "counter", "Messages dropped by outbox caps",
          [('{reason="size"}', fm["dropped_size"]), ('{reason="age"}', fm["dropped_age"])])
        m("physical_fusion_outbox_cursor_rewinds_total", "counter",
          "Times a corrupt mid-journal record made the event cursor rewind (events re-sent under the same ids)",
          [("", fm.get("cursor_rewinds", 0))])
        m("physical_fusion_enqueued_total", "counter", "Messages enqueued", [("", fm["enqueued_total"])])
        m("physical_fusion_acked_total", "counter", "Messages acknowledged (PUBACK) by the broker", [("", fm["acked"])])
        m("physical_fusion_reconnects_total", "counter", "Team broker disconnects", [("", fm["reconnects"])])
        m("physical_fusion_outbox_last_entry", "counter", "Number of the last outbox entry (internal, not the wire seq)",
          [("", fm["last_seq"])])
        m("physical_fusion_last_puback_age_seconds", "gauge", "Seconds since the last PUBACK from the team broker (-1: none yet)",
          [("", fm["last_puback_age_s"] if fm.get("last_puback_age_s") is not None else -1)])
        m("physical_fusion_producer_age_seconds", "gauge", "Seconds since the fusion producer loop last ran (-1: not yet)",
          [("", fm["producer_age_s"] if fm.get("producer_age_s") is not None else -1)])
    m("blackrelay_sse_clients", "gauge", "Open SSE streams", [("", st.counters["sse_clients"])])
    m("blackrelay_sse_events_sent_total", "counter", "SSE events written", [("", st.counters["sse_events_sent"])])
    m("blackrelay_sse_idle_dropped_total", "counter", "SSE clients dropped for no delivery", [("", st.counters["sse_idle_dropped"])])
    m("blackrelay_malformed_frames_total", "counter", "Malformed adapter frames rejected by the normalizer",
      [("", c.get("malformed_frames_total", 0))])
    m("blackrelay_api_requests_total", "counter", "API requests", [("", st.counters["api_requests"])])
    m("blackrelay_api_auth_failures_total", "counter", "API auth failures", [("", st.counters["api_auth_failures"])])
    m("blackrelay_api_rate_limited_total", "counter", "API 429s", [("", st.counters["api_rate_limited"])])
    m("blackrelay_events_total", "counter", "Events produced by normalizer", [("", c.get("events_total", 0))])
    m("blackrelay_diff_events_total", "counter", "Synthetic events from snapshot diffs", [("", c.get("diff_events_total", 0))])
    m("blackrelay_snapshots_total", "counter", "Snapshots processed", [("", c.get("snapshots_total", 0))])
    m("blackrelay_failovers_total", "counter", "Source switches to fallback", [("", c.get("failovers_total", 0))])
    ad = h.get("adapter_stats", {})
    for key, nm in (("reconnects", "adapter_reconnects_total"), ("violations", "oneway_violations_blocked_total"),
                    ("panel_messages", "adapter_panel_messages_total"), ("probe_failures", "adapter_probe_failures_total"),
                    ("bad_zone_ids", "adapter_bad_zone_ids_total")):
        m(f"blackrelay_{nm}", "counter", nm, [(f'{{source="{s}"}}', ad.get(s, {}).get(key, 0))
                                               for s in ("iq_remote", "control4")])
    return "\n".join(L) + "\n"


def make_metrics_app(st: OutState) -> web.Application:
    async def metrics(_):
        return web.Response(text=render_metrics(st), content_type="text/plain", charset="utf-8")
    app = web.Application()
    app.router.add_get("/metrics", metrics)
    return app


def make_fusion_health_app(st: OutState) -> web.Application:
    """Metrics + /healthz for the standalone fusion container (GET only; 200 healthy, 503 otherwise)."""
    app = make_metrics_app(st)

    async def healthz(_):
        f = st.fusion
        h = f.health() if f is not None else {"ok": False, "problems": ["publisher not started"]}
        return web.json_response(h, status=200 if h["ok"] else 503)
    app.router.add_get("/healthz", healthz)
    return app


def tls_ctx(cert: str, key: str) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(cert, key)
    return ctx
