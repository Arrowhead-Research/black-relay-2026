"""Team ("fusion") MQTT publisher, DRAFT v0. See MQTT-CONTRACT.md.

PUBLISH-ONLY: this module never subscribes to anything (the client class below refuses `subscribe`).

Data flow:
    SQLite event log --(seq cursor)--> FusionPublisher (producer) --> Outbox (durable, on disk)
    OutState (live view) --(status/zone heartbeat)-->  Outbox
    Outbox --> FusionSender (paho thread) --QoS1--> team broker --PUBACK--> Outbox.ack()

The producer runs on the asyncio loop; the sender is a plain thread around paho's own network thread. The only
coupling is the Outbox, whose lock is never held across a paho call (see outbox.py).

All wire formatting is in formats.py. This file decides WHEN to emit, not WHAT it looks like.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import functools
import threading
import time
from collections import OrderedDict
from typing import Any, Callable

import paho.mqtt.client as mqtt

from . import formats as F
from .outbox import EVENT, STATUS, Item, Outbox
from .state import OutState
from ..normalizer.store import Reader

log = logging.getLogger("br2.fusion")

EVENT_PAGE = 500
EARLY_MAX = 256                # bound on PUBACKs remembered while their publish() call had not returned


def fusion_settings(cfg) -> dict:
    return cfg.raw["fusion"]


class PublishOnlyClient(mqtt.Client):
    """paho client that cannot subscribe: the team connection is publish-only by construction."""

    def subscribe(self, *a, **k):  # noqa: D401
        raise RuntimeError("fusion client is publish-only: subscribe is not allowed")

    def unsubscribe(self, *a, **k):
        raise RuntimeError("fusion client is publish-only: unsubscribe is not allowed")

    # --- inbound hardening. We subscribe to nothing, so a well-behaved broker sends us only CONNACK, PUBACK,
    # PINGRESP and DISCONNECT (a few bytes each). Anything else is hostile or broken: cut the connection instead of
    # buffering it. paho would otherwise keep every unsolicited QoS 2 PUBLISH in `_in_messages` until a PUBREL that
    # never comes, and read a declared-256-MiB packet into memory.
    MAX_INBOUND_PACKET = 1024
    inbound_dropped = 0

    def _handle_publish(self):
        self.inbound_dropped += 1
        log.error("fusion: unsolicited inbound PUBLISH from the broker (we subscribe to nothing): dropping the connection")
        return mqtt.MQTT_ERR_PROTOCOL

    def _packet_read(self):
        rc = super()._packet_read()
        if self._in_packet.get("remaining_length", 0) > self.MAX_INBOUND_PACKET:
            self.inbound_dropped += 1
            log.error("fusion: inbound packet of %d bytes (limit %d): dropping the connection",
                      self._in_packet["remaining_length"], self.MAX_INBOUND_PACKET)
            return mqtt.MQTT_ERR_PROTOCOL
        return rc


def guarded(fn):
    """paho runs callbacks on its network thread with suppress_exceptions=False: an exception that escapes one kills
    that thread for good (no more PUBACKs, no reconnects). Every paho callback in this codebase is wrapped so that
    nothing can escape: the error is logged and counted instead."""
    @functools.wraps(fn)
    def wrapper(self, *a, **k):
        try:
            return fn(self, *a, **k)
        except Exception:  # noqa: BLE001
            self.counters["callback_errors"] += 1
            log.exception("fusion: error in paho callback %s (swallowed)", fn.__name__)
            return None
    return wrapper


class FusionSender:
    """Sends outbox messages with QoS 1 and removes each one only when its PUBACK arrives.

    paho calls on_publish from its network thread while holding its internal mutex. Therefore: nothing here holds
    the outbox lock (or `_mid_lock` while calling the outbox) around client.publish(), and on_publish only takes
    `_mid_lock` (never held across a paho call) and then the outbox lock."""

    def __init__(self, fcfg: dict, outbox: Outbox, client_factory: Callable[[], Any] | None = None):
        self.f, self.outbox = fcfg, outbox
        self.host, self.port = str(fcfg["host"]), int(fcfg["port"])
        self.window = max(1, int(fcfg.get("max_inflight", 50)))
        self._factory = client_factory
        self.client: Any = None
        self.connected = threading.Event()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._mid_lock = threading.Lock()
        self._inflight: dict[int, str] = {}          # mid -> outbox id
        # PUBACKs that arrived while publish() had not yet returned its mid: mid -> value of `_ack_clock` when it
        # arrived. An entry only counts for a publish() call that STARTED before it arrived (see pump_once), so a
        # leftover entry can never ack a different message that happens to get the same mid after a wrap.
        self._early: "OrderedDict[int, int]" = OrderedDict()
        self._ack_clock = 0
        self._thread: threading.Thread | None = None
        self._loop_started = False
        self._gen = 0                                  # client generation: bumped on every (re)start; see _is_current
        self._mono: Callable[[], float] = time.monotonic
        self.stall_s = float(fcfg.get("watchdog_stall_s", 60))
        self._progress = self._mono()                  # last sign of life: connect, PUBACK, or an empty outbox
        self.counters = {"published": 0, "acked": 0, "reconnects": 0, "publish_errors": 0,
                         "callback_errors": 0, "watchdog_restarts": 0, "inbound_dropped": 0,
                         "stale_callbacks": 0}
        self.last_puback_mono: float | None = None        # monotonic time of the last PUBACK (None: none yet)

    # ---- configuration
    def _password(self) -> str | None:
        pf = self.f.get("password_file")
        if pf:
            try:
                return open(pf, encoding="utf-8").read().strip() or None
            except OSError as e:
                log.error("fusion: cannot read password_file %s: %s", pf, e)
        return None

    def _make_client(self):
        if self._factory:
            return self._factory()
        c = PublishOnlyClient(mqtt.CallbackAPIVersion.VERSION2, client_id=str(self.f["client_id"]),
                              protocol=mqtt.MQTTv311, clean_session=True)
        tls = self.f.get("tls")
        if tls:
            c.tls_set(ca_certs=tls.get("ca_file") or None, certfile=tls.get("certfile") or None,
                      keyfile=tls.get("keyfile") or None)
        else:
            log.warning("fusion: TLS is OFF. Publishing PLAINTEXT MQTT to %s:%s. This is accepted in v0 only "
                        "because the team broker is plaintext today; add a `fusion.tls` block when it supports TLS.",
                        self.host, self.port)
        user = self.f.get("username")
        if user:
            if not tls:
                log.warning("fusion: username is set but TLS is off: the credentials cross the network unencrypted")
            c.username_pw_set(user, self._password())
        c.max_inflight_messages_set(self.window)
        c.max_queued_messages_set(self.window + 16)          # bounded outgoing queue (default: unlimited)
        c.reconnect_delay_set(min_delay=1, max_delay=60)
        return c

    # ---- paho callbacks (run on paho's thread; every one is wrapped by @guarded)
    # Every callback belongs to ONE client generation. After a watchdog restart the old client may still deliver late
    # callbacks (disconnect, PUBACK); those must neither clear `connected` for the new client nor ack by a mid that
    # the new client reused. `_start_client` binds each callback to its generation; a callback whose generation is
    # not the current one is ignored (counted in `stale_callbacks`).
    def _is_current(self, client, gen) -> bool:
        """Call with `_mid_lock` held."""
        if gen is None:
            gen = getattr(client, "_br_gen", None)
        if gen is None:                                # an untagged client (tests / factories): identity decides;
            return client is None or client is self.client      # a caller that names no client is the current one
        return gen == self._gen

    def _stale(self, what: str) -> None:
        self.counters["stale_callbacks"] += 1
        log.info("fusion: ignoring %s from a previous client generation", what)

    @guarded
    def _on_connect(self, client, userdata, flags, rc, props=None, gen=None):
        with self._mid_lock:
            current = self._is_current(client, gen)
        if not current:
            return self._stale("on_connect")
        ok = (rc == 0) if isinstance(rc, int) else (not getattr(rc, "is_failure", False))
        if ok:
            log.info("fusion: connected to %s:%s", self.host, self.port)
            self._progress = self._mono()
            self.connected.set()
            self._wake.set()
        else:
            log.warning("fusion: connect refused: %s", rc)

    @guarded
    def _on_disconnect(self, client, userdata, *args, gen=None):
        with self._mid_lock:
            if not self._is_current(client, gen):
                current = False
            else:
                current = True
                was = self.connected.is_set()
                self.connected.clear()
                self._inflight.clear()                # unacked messages stay in the outbox and are re-sent
                self._early.clear()
        if not current:
            return self._stale("on_disconnect")
        if was:
            self.counters["reconnects"] += 1
            log.warning("fusion: disconnected from broker; %d message(s) queued", self.outbox.stats()["depth"])

    @guarded
    def _on_publish(self, client, userdata, mid, *args, gen=None):
        with self._mid_lock:
            if not self._is_current(client, gen):
                current = False
                k = None
            else:
                current = True
                self._ack_clock += 1
                k = self._inflight.pop(mid, None)
                if k is None:
                    self._early[mid] = self._ack_clock
                    while len(self._early) > EARLY_MAX:
                        self._early.popitem(last=False)
        if not current:
            return self._stale("PUBACK")
        if k is not None:
            self._acked(k)

    def _acked(self, k: str) -> None:
        self._progress = self._mono()
        try:
            self.outbox.ack(k)                        # takes the outbox lock; paho is NOT being called
        finally:
            self.counters["acked"] += 1
            self.last_puback_mono = time.monotonic()
            self._wake.set()

    # ---- lifecycle
    def _start_client(self) -> None:
        c = self._make_client()
        with self._mid_lock:
            self._gen += 1
            gen = self._gen
            self.client = c
        try:
            c._br_gen = gen
        except Exception:  # noqa: BLE001
            pass
        self.client.on_connect, self.client.on_disconnect, self.client.on_publish = (
            lambda cl, ud, fl, rc, props=None, _g=gen: self._on_connect(cl, ud, fl, rc, props, gen=_g),
            lambda cl, ud, *a, _g=gen: self._on_disconnect(cl, ud, *a, gen=_g),
            lambda cl, ud, mid, *a, _g=gen: self._on_publish(cl, ud, mid, *a, gen=_g))
        c.connect_async(self.host, self.port, keepalive=int(self.f.get("keepalive_s", 30)))
        c.loop_start()
        self._loop_started = True

    @staticmethod
    def _stop_client(old: Any, timeout: float = 5.0) -> bool:
        """Disconnect `old` and JOIN its network loop. True when its thread is gone. A stuck loop is bounded by
        `timeout`, its socket is then closed under it; its late callbacks are ignored anyway (generation)."""
        def work():
            for fn in (getattr(old, "disconnect", None), getattr(old, "loop_stop", None)):
                try:
                    if fn:
                        fn()
                except Exception:  # noqa: BLE001
                    pass
        t = threading.Thread(target=work, name="fusion-old-client", daemon=True)
        t.start()
        t.join(timeout)
        if t.is_alive():
            sock = getattr(old, "_sock", None)
            try:
                if sock is not None:
                    sock.close()
            except Exception:  # noqa: BLE001
                pass
            t.join(1.0)
        th = getattr(old, "_thread", None)
        return not t.is_alive() and not (th is not None and th.is_alive())

    def start(self) -> None:
        self._start_client()
        self._thread = threading.Thread(target=self._run, name="fusion-sender", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self.client is not None:
            try:
                self.client.disconnect()
            finally:
                self.client.loop_stop()
        if self._thread:
            self._thread.join(2)

    def wake(self) -> None:
        self._wake.set()

    def inflight(self) -> int:
        with self._mid_lock:
            return len(self._inflight)

    def pump_once(self) -> int:
        """Publish whatever fits in the window. Returns the number of messages handed to paho."""
        sent = 0
        while self.connected.is_set() and not self._stop.is_set():
            with self._mid_lock:
                room = self.window - len(self._inflight)
                exclude = set(self._inflight.values())
            if room <= 0:
                break
            batch = self.outbox.pending(room, exclude)       # outbox lock taken and released inside
            if not batch:
                break
            for e in batch:
                with self._mid_lock:
                    clock0 = self._ack_clock                  # an early PUBACK must be newer than this to be ours
                try:                                          # no lock of ours is held here
                    info = self.client.publish(e.topic, e.payload, qos=F.QOS, retain=F.RETAIN)
                except Exception as ex:  # noqa: BLE001
                    self.counters["publish_errors"] += 1
                    log.warning("fusion: publish failed: %s", ex)
                    return sent
                if info.rc != mqtt.MQTT_ERR_SUCCESS:
                    self.counters["publish_errors"] += 1
                    return sent
                self.counters["published"] += 1
                sent += 1
                with self._mid_lock:
                    at = self._early.pop(info.mid, None)
                    early = at is not None and at > clock0
                    if not early:
                        self._inflight[info.mid] = e.id       # (a stale entry was just discarded, not honoured)
                if early:
                    self._acked(e.id)
        return sent

    # ---- watchdog
    def watchdog_check(self) -> bool:
        """Detect a dead or stuck publisher loop and reconnect. Returns True if it restarted the client.
        Dead: paho's network thread is no longer alive. Stuck: connected, outbox non-empty, and no PUBACK / connect
        for `watchdog_stall_s`."""
        if self._stop.is_set() or self.client is None:
            return False
        now = self._mono()
        # paho sets `_thread = None` itself when its loop function exits, so None after loop_start() means dead too
        dead = self._loop_started and hasattr(self.client, "_thread") and not (
            self.client._thread is not None and self.client._thread.is_alive())
        stuck = False
        if self.connected.is_set():
            if self.outbox.stats()["depth"] == 0:
                self._progress = now
            else:
                stuck = now - self._progress > self.stall_s
        if not (dead or stuck):
            return False
        self.counters["watchdog_restarts"] += 1
        log.error("fusion watchdog: publisher %s (outbox non-empty, connected=%s): reconnecting",
                  "network thread is dead" if dead else f"made no progress for {now - self._progress:.0f}s",
                  self.connected.is_set())
        self._restart_client()
        return True

    def _restart_client(self) -> None:
        """Replace the client. The old one is fully stopped (disconnect + loop thread joined) BEFORE the new one is
        started, and the generation is bumped first so nothing the old client still delivers can touch our state."""
        old = self.client
        with self._mid_lock:
            self._gen += 1                                # the old client is now a previous generation
            self.connected.clear()
            self._inflight.clear()
            self._early.clear()
        if old is not None and not self._stop_client(old):
            log.error("fusion watchdog: the old client's network thread did not stop; starting a new client anyway "
                      "(its late callbacks are ignored)")
        self._progress = self._mono()
        try:
            self._start_client()
        except Exception:  # noqa: BLE001
            log.exception("fusion watchdog: cannot start a new client; will retry")
            return

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(1.0)
            self._wake.clear()
            try:
                self.pump_once()
                self.watchdog_check()
            except Exception:  # noqa: BLE001
                log.exception("fusion sender error")
                time.sleep(1)


class FusionPublisher:
    """Producer: turns committed log rows and live state into outbox items, then keeps the sender fed."""

    def __init__(self, cfg, state: OutState, reader: Reader, outbox: Outbox | None = None,
                 sender: FusionSender | None = None, clock: Callable[[], float] = time.time,
                 mono: Callable[[], float] = time.monotonic):
        self.cfg, self.st, self.reader = cfg, state, reader
        self.f = fusion_settings(cfg)
        self.prefix = F.clean_prefix(self.f.get("topic_prefix"))
        self.site = cfg.raw.get("site") or F.DEFAULT_SITE
        self.t_events, self.t_panel, self.t_zones = (F.topic_events(self.prefix), F.topic_panel(self.prefix),
                                                     F.topic_zones(self.prefix))
        ob = self.f.get("outbox") or {}
        self.outbox = outbox or Outbox(ob.get("dir") or os.path.join(self._state_dir(), "fusion"),
                                       max_bytes=ob.get("max_bytes", 10 * 1024 * 1024),
                                       max_messages=ob.get("max_messages", 20000),
                                       max_age_s=ob.get("max_age_s", 86400))
        self.sender = sender or FusionSender(self.f, self.outbox)
        self.interval = float(self.f.get("status_interval_s", 60))
        self.clock, self.mono = clock, mono
        self._zone_cache: dict[str, dict] = {}
        self._panel_last: tuple[str, float] | None = None       # (json of view, mono sent)
        self._zone_last: dict[str, tuple[str, float]] = {}
        self._started = mono()
        self._last_expire = mono()
        self._cursor_ready = False
        self.last_tick_mono: float | None = None
        state.fusion = self                                      # for /metrics

    def _state_dir(self) -> str:
        from .. import config as C
        return C.out_state_dir(self.cfg)

    # ---- metrics
    def metrics(self) -> dict:
        s = self.outbox.stats()
        lp = getattr(self.sender, "last_puback_mono", None)
        s.update(connected=1 if self.sender.connected.is_set() else 0, inflight=self.sender.inflight(),
                 reconnects=self.sender.counters["reconnects"], published=self.sender.counters["published"],
                 acked=self.sender.counters["acked"],
                 callback_errors=self.sender.counters.get("callback_errors", 0),
                 watchdog_restarts=self.sender.counters.get("watchdog_restarts", 0),
                 inbound_dropped=(self.sender.counters.get("inbound_dropped", 0)
                                  + getattr(getattr(self.sender, "client", None), "inbound_dropped", 0)),
                 last_puback_age_s=(round(self.mono() - lp, 3) if lp is not None else None),
                 producer_age_s=(round(self.mono() - self.last_tick_mono, 3)
                                 if self.last_tick_mono is not None else None))
        return s

    def health(self) -> dict:
        """Liveness for the container healthcheck. Unhealthy when the producer loop has stopped ticking, or when the
        oldest queued message has waited longer than `health_max_backlog_age_s` (broker unreachable or not acking).
        An idle, connected publisher is healthy however long ago the last PUBACK was."""
        m = self.metrics()
        max_tick = float(self.f.get("health_max_tick_age_s", 30))
        max_backlog = float(self.f.get("health_max_backlog_age_s", 300))
        tick_age = m["producer_age_s"]
        problems = []
        if tick_age is None:
            if self.mono() - self._started > max_tick:
                problems.append("producer has not run")
        elif tick_age > max_tick:
            problems.append(f"producer stalled for {tick_age:.0f}s")
        if m.get("last_write_error"):
            problems.append(f"outbox journal write failing: {m['last_write_error']} (retrying; events are not lost)")
        if m["depth"] > 0 and m["oldest_age_s"] > max_backlog:
            problems.append(f"oldest queued message is {m['oldest_age_s']:.0f}s old (broker unreachable or not acking)")
        return {"ok": not problems, "problems": problems, "degraded": bool(m.get("last_write_error")), "connected": bool(m["connected"]), "depth": m["depth"],
                "dropped": m["dropped_size"] + m["dropped_age"], "last_puback_age_s": m["last_puback_age_s"],
                "producer_age_s": tick_age}

    # ---- producer steps
    def _remember_zones(self) -> None:
        for zid, z in self.st.zones.items():
            self._zone_cache[zid] = {k: z.get(k) for k in ("id", "name", "kind", "partition_id", "state", "stale",
                                                           "source")}

    def _init_cursor(self) -> bool:
        """First start with no cursor: begin at the current head (history from before fusion was enabled is not
        replayed onto the team broker). Afterwards the cursor is persisted atomically with the messages it covers."""
        did = self.reader.db_id()
        if did is None:
            return False
        cur = self.outbox.cursor
        head, floor = self.reader.last_seq(), self.reader.floor()
        pub = None
        if cur is None:
            seq = head
            log.info("fusion: no cursor yet; starting at log seq %d", seq)
        else:
            same_db = cur.get("db_id") in (None, did)
            seq = int(cur.get("seq", 0)) if same_db else 0
            pub = cur.get("pub") if same_db else None
            seq = min(seq, head)
            seq = max(seq, floor - 1)
        self.outbox.append([], cursor={"seq": seq, "db_id": did, "pub": pub})
        self._cursor_ready = True
        return True

    def _event_items(self, now: float) -> tuple[list[Item], dict | None]:
        cur = self.outbox.cursor or {}
        evs = self.reader.events_after(int(cur.get("seq", 0)), EVENT_PAGE, with_zone=True)
        if not evs:
            return [], None
        did = cur.get("db_id")
        pub = cur.get("pub")                                 # seq of the last event message queued (the prev_seq chain)
        items: list[Item] = []
        for e in evs:
            z = self.st.zones.get(e["subject"]) or self._zone_cache.get(e["subject"])
            et = F.map_log_event(e, (F.zone_at_event(z, e) or {}).get("kind"))
            if et is None:
                continue
            mid, build = F.event_builder(self.site, e, z, et, now, did, prev_seq=pub)
            pub = e["seq"]
            items.append(Item(mid, self.t_events, EVENT, lambda _n, b=build: F.encode(b())))
        return items, {"seq": evs[-1]["seq"], "db_id": did, "pub": pub}

    def _status_items(self, now: float) -> list[Item]:
        items: list[Item] = []
        m = self.mono()
        if self.st.have_dump or m - self._started >= float(self.f.get("startup_grace_s", 10)):
            hv = self.st.health_view()
            view = F.panel_view(hv)
            key = json.dumps(view, sort_keys=True)
            if self._panel_last is None or self._panel_last[0] != key or m - self._panel_last[1] >= self.interval:
                mid, build = F.panel_builder(self.site, view, hv.get("last_panel_message_at"), now)
                items.append(Item(mid, self.t_panel, STATUS, lambda _n, b=build: F.encode(b())))
                self._panel_last = (key, m)
        if self.st.have_dump:
            for zid in list(self._zone_last):
                if zid not in self.st.zones:
                    del self._zone_last[zid]
            for zid, z in sorted(self.st.zones.items()):
                key = json.dumps(F.zone_view(z), sort_keys=True)
                last = self._zone_last.get(zid)
                if last is None or last[0] != key or m - last[1] >= self.interval:
                    mid, build = F.zone_status_builder(self.site, z, now)
                    items.append(Item(mid, self.t_zones, STATUS, lambda _n, b=build: F.encode(b())))
                    self._zone_last[zid] = (key, m)
        return items

    async def tick(self) -> None:
        """One producer pass. Safe to call from tests without sleeping."""
        now = self.clock()
        self.last_tick_mono = self.mono()
        if self.st.have_dump:
            self._remember_zones()
            if not self._cursor_ready and not self._init_cursor():
                return
            while True:                                     # page through ALL committed events not yet queued
                items, cursor = self._event_items(now)
                if cursor is None:
                    break
                await asyncio.to_thread(self.outbox.append, items, cursor)
                if self.reader.last_seq() <= cursor["seq"]:
                    break
        status = self._status_items(now)
        if status:
            await asyncio.to_thread(self.outbox.append, status)
        if self.mono() - self._last_expire >= 30:
            self._last_expire = self.mono()
            await asyncio.to_thread(self.outbox.expire)
        self.sender.wake()

    async def run(self) -> None:
        self.sender.start()
        log.info("fusion publisher up: %s:%s prefix=%s site=%s", self.f["host"], self.f["port"], self.prefix,
                 F.site_block(self.site)["id"])
        try:
            while True:
                ev = self.st.changed
                try:
                    await self.tick()
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001
                    log.exception("fusion producer error")
                try:
                    await asyncio.wait_for(ev.wait(), 1.0)
                except asyncio.TimeoutError:
                    pass
        finally:
            self.sender.stop()
            self.outbox.close()
