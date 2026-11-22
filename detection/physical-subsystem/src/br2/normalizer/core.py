"""Pure, deterministic normalizer engine (no I/O). All mutation happens through handle()/tick()/ipc_down(),
which the service calls from ONE task, so arbitration and diffing are race-free by construction.

Algorithm
  * Every adapter streams a source-neutral observation feed: snapshot (full), zone/partition deltas,
    link up/down. The engine keeps one SHADOW view per source (its own latest full picture, updated by
    that source's snapshot + deltas, whether or not the source is currently active).
  * The CACHE is the published view. It always equals resolve(shadow[active]) (plus stale flag).
  * Failover/failback and every (re)connect snapshot are the same operation: reconcile(cache, new view)
    emitting exact synthetic events for each difference (zone.added/removed, state/tamper/battery/online,
    partition arming/alarm). Deltas of the active source emit non-synthetic events.
  * Arbitration is a pure function of (link states, active, time since primary went down).

Robustness mechanisms (round 2)
  * TRANSACTIONAL: every entry point (handle/tick/ipc_down) first captures the published state (copy-on-write: records
    are never mutated after publication, so shallow dict copies suffice). The service persists the returned Batch
    atomically and then calls commit(); if persisting fails it calls rollback(), which restores the previous state
    exactly and schedules a full resync (the retained shadows are inputs and are kept), so memory can never be
    ahead of the durable log / what consumers saw.
  * PER-SOURCE IPC LIVENESS: every frame (incl. keep-alive pings) stamps its source; a source that has been silent for
    `timers.ipc_liveness_s` is declared down (hung adapter), and stays down until it sends a fresh snapshot.
  * AUTHORITY: a full snapshot from the PRIMARY is authoritative: every zone/partition absent from it is removed
    (zone.removed, retained topics cleared) whichever source last updated it. A FALLBACK snapshot only removes what the
    fallback owns/reported earlier, or any kind not in control4.uncovered_kinds (default empty)
    and not overridden by zones.N.control4_visible: false; any other record (uncovered kind) is kept and
    flagged stale, and un-staled/refreshed when the primary's next snapshot lands.
  * VALIDATION: zone/partition frames are validated at the boundary; a malformed one is rejected whole (logged, counted
    in malformed_frames_total) so it can never poison a shadow.
"""
from __future__ import annotations

import copy
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from .. import model as M

PRIMARY, FALLBACK = "iq_remote", "control4"
log = logging.getLogger("br2.engine")
_MAXID = 2 ** 31


def _intlike(v: Any, allow_empty: bool = False) -> bool:
    if allow_empty and v in (None, ""):
        return True
    if isinstance(v, bool):
        return False
    if isinstance(v, int):
        return 0 <= v < _MAXID
    if isinstance(v, str):
        t = v.strip()
        return t.isascii() and t.isdigit() and int(t) < _MAXID
    return False


def _strish(v: Any) -> bool:
    return v is None or isinstance(v, (str, int, float))


def _valid_zone(z: Any) -> bool:
    """A zone observation/delta: integer-like id, integer-like (or absent) partition, scalar text-ish fields."""
    if not isinstance(z, dict) or not _intlike(z.get("panel_zone_id")):
        return False
    if not _intlike(z.get("partition_id"), allow_empty=True):
        return False
    if not all(_strish(z.get(k)) for k in ("name", "type", "group", "status", "battery")):
        return False
    return z.get("raw") is None or isinstance(z.get("raw"), dict)


def _valid_part(p: Any) -> bool:
    return (isinstance(p, dict) and _intlike(p.get("partition_id"))
            and all(_strish(p.get(k)) for k in ("name", "status", "alarm_state", "alarm_type")))


def zinfo(rec: dict, stale: bool | None = None) -> dict:
    """Zone attributes as of an event, stored in the log row: later renames / re-classification / stale flips must not
    rewrite history for consumers that read the event afterwards. Together with the event's own from/to this is enough
    to rebuild the cache by replaying the log from seq 0 (name, kind, partition, state, tamper, battery_low, online,
    stale)."""
    return {"name": rec.get("name"), "kind": rec.get("kind"), "partition": rec.get("partition_id"),
            "stale": bool(rec.get("stale")) if stale is None else stale, "state": rec.get("state"),
            "tamper": bool(rec.get("tamper")), "battery_low": bool(rec.get("battery_low")),
            "online": bool(rec.get("online", True))}


def pinfo(rec: dict, stale: bool | None = None) -> dict:
    """Partition attributes as of an event (stored in the same `zone_json` log column as zinfo for zones)."""
    return {"name": rec.get("name"), "arming": rec.get("arming"), "alarm_type": rec.get("alarm_type"),
            "stale": bool(rec.get("stale")) if stale is None else stale}


@dataclass
class Batch:
    events: list[dict] = field(default_factory=list)
    zones: dict[str, dict] = field(default_factory=dict)       # upserted records by id
    removed_zones: list[str] = field(default_factory=list)
    partitions: dict[str, dict] = field(default_factory=dict)
    removed_partitions: list[str] = field(default_factory=list)
    status: str | None = None                                   # set when bridge status changed
    meta: dict[str, Any] = field(default_factory=dict)          # durable engine meta (e.g. active source)

    def empty(self) -> bool:
        return not (self.events or self.zones or self.removed_zones or self.partitions
                    or self.removed_partitions or self.status or self.meta)


class Clock:
    def mono(self) -> float:
        import time
        return time.monotonic()

    def wall(self) -> float:
        import time
        return time.time()


@dataclass
class Src:
    name: str
    enabled: bool = False
    live: bool = False
    ipc: bool = False
    down_since: float | None = None
    zones: dict[int, dict] = field(default_factory=dict)
    parts: dict[int, dict] = field(default_factory=dict)
    last_msg_wall: float | None = None
    stats: dict[str, float] = field(default_factory=dict)
    link_since_wall: float | None = None
    reported_live: bool = False          # last link state a bridge.link event was produced for (transactional)
    last_frame: float | None = None      # monotonic time of the last IPC frame from this source
    live_since: float | None = None      # monotonic time the source last became live (flap hold-down)
    empty_snaps: int = 0                 # consecutive empty-zone snapshots seen (never trusted blindly)
    gone_z: set = field(default_factory=set)   # zone ids THIS source itself reported earlier and has since dropped
    gone_p: set = field(default_factory=set)
    link_reason: str | None = None       # why the adapter last reported its link down (e.g. pin_mismatch)


class Engine:
    def __init__(self, cfg, clock: Clock | None = None):
        self.cfg = cfg
        self.clock = clock or Clock()
        t0 = self.clock.mono()
        self.src = {
            PRIMARY: Src(PRIMARY, enabled=cfg.iq_enabled, down_since=t0),
            FALLBACK: Src(FALLBACK, enabled=cfg.c4_enabled, down_since=t0),
        }
        self.fallback_after = float(cfg.timers["fallback_after_s"])
        self.ipc_deadline = float(cfg.timers.get("ipc_liveness_s", 5))
        self.holddown = float(cfg.timers.get("failback_holddown_s", 3))   # primary must be stable this long
        self.active: str | None = None
        self.zones: dict[int, dict] = {}
        self.parts: dict[int, dict] = {}
        self._status = "offline"
        self.last_msg_wall: float | None = None
        self.counters = {"events_total": 0, "snapshots_total": 0, "failovers_total": 0, "diff_events_total": 0,
                         "liveness_timeouts_total": 0, "rollbacks_total": 0,
                         "empty_snapshots_rejected_total": 0, "malformed_frames_total": 0,
                         "unknown_zone_deltas_total": 0, "partial_snapshots_total": 0}
        self.resync_needed = False
        self._saved: dict | None = None

    # ------------------------------------------------------------ transaction (copy-on-write)
    def _begin(self) -> None:
        if self._saved is not None:          # an earlier entry point was neither committed nor rolled back
            return
        self._saved = {"zones": dict(self.zones), "parts": dict(self.parts), "active": self.active,
                       "status": self._status, "last_msg_wall": self.last_msg_wall,
                       "counters": dict(self.counters),
                       "links": {n: (s.reported_live, s.link_since_wall) for n, s in self.src.items()}}

    def commit(self) -> None:
        """The Batch returned by the last entry point is durable; the new published state stands."""
        self._saved = None

    def rollback(self, resync: bool = True) -> None:
        """The Batch could not be persisted (or computing it failed): restore the previous published state exactly.
        Source shadows (inputs) are kept, so the scheduled resync recomputes the same change from them."""
        sv, self._saved = self._saved, None
        if sv is None:
            return
        self.zones, self.parts, self.active, self._status = sv["zones"], sv["parts"], sv["active"], sv["status"]
        self.last_msg_wall = sv["last_msg_wall"]
        rb = self.counters.get("rollbacks_total", 0) + 1
        self.counters = sv["counters"]
        self.counters["rollbacks_total"] = rb
        for n, (rep, since) in sv["links"].items():
            self.src[n].reported_live, self.src[n].link_since_wall = rep, since
        if resync:
            self.resync_needed = True

    def meta(self) -> dict:
        return {"active": self.active}

    # ------------------------------------------------------------ restore
    def restore(self, zones: list[dict], parts: list[dict], meta: dict | None = None) -> Batch:
        """Load the persisted cache after a restart. Nothing vouches for it yet, so every record is stale. Returns the
        Batch that records that (a zone.stale event for each zone that was fresh when we last ran); the service
        persists it before it does anything else."""
        self._begin()                       # same copy-on-write transaction as every other transition
        try:
            b = Batch()
            ts = self._now()[2]
            for z in zones:
                was_fresh = not z.get("stale")
                z = dict(z); z["stale"] = True
                self.zones[int(z["panel_zone_id"])] = z
                if was_fresh:
                    b.zones[z["id"]] = copy.deepcopy(z)
                    b.events.append({"type": "zone.stale", "subject": z["id"], "from": False, "to": True,
                                     "source": z.get("source"), "synthetic": False, "ts": ts, "zone": zinfo(z, True)})
            for p in parts:
                was_fresh = not p.get("stale")
                p = dict(p); p["stale"] = True
                self.parts[int(p["panel_partition_id"])] = p
                if was_fresh:
                    b.partitions[p["id"]] = copy.deepcopy(p)
                    b.events.append({"type": "partition.stale", "subject": p["id"], "from": False, "to": True,
                                     "source": p.get("source"), "synthetic": False, "ts": ts, "zone": pinfo(p, True)})
            # We restarted while the fallback was the published source: the primary has (as far as anyone knows) been
            # down since before we started, so do not make the fallback wait another fallback_after_s.
            if (meta or {}).get("active") == FALLBACK and self.src[PRIMARY].enabled:
                self.src[PRIMARY].down_since = self.clock.mono() - self.fallback_after
        except BaseException:
            self.rollback(resync=False)     # a malformed cache row must not leave a half-restored engine
            raise
        return b

    # ------------------------------------------------------------ helpers
    def _now(self) -> tuple[float, float, str]:
        return self.clock.mono(), self.clock.wall(), M.iso(self.clock.wall())

    def _enabled(self) -> list[str]:
        return [n for n in (PRIMARY, FALLBACK) if self.src[n].enabled]

    def _ignored(self, zid: int) -> bool:
        return bool(self.cfg.zone_over.get(zid, {}).get("ignore"))

    def _hidden(self, obs: dict) -> bool:
        """Keypad / IQ Remote zones (the panel's own remotes, incl. this bridge) are not sensors: not published unless
        zones.include_keypads is true. They stay in the source's shadow, never in the cache."""
        return (not self.cfg.include_keypads) and M.is_keypad(obs.get("type"), obs.get("group"), obs.get("name"))

    def _resolve_zone(self, zid: int, obs: dict, prev: dict | None) -> dict:
        ov = self.cfg.zone_over.get(zid, {})
        kind = M.classify(obs.get("type"), obs.get("group"), ov.get("kind"))
        state, online = M.map_state(kind, obs.get("status"))
        if state is None:
            state = prev["state"] if prev else "unknown"
        if online is None:
            online = prev["online"] if prev else True
        tamper = bool(obs.get("tamper")) or M._norm(obs.get("status")) == "tampered"
        bl = M.map_battery(obs.get("battery"))
        if bl is None:
            bl = prev["battery_low"] if prev else False
        rec = copy.deepcopy(prev) if prev else M.blank_zone(zid)
        rec.update({
            "name": ov.get("name") or obs.get("name") or rec.get("name") or f"Zone {zid}",
            "kind": kind,
            "partition_id": int(obs["partition_id"]) if obs.get("partition_id") not in (None, "") else rec["partition_id"],
            "state": state, "tamper": tamper, "battery_low": bl, "online": online,
            "raw": dict(obs.get("raw") or {}),
        })
        return rec

    def _resolve_part(self, pid: int, obs: dict, prev: dict | None) -> dict:
        arming = M.map_arming(obs.get("status"), obs.get("alarm_state"))
        rec = copy.deepcopy(prev) if prev else {"id": M.partition_key(pid), "panel_partition_id": pid,
                                                "name": f"Partition {pid}", "arming": "unknown", "alarm_type": None,
                                                "last_changed": None, "source": None, "stale": True}
        at = obs.get("alarm_type")
        if arming != "alarm":
            at = None
        elif not at:
            at = "unknown"
        rec.update({"name": obs.get("name") or rec["name"], "arming": arming, "alarm_type": at})
        return rec

    def _zone_events(self, old: dict | None, new: dict, src: str | None, syn: bool, ts: str, out: list[dict]) -> None:
        """Every change between `old` and `new` is its own event, in a fixed order; each event's `zone` block is the zone
        AS OF THAT EVENT (changes applied cumulatively), so replaying the log from seq 0 rebuilds the cache exactly."""
        sub = new["id"]
        if old is None:
            out.append({"type": "zone.added", "subject": sub, "from": None, "to": new["state"], "source": src,
                        "synthetic": syn, "ts": ts, "zone": zinfo(new, False)})       # callers mark it fresh right after
            return
        cur = dict(old)

        def ev(t, f, to, fld):
            cur[fld] = to
            out.append({"type": t, "subject": sub, "from": f, "to": to, "source": src, "synthetic": syn, "ts": ts,
                        "zone": zinfo(cur)})
        if old.get("stale"):
            # every caller marks the record fresh right after this: log the flip in the same batch (same transaction)
            ev("zone.stale", True, False, "stale")
        for fld, t, nf in (("name", "zone.renamed", "name"), ("kind", "zone.kind", "kind"),
                           ("partition_id", "zone.partition", "partition_id")):
            if old.get(fld) != new.get(fld):
                ev(t, old.get(fld), new.get(fld), nf)
        for fld, t in (("state", "zone.state"), ("tamper", "zone.tamper"),
                       ("battery_low", "zone.battery"), ("online", "zone.online")):
            if old[fld] != new[fld]:
                ev(t, old[fld], new[fld], fld)

    def _part_events(self, old: dict | None, new: dict, src: str | None, syn: bool, ts: str, out: list[dict]) -> None:
        """Same rule as _zone_events: one event per change, `zone` column = the partition AS OF THAT EVENT."""
        sub = new["id"]
        if old is None:
            out.append({"type": "partition.added", "subject": sub, "from": None, "to": new["arming"], "source": src,
                        "synthetic": syn, "ts": ts, "zone": pinfo(new, False)})
            return
        cur = dict(old)

        def ev(t, f, to, fld):
            cur[fld] = to
            out.append({"type": t, "subject": sub, "from": f, "to": to, "source": src, "synthetic": syn, "ts": ts,
                        "zone": pinfo(cur)})
        if old.get("stale"):
            ev("partition.stale", True, False, "stale")
        for fld, t in (("name", "partition.renamed"), ("arming", "partition.arming"),
                       ("alarm_type", "partition.alarm")):
            if old[fld] != new[fld]:
                ev(t, old[fld], new[fld], fld)

    # ------------------------------------------------------------ views / reconcile
    def _view(self, name: str) -> tuple[dict[int, dict], dict[int, dict]]:
        s = self.src[name]
        zv = {zid: self._resolve_zone(zid, obs, self.zones.get(zid))
              for zid, obs in s.zones.items() if not self._ignored(zid) and not self._hidden(obs)}
        pv = {pid: self._resolve_part(pid, obs, self.parts.get(pid)) for pid, obs in s.parts.items()}
        return zv, pv

    def _fallback_authoritative(self, name: str, zid: int, old: dict) -> bool:
        """May snapshot `name` (the fallback) remove zone `zid` it omits? Control4 lists every panel zone, so yes unless
        the zone's kind is in control4.uncovered_kinds (it owns/reported it earlier => always yes). `control4_visible: false` forces stale-only."""
        if self.cfg.zone_over.get(zid, {}).get("control4_visible") is False:
            return False
        if old.get("source") in (name, None) or zid in self.src[name].gone_z:
            return True
        return old.get("kind") not in (self.cfg.c4.get("uncovered_kinds") or ())

    def _reconcile(self, name: str, b: Batch, syn: bool = True) -> None:
        """Make the cache equal to source `name`'s shadow, emitting exact synthetic events for every diff.
        The primary is authoritative (absent => removed); records the fallback never reported are kept (stale)."""
        mono, wall, ts = self._now()
        zv, pv = self._view(name)
        for zid in sorted(set(zv) | set(self.zones)):
            old, new = self.zones.get(zid), zv.get(zid)
            if new is None:
                assert old is not None
                if (name == PRIMARY or self._ignored(zid) or zid in self.src[name].zones      # held but hidden (keypad)
                        or self._fallback_authoritative(name, zid, old)):
                    self.zones.pop(zid)
                    b.events.append({"type": "zone.removed", "subject": old["id"], "from": old["state"], "to": None,
                                     "source": name, "synthetic": syn, "ts": ts, "zone": zinfo(old)})
                    b.removed_zones.append(old["id"])
                    self.counters["diff_events_total"] += 1
                elif not old["stale"]:
                    rec = dict(old, stale=True)
                    self.zones[zid] = rec
                    b.zones[rec["id"]] = copy.deepcopy(rec)
                    b.events.append({"type": "zone.stale", "subject": rec["id"], "from": False, "to": True,
                                     "source": rec.get("source"), "synthetic": syn, "ts": ts, "zone": zinfo(rec)})
                continue
            n0 = len(b.events)
            self._zone_events(old, new, name, syn, ts, b.events)
            if old is None or old["state"] != new["state"]:
                new["last_changed"] = ts
            new.update({"source": name, "stale": False, "last_seen": ts})
            self.zones[zid] = new
            b.zones[new["id"]] = copy.deepcopy(new)
            self.counters["diff_events_total"] += len(b.events) - n0
        for pid in sorted(set(pv) | set(self.parts)):
            old, new = self.parts.get(pid), pv.get(pid)
            if new is None:
                if old and (name == PRIMARY or old.get("source") in (name, None) or pid in self.src[name].gone_p):
                    self.parts.pop(pid, None)
                    b.removed_partitions.append(old["id"])
                    b.events.append({"type": "partition.removed", "subject": old["id"], "from": old["arming"],
                                     "to": None, "source": name, "synthetic": syn, "ts": ts, "zone": pinfo(old)})
                elif old and not old["stale"]:
                    # a fallback snapshot does not vouch for a partition it does not own: keep it, flagged stale
                    rec = dict(old, stale=True)
                    self.parts[pid] = rec
                    b.partitions[rec["id"]] = copy.deepcopy(rec)
                    b.events.append({"type": "partition.stale", "subject": rec["id"], "from": False, "to": True,
                                     "source": rec.get("source"), "synthetic": syn, "ts": ts, "zone": pinfo(rec)})
                continue
            n0 = len(b.events)
            self._part_events(old, new, name, syn, ts, b.events)
            if old is None or old["arming"] != new["arming"]:
                new["last_changed"] = ts
            new.update({"source": name, "stale": False})
            self.parts[pid] = new
            b.partitions[new["id"]] = copy.deepcopy(new)
            self.counters["diff_events_total"] += len(b.events) - n0

    def _mark_stale(self, b: Batch) -> None:
        """Nobody vouches for the cache: flag every record stale (records are replaced, never mutated). Each zone flip
        is logged (zone.stale) in the same batch, so it has a true timestamp and seq."""
        ts = self._now()[2]
        for k, rec in list(self.zones.items()):
            if not rec["stale"]:
                rec = dict(rec, stale=True)
                self.zones[k] = rec
                b.zones[rec["id"]] = copy.deepcopy(rec)
                b.events.append({"type": "zone.stale", "subject": rec["id"], "from": False, "to": True,
                                 "source": rec.get("source"), "synthetic": False, "ts": ts, "zone": zinfo(rec)})
        for k, rec in list(self.parts.items()):
            if not rec["stale"]:
                rec = dict(rec, stale=True)
                self.parts[k] = rec
                b.partitions[rec["id"]] = copy.deepcopy(rec)
                b.events.append({"type": "partition.stale", "subject": rec["id"], "from": False, "to": True,
                                 "source": rec.get("source"), "synthetic": False, "ts": ts, "zone": pinfo(rec)})

    # ------------------------------------------------------------ arbitration
    def _want(self, mono: float) -> str | None:
        p, f = self.src[PRIMARY], self.src[FALLBACK]
        if p.enabled and p.live:
            # flap hold-down: while the fallback is published and live, the primary must have been continuously
            # live for `holddown` before we switch back (a primary that is up 200 ms every 6 s must not thrash us)
            if not (self.active == FALLBACK and f.enabled and f.live and p.live_since is not None
                    and mono - p.live_since < self.holddown):
                return PRIMARY
            return FALLBACK
        if f.enabled and f.live:
            if not p.enabled or self.active == FALLBACK:
                return FALLBACK
            if mono - (p.down_since if p.down_since is not None else mono) >= self.fallback_after:
                return FALLBACK
        return None

    def _compute_status(self) -> str:
        if self.active is None:
            return "offline"
        en = self._enabled()
        return "online" if en and self.active == en[0] else "degraded"

    def _sync_links(self, b: Batch) -> None:
        """One bridge.link event per real link-state change; derived from state, so a rolled-back batch re-emits."""
        _, wall, ts = self._now()
        for name in (PRIMARY, FALLBACK):
            s = self.src[name]
            if s.live != s.reported_live:
                s.reported_live = s.live
                s.link_since_wall = wall
                b.events.append({"type": "bridge.link", "subject": name, "from": "up" if not s.live else "down",
                                 "to": "up" if s.live else "down", "source": name, "synthetic": False, "ts": ts})

    def _arbitrate(self, b: Batch, trigger_snapshot_of: str | None = None, force: bool = False) -> None:
        self._sync_links(b)
        mono = self.clock.mono()
        want = self._want(mono)
        prev = self.active
        if want != prev:
            self.active = want
            b.meta["active"] = want
            if want is None:
                self._mark_stale(b)
            else:
                if prev is not None:
                    self.counters["failovers_total"] += 1
                elif want == FALLBACK:
                    self.counters["failovers_total"] += 1
                self._reconcile(want, b)
        elif want is not None and (trigger_snapshot_of == want or force):
            self._reconcile(want, b)
        elif want is None and force:
            self._mark_stale(b)
        st = self._compute_status()
        if st != self._status:
            self._status = st
            b.status = st

    # ------------------------------------------------------------ inputs
    def touch(self, name: str) -> None:
        """Any IPC frame (including the 1 s keep-alive ping) proves the adapter process is running and talking."""
        s = self.src.get(name)
        if s is not None:
            s.last_frame = self.clock.mono()

    def grant_grace(self) -> None:
        """The normalizer's own loop stalled (so unread frames are queued): do not blame the adapters for it."""
        t = self.clock.mono()
        for s in self.src.values():
            if s.last_frame is not None:
                s.last_frame = t

    def ipc_up(self, name: str) -> None:
        if name in self.src:
            self.src[name].ipc = True
            self.touch(name)

    def ipc_down(self, name: str) -> Batch:
        b = Batch()
        if name not in self.src:
            return b
        self._begin()
        s = self.src[name]
        s.ipc = False
        s.last_frame = None
        self._link_down(name, b)
        return b

    def _link_down(self, name: str, b: Batch) -> None:
        s = self.src[name]
        if s.live:
            s.live = False
            s.down_since = self.clock.mono()
            s.live_since = None
        self._arbitrate(b)

    def tick(self) -> Batch:
        b = Batch()
        self._begin()
        mono = self.clock.mono()
        for name, s in self.src.items():
            if s.live and s.last_frame is not None and mono - s.last_frame > self.ipc_deadline:
                # connection open but nothing (not even a keep-alive) for ipc_deadline: the adapter is hung/frozen.
                # Its shadow is no longer trustworthy; it stays down until it sends a fresh snapshot.
                self.counters["liveness_timeouts_total"] += 1
                s.live = False
                s.down_since = mono
                s.live_since = None
        force, self.resync_needed = self.resync_needed, False
        self._arbitrate(b, force=force)
        return b

    def handle(self, msg: dict) -> Batch:
        b = Batch()
        t = msg.get("t")
        name = msg.get("src")
        if name not in self.src:
            return b
        self._begin()
        s = self.src[name]
        _, wall, ts = self._now()
        s.last_frame = self.clock.mono()
        if t in ("snapshot", "zone", "zone_removed", "partition", "seen"):
            s.last_msg_wall = wall
            self.last_msg_wall = wall
        if t == "hello":
            s.ipc = True
        elif t == "stat":
            s.stats.update({k: v for k, v in msg.items() if k not in ("t", "src") and isinstance(v, (int, float))})
        elif t == "link":
            if msg.get("up"):
                pass  # liveness is established by the snapshot that follows; avoids live-without-state
            else:
                r = msg.get("reason")
                s.link_reason = r[:64] if isinstance(r, str) and re.fullmatch(r"[A-Za-z0-9_ .:-]{1,64}", r) else None
                self._link_down(name, b)
        elif t == "snapshot":
            if not (isinstance(msg.get("zones", []), list) and isinstance(msg.get("partitions", []), list)
                    and all(_valid_zone(z) for z in msg.get("zones", []))
                    and all(_valid_part(p) for p in msg.get("partitions", []))):
                return self._reject(name, "snapshot")
            self.counters["snapshots_total"] += 1
            zs = {int(z["panel_zone_id"]): dict(z) for z in msg.get("zones", [])}
            if msg.get("zones_complete") is False:
                # the adapter says this snapshot did not carry every zone (a partition came without its zone list):
                # what it lists is an update, what it omits is UNKNOWN, not deleted
                self.counters["partial_snapshots_total"] += 1
                zs = {**s.zones, **zs}
            if not zs and (s.zones or any(not self._ignored(k) for k in self.zones)):
                # an empty zone list while we know zones is far more likely a read glitch than a panel with every
                # sensor deleted: require 3 consecutive empties before believing it, never wipe on the first
                s.empty_snaps += 1
                if s.empty_snaps < 3:
                    self.counters["empty_snapshots_rejected_total"] += 1
                    return b
            s.empty_snaps = 0
            ps = {int(p["partition_id"]): dict(p) for p in msg.get("partitions", [])}
            s.gone_z = (s.gone_z | set(s.zones)) - set(zs)
            s.gone_p = (s.gone_p | set(s.parts)) - set(ps)
            s.zones, s.parts = zs, ps
            if name == PRIMARY:     # the primary re-vouches for these: a stale fallback "dropped" note no longer applies
                fb = self.src[FALLBACK]
                fb.gone_z -= set(zs)
                fb.gone_p -= set(ps)
            s.link_reason = None
            if not s.live:
                s.live = True
                s.down_since = None
                s.live_since = self.clock.mono()
            self._arbitrate(b, trigger_snapshot_of=name)
        elif t == "zone":
            z = msg.get("zone")
            if not _valid_zone(z):
                return self._reject(name, "zone delta")
            zid = int(z["panel_zone_id"])
            if self._ignored(zid):
                return b
            if zid not in s.zones and msg.get("new") is not True:
                # A delta for a zone this source's picture does not contain: a late update racing a delete, or an
                # id never seen. It must never CREATE a zone (a ghost with no type, name "Zone N"). Only an explicit
                # insert (`new: true`, from the adapter's own dbChanged insert) or a snapshot creates zones.
                self.counters["unknown_zone_deltas_total"] += 1
                self.resync_needed = True               # coalesced: one reconcile at the next tick however many arrive
                log.warning("%s: delta for unknown zone %s ignored; resync requested", name, zid)
                return b
            s.zones.setdefault(zid, {"panel_zone_id": zid}).update(z)
            s.gone_z.discard(zid)
            if self.active == name and s.live:
                self._apply_zone(zid, name, b)
        elif t == "zone_removed":
            try:
                zid = int(msg["panel_zone_id"])
            except (KeyError, TypeError, ValueError):
                return self._reject(name, "zone_removed")
            if s.zones.pop(zid, None) is not None:
                s.gone_z.add(zid)
            if self.active == name and s.live and zid in self.zones:
                old = self.zones.pop(zid)
                b.events.append({"type": "zone.removed", "subject": old["id"], "from": old["state"], "to": None,
                                 "source": name, "synthetic": False, "ts": ts, "zone": zinfo(old)})
                b.removed_zones.append(old["id"])
        elif t == "partition":
            p = msg.get("partition")
            if not _valid_part(p):
                return self._reject(name, "partition delta")
            pid = int(p["partition_id"])
            s.gone_p.discard(pid)
            s.parts.setdefault(pid, {"partition_id": pid}).update(p)
            if self.active == name and s.live:
                old = self.parts.get(pid)
                new = self._resolve_part(pid, s.parts[pid], old)
                self._part_events(old, new, name, False, ts, b.events)
                if old is None or old["arming"] != new["arming"]:
                    new["last_changed"] = ts
                new.update({"source": name, "stale": False})
                self.parts[pid] = new
                b.partitions[new["id"]] = copy.deepcopy(new)
        if b.events:
            self.counters["events_total"] += len(b.events)
        return b

    def _reject(self, name: str, what: str) -> Batch:
        self.counters["malformed_frames_total"] += 1
        log.warning("%s: malformed %s rejected at the normalizer boundary", name, what)
        return Batch()

    def _apply_zone(self, zid: int, name: str, b: Batch) -> None:
        _, _, ts = self._now()
        old = self.zones.get(zid)
        if self._hidden(self.src[name].zones[zid]):
            if old is not None:             # re-typed into a keypad: it leaves the published view
                self.zones.pop(zid)
                b.events.append({"type": "zone.removed", "subject": old["id"], "from": old["state"], "to": None,
                                 "source": name, "synthetic": False, "ts": ts, "zone": zinfo(old)})
                b.removed_zones.append(old["id"])
            return
        new = self._resolve_zone(zid, self.src[name].zones[zid], old)
        self._zone_events(old, new, name, False, ts, b.events)
        if old is None or old["state"] != new["state"]:
            new["last_changed"] = ts
        new.update({"source": name, "stale": False, "last_seen": ts})
        self.zones[zid] = new
        b.zones[new["id"]] = copy.deepcopy(new)

    # ------------------------------------------------------------ outputs
    @property
    def status(self) -> str:
        return self._status

    def health(self) -> dict:
        mono = self.clock.mono()

        def link(n):
            s = self.src[n]
            if not s.enabled:
                return "disabled"
            return "up" if s.live else "down"
        return {
            "status": self._status,
            "active_source": self.active,
            "links": {PRIMARY: link(PRIMARY), FALLBACK: link(FALLBACK)},
            "link_reasons": {n: s.link_reason for n, s in self.src.items() if s.link_reason and not s.live},
            "last_panel_message_epoch": self.last_msg_wall,
            "zones": len(self.zones),
            "partitions": len(self.parts),
            "stale_zones": sum(1 for z in self.zones.values() if z["stale"]),
            "counters": dict(self.counters),
            "adapter_stats": {n: dict(s.stats) for n, s in self.src.items()},
            "ipc_age_s": {n: (round(mono - s.last_frame, 2) if s.last_frame is not None else None)
                          for n, s in self.src.items()},
            "resync_pending": self.resync_needed,
        }
