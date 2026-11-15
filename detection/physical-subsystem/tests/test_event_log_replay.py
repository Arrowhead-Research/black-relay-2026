"""Seeded model-based test of the normalizer (Engine + NormalizerService persistence, no sockets): random panel
changes, link flaps, hung adapters, partial/glitched snapshots, restarts. After every step the events written during
that step, applied to the cache as it was before, must give EXACTLY the cache after the step (zones: name, kind,
partition, state, tamper, battery_low, online, stale; partitions: name, arming, alarm_type, stale), and at every
quiescent point replaying the whole log from seq 0 must reproduce the cache exactly. No allowances: every event's
own `zone` block (state as of the event) must also equal the replayed state.
Seeds and step counts are fixed so the run is deterministic and bounded (see SEEDS/STEPS)."""
import logging
import os
import random
import shutil
import json
import tempfile
import traceback

import pytest

from br2 import config as C, model as M
from br2.normalizer.core import Engine, Clock, PRIMARY, FALLBACK
from br2.normalizer.service import NormalizerService

SEEDS = range(1, 25)
STEPS = 200

class Fail(Exception):
    pass


SRC = (PRIMARY, FALLBACK)
STATUS = ["Open", "Closed", "Active", "Idle", "Unreachable", "Tampered"]
TYPES = [("Door_Window", "entryexitdelay"), ("Motion", "instantinterior"), ("GlassBreak", "glassbreak"), ("Smoke_Detector", "fire")]
BATT = ["Normal", "Low"]
PSTAT = ["DISARM", "ARM_STAY", "ARM_AWAY", "ENTRY_DELAY"]


class FC(Clock):
    def __init__(s): s.t = 1000.0
    def mono(s): return s.t
    def wall(s): return 1.8e9 + s.t


ZKEYS = ("name", "kind", "partition_id", "state", "tamper", "battery_low", "online", "stale")
PKEYS = ("name", "arming", "alarm_type", "stale")


def proj(zones):
    return {z["id"]: {k: z[k] for k in ZKEYS} for z in zones.values()}


def projp(parts):
    return {p["id"]: {k: p[k] for k in PKEYS} for p in parts.values()}


def _zrow(zi):
    return {"name": zi.get("name"), "kind": zi.get("kind"), "partition_id": zi.get("partition"), "state": zi.get("state"),
            "tamper": zi.get("tamper"), "battery_low": zi.get("battery_low"), "online": zi.get("online"),
            "stale": zi.get("stale")}


ZFIELD = {"zone.state": "state", "zone.tamper": "tamper", "zone.battery": "battery_low", "zone.online": "online",
          "zone.stale": "stale", "zone.renamed": "name", "zone.kind": "kind", "zone.partition": "partition_id"}
PFIELD = {"partition.arming": "arming", "partition.alarm": "alarm_type", "partition.stale": "stale",
          "partition.renamed": "name"}


def reduce_events(cache, evs):
    """Apply log events to a (zones, partitions) projection. Every transition must chain from the current value and the
    event's own as-of-event block must equal the result."""
    zc = {k: dict(v) for k, v in cache[0].items()}
    pc = {k: dict(v) for k, v in cache[1].items()}
    for e in evs:
        t, sub, fr, to, zi = e["type"], e["subject"], e["from"], e["to"], e.get("zone")
        at = f"seq {e['seq']} {t} {sub}"
        if t.startswith("zone."):
            if not isinstance(zi, dict): raise Fail(f"{at}: no zone block")
            if t == "zone.added":
                if sub in zc: raise Fail(f"{at}: added for existing zone")
                zc[sub] = _zrow(zi)
                if fr is not None or to != zc[sub]["state"] or zc[sub]["stale"] is not False: raise Fail(f"{at}: bad added {fr!r}->{to!r} {zi}")
                if None in zc[sub].values(): raise Fail(f"{at}: zone.added block incomplete {zi}")
                continue
            if sub not in zc: raise Fail(f"{at}: unknown zone")
            c = zc[sub]
            if t == "zone.removed":
                if fr != c["state"] or to is not None: raise Fail(f"{at}: removed from={fr} cache state={c['state']}")
                if _zrow(zi) != c: raise Fail(f"{at}: removed block {zi} != cache {c}")
                del zc[sub]
                continue
            fld = ZFIELD.get(t)
            if fld is None: raise Fail(f"{at}: unknown event type")
            if c[fld] != fr: raise Fail(f"{at}: from={fr!r} but chain value={c[fld]!r}")
            if fr == to: raise Fail(f"{at}: no-op event {fr!r}->{to!r}")
            c[fld] = to
            if _zrow(zi) != c: raise Fail(f"{at}: block {zi} != replayed {c}")
        elif t.startswith("partition."):
            if not isinstance(zi, dict): raise Fail(f"{at}: no partition block")
            row = {k: zi.get(k) for k in PKEYS}
            if t == "partition.added":
                if sub in pc: raise Fail(f"{at}: added for existing partition")
                if zi.get("stale") is not False or zi.get("arming") != to or fr is not None: raise Fail(f"{at}: bad added")
                pc[sub] = row
                continue
            if sub not in pc: raise Fail(f"{at}: unknown partition")
            c = pc[sub]
            if t == "partition.removed":
                if fr != c["arming"] or to is not None or row != c: raise Fail(f"{at}: bad removed {zi} vs {c}")
                del pc[sub]
                continue
            fld = PFIELD.get(t)
            if fld is None: raise Fail(f"{at}: unknown event type")
            if c[fld] != fr: raise Fail(f"{at}: from={fr!r} but chain value={c[fld]!r}")
            if fr == to: raise Fail(f"{at}: no-op event")
            c[fld] = to
            if row != c: raise Fail(f"{at}: block {zi} != replayed {c}")
        elif t != "bridge.link":
            raise Fail(f"{at}: unknown event family")
    return zc, pc


class Sim:
    def __init__(self, seed_actions):
        self.d = tempfile.mkdtemp(prefix="mt", dir="/dev/shm" if os.path.isdir("/dev/shm") else None)
        self.clock = FC()
        self.cfg = C.parse({"iq_remote": {"enabled": True, "panel_ip": "1.1.1.1"}, "control4": {"enabled": True, "host": "h"},
                            "timers": {"fallback_after_s": 10, "ipc_liveness_s": 5, "failback_holddown_s": 3},
                            "storage": {"data_dir": self.d, "retention_days": 30}})
        self.panel_z, self.panel_p = {}, {}
        self.nextz = 1
        self.conn = {s: False for s in SRC}       # adapter linked & talking
        self.hung = {s: False for s in SRC}
        self.live = {s: False for s in SRC}       # model of engine "live": accepted snapshot since connect
        self.bel = {s: ({}, {}) for s in SRC}     # oracle shadow == what the engine should hold per source
        self.empties = {s: 0 for s in SRC}
        self.exact = {s: False for s in SRC}      # shadow provably == panel truth (no partial/glitch since last full)
        self.pdirty = {s: False for s in SRC}
        self.last_cache = None
        self.last_seq = 0
        self.log = []
        self.start_svc()
        for _ in range(3): self.add_zone(None)
        self.panel_p[0] = {"partition_id": 0, "name": "Main", "status": "DISARM", "alarm_state": "", "alarm_type": None}

    # ---- service plumbing
    def start_svc(self):
        self.svc = NormalizerService(self.cfg, ipc_dir=self.d, engine=Engine(self.cfg, self.clock))
        self.eng = self.svc.engine
        pre = self.last_cache
        self.persist(self.svc._restore_batch)
        if pre is not None:   # restore must reproduce what consumers saw, stale
            now = (proj(self.eng.zones), projp(self.eng.parts))
            want = ({k: dict(v, stale=True) for k, v in pre[0].items()}, {k: dict(v, stale=True) for k, v in pre[1].items()})
            if now != want: raise Fail(f"restore changed the cache: {diff(want[0], now[0]) + diff(want[1], now[1])}")
        for s in SRC: self.live[s] = False; self.conn[s] = False; self.hung[s] = False
        self.empties = {s: 0 for s in SRC}
        self.bel = {s: ({}, {}) for s in SRC} if pre is None else self.bel  # engine shadows start empty
        self.bel = {s: ({}, {}) for s in SRC}
        self.exact = {s: False for s in SRC}

    def persist(self, b):
        self.svc.process_batch(b)

    def feed(self, m):
        m = dict(m)
        self.eng.touch(m["src"])
        self.persist(self.eng.handle(m))

    def tick(self):
        self.persist(self.eng.tick())

    def advance(self, dt, touch=True):
        self.clock.t += dt
        if touch:
            for s in SRC:
                if self.conn[s] and not self.hung[s]: self.eng.touch(s)
        self.tick()

    # ---- panel truth
    def obs(self, zid):
        return dict(self.panel_z[zid])

    def add_zone(self, r):
        zid = self.nextz; self.nextz += 1
        r = random.Random(r) if r is not None else random.Random(zid)
        t, g = r.choice(TYPES)
        self.panel_z[zid] = {"panel_zone_id": zid, "name": f"S{zid}", "type": t, "group": g, "status": r.choice(STATUS),
                             "battery": r.choice(BATT), "partition_id": 0, "raw": {}}
        return zid

    def pick(self, r, d):
        ks = sorted(d); return ks[int(r * len(ks)) % len(ks)] if ks else None

    # ---- adapter -> engine frames
    def deliver(self, frame_for):
        for s in SRC:
            if self.conn[s]:
                fr = frame_for(s)
                if fr is not None:
                    fr = dict(fr, src=s)
                    self.feed(fr)
                    self.model_frame(s, fr)

    def model_frame(self, s, fr):
        z, p = self.bel[s]
        t = fr["t"]
        if t == "zone":
            d = fr["zone"]; zid = d["panel_zone_id"]
            if zid not in z and fr.get("new") is not True: return
            z.setdefault(zid, {"panel_zone_id": zid}).update(d)
        elif t == "zone_removed":
            z.pop(fr["panel_zone_id"], None)
        elif t == "partition":
            d = fr["partition"]; p.setdefault(d["partition_id"], {"partition_id": d["partition_id"]}).update(d)

    def snapshot_frame(self, s, mode):
        z, p = self.bel[s]
        zs = [self.obs(k) for k in sorted(self.panel_z)]
        ps = [dict(v) for v in self.panel_p.values()]
        fr = {"t": "snapshot", "zones": zs, "partitions": ps}
        if mode == "partial":
            keep = [x for x in zs if x["panel_zone_id"] % 2 == 0]
            fr = {"t": "snapshot", "zones": keep, "partitions": ps, "zones_complete": False}
        elif mode == "glitch":
            fr = {"t": "snapshot", "zones": [], "partitions": ps}
        return fr

    def send_snapshot(self, s, mode):
        fr = dict(self.snapshot_frame(s, mode), src=s)
        z, p = self.bel[s]
        zs = {x["panel_zone_id"]: dict(x) for x in fr["zones"]}
        if fr.get("zones_complete") is False: zs = {**z, **zs}
        self.feed(fr)
        if not zs and (z or any(True for _ in self.eng.zones) and False):
            pass
        # oracle replicate of the documented empty rule: an empty zone list while zones are known needs 3 in a row
        known = bool(z) or bool(self._oracle_cache_zone_ids)
        if not zs and known:
            self.empties[s] += 1
            if self.empties[s] < 3:
                return
        self.empties[s] = 0
        self.bel[s] = (zs, {x["partition_id"]: dict(x) for x in fr["partitions"]})
        self.live[s] = True
        self.exact[s] = (mode == "full") or (mode == "glitch" and not self.panel_z)
        self.pdirty[s] = False

    @property
    def _oracle_cache_zone_ids(self):
        return set(self.eng.zones)  # cache BEFORE the snapshot was applied is what the rule reads; computed by caller below

    # ---- actions
    def do(self, a):
        op = a[0]
        if op == "connect":
            _, s, r = a
            if self.conn[s]: return
            self.conn[s] = True; self.hung[s] = False
            self._snap(s, "partial" if r < 0.15 else "full")
        elif op == "resync":
            _, s, r = a
            if not (self.conn[s] and self.live[s]): return
            self._snap(s, "glitch" if r < 0.25 and self.panel_z else ("partial" if r < 0.4 else "full"))
        elif op == "disconnect":
            _, s, r = a
            if not self.conn[s]: return
            self.conn[s] = False
            if r < 0.25:   # hung adapter: no frame at all, engine finds out by liveness
                self.hung[s] = True
            elif r < 0.5:
                self.persist(self.eng.ipc_down(s)); self.live[s] = False
            else:
                self.feed({"t": "link", "up": False, "src": s}); self.live[s] = False
        elif op == "mut":
            self._mut(*a[1:])
        elif op == "unknown":
            _, r = a
            ghost = 5000 + int(r * 100)
            self.deliver(lambda s: {"t": "zone", "zone": {"panel_zone_id": ghost, "status": "Open"}})
            self.deliver(lambda s: {"t": "zone_removed", "panel_zone_id": ghost})
        elif op == "advance":
            self.advance(a[1])
        elif op == "restart":
            self.last_cache = (proj(self.eng.zones), projp(self.eng.parts))
            self.svc.store.close()
            self.start_svc()
        elif op == "settle":
            self.settle()

    def _snap(self, s, mode):
        # oracle needs the cache state as it stood BEFORE the engine processed this snapshot
        self._pre_zones = set(self.eng.zones)
        fr = dict(self.snapshot_frame(s, mode), src=s)
        z, p = self.bel[s]
        zs = {x["panel_zone_id"]: dict(x) for x in fr["zones"]}
        if fr.get("zones_complete") is False: zs = {**z, **zs}
        known = bool(z) or any(not False for _ in self._pre_zones)
        self.feed(fr)
        if not zs and known:
            self.empties[s] += 1
            if self.empties[s] < 3:
                return
        self.empties[s] = 0
        self.bel[s] = (zs, {x["partition_id"]: dict(x) for x in fr["partitions"]})
        self.live[s] = True
        self.exact[s] = (mode == "full") or (mode == "glitch" and not self.panel_z)
        self.pdirty[s] = False

    def _mut(self, kind, r, r2):
        P = self.panel_z
        zid = self.pick(r, P)
        if kind == "status" and zid:
            P[zid]["status"] = STATUS[int(r2 * 6) % 6]
            self.deliver(lambda s: {"t": "zone", "zone": {"panel_zone_id": zid, "status": P[zid]["status"]}})
        elif kind == "battery" and zid:
            P[zid]["battery"] = BATT[int(r2 * 2) % 2]
            self.deliver(lambda s: {"t": "zone", "zone": {"panel_zone_id": zid, "battery": P[zid]["battery"]}})
        elif kind == "rename" and zid:
            P[zid]["name"] = f"N{int(r2 * 1000)}"
            self.deliver(lambda s: {"t": "zone", "zone": {"panel_zone_id": zid, "name": P[zid]["name"]}})
        elif kind == "retype" and zid:
            t, g = TYPES[int(r2 * 4) % 4]; P[zid]["type"], P[zid]["group"] = t, g
            self.deliver(lambda s: {"t": "zone", "zone": {"panel_zone_id": zid, "type": t, "group": g}})
        elif kind == "insert":
            nz = self.add_zone(int(r2 * 1e6))
            self.deliver(lambda s: {"t": "zone", "zone": self.obs(nz), "new": True})
        elif kind == "delete" and zid:
            del P[zid]
            self.deliver(lambda s: {"t": "zone_removed", "panel_zone_id": zid})
        elif kind == "pstat":
            pid = 0
            st = PSTAT[int(r2 * 4) % 4]
            al = "Alarm" if r2 > 0.9 else ""
            self.panel_p[pid].update(status=st, alarm_state=al, alarm_type="Police" if al else None)
            d = dict(self.panel_p[pid])
            self.deliver(lambda s: {"t": "partition", "partition": dict(d)})
        elif kind == "prename":
            ks = sorted(self.panel_p)
            pid = ks[int(r * len(ks)) % len(ks)]
            self.panel_p[pid]["name"] = f"PN{int(r2 * 1000)}"
            d = dict(self.panel_p[pid])
            self.deliver(lambda s: {"t": "partition", "partition": dict(d)})
        elif kind == "zpart" and zid:
            P[zid]["partition_id"] = int(r2 * 3)
            self.deliver(lambda s: {"t": "zone", "zone": {"panel_zone_id": zid, "partition_id": P[zid]["partition_id"]}})
        elif kind == "padd":
            pid = 1 + int(r2 * 2)
            if pid in self.panel_p: return
            self.panel_p[pid] = {"partition_id": pid, "name": f"P{pid}", "status": "DISARM", "alarm_state": "", "alarm_type": None}
            d = dict(self.panel_p[pid])
            self.deliver(lambda s: {"t": "partition", "partition": dict(d)})
        elif kind == "premove":
            ks = [k for k in self.panel_p if k != 0]
            if not ks: return
            del self.panel_p[ks[int(r2 * len(ks)) % len(ks)]]
            for s in SRC: self.pdirty[s] = True      # adapters emit no partition-removed delta: only a snapshot says so

    # ---- quiescence + invariants
    def settle(self):
        for _ in range(14): self.advance(2.0)
        self.check_quiescent()

    def expect_zone(self, o):
        kind = M.classify(o.get("type"), o.get("group"))
        state, online = M.map_state(kind, o.get("status"))
        return {"name": o.get("name"), "kind": kind, "state": state, "online": online,
                "tamper": M._norm(o.get("status")) == "tampered", "battery_low": M.map_battery(o.get("battery")),
                "partition_id": int(o.get("partition_id") or 0)}

    def check_quiescent(self):
        e = self.eng
        live = [s for s in SRC if self.live[s] and self.conn[s] and not self.hung[s]]
        want = PRIMARY if PRIMARY in live else (FALLBACK if FALLBACK in live else None)
        if e.active != want:
            raise Fail(f"active={e.active} expected {want} (live={live}) after settle")
        if want is None:
            if any(not z["stale"] for z in e.zones.values()): raise Fail("no live source but a fresh zone remains")
            if any(not p["stale"] for p in e.parts.values()): raise Fail("no live source but a fresh partition remains")
            if e.status != "offline": raise Fail(f"status {e.status} with no active source")
        else:
            bz, bp = self.bel[want]
            if set(e.zones) != set(bz): raise Fail(f"zone ids cache={sorted(e.zones)} shadow[{want}]={sorted(bz)}")
            for zid, o in bz.items():
                ex, got = self.expect_zone(o), e.zones[zid]
                for k, v in ex.items():
                    if k in ("state", "online") and M._norm(o.get("status")) == "tampered": continue
                    if got[k] != v and not (k in ("state", "online", "battery_low") and v is None):
                        raise Fail(f"zone {zid} {k}: cache={got[k]!r} expected={v!r} (obs={o})")
                if got["stale"]: raise Fail(f"zone {zid} stale under live active source")
                if got["source"] != want: raise Fail(f"zone {zid} source {got['source']} != {want}")
            if self.exact[want]:
                if set(e.zones) != set(self.panel_z): raise Fail(f"exact source {want}: cache ids {sorted(e.zones)} != panel {sorted(self.panel_z)}")
                for zid, o in self.panel_z.items():
                    ex, got = self.expect_zone(o), e.zones[zid]
                    for k, v in ex.items():
                        if k in ("state", "online", "battery_low") and (v is None or M._norm(o.get("status")) == "tampered"): continue
                        if got[k] != v: raise Fail(f"PANEL TRUTH zone {zid} {k}: cache={got[k]!r} panel={v!r}")
            for pid, o in bp.items():
                got = e.parts.get(pid)
                if got is None: raise Fail(f"partition {pid} missing from cache")
                arm = M.map_arming(o.get("status"), o.get("alarm_state"))
                at = o.get("alarm_type") if arm == "alarm" else None
                if arm == "alarm" and not at: at = "unknown"
                if (got["arming"], got["alarm_type"]) != (arm, at): raise Fail(f"partition {pid}: cache={(got['arming'], got['alarm_type'])} expected={(arm, at)}")
                if got["stale"]: raise Fail(f"partition {pid} stale under live source")
            extra = set(e.parts) - set(bp)
            for pid in extra:
                if not e.parts[pid]["stale"]:
                    raise Fail(f"partition {pid} not in shadow[{want}] but cache holds it FRESH")
            if want == PRIMARY and extra: raise Fail(f"primary snapshot is authoritative but partitions {sorted(extra)} remain")
            if self.exact[want] and not self.pdirty[want] and want == PRIMARY and set(e.parts) != set(self.panel_p):
                raise Fail(f"partitions cache={sorted(e.parts)} panel={sorted(self.panel_p)}")
        self.check_persisted()
        self.check_replay()

    def check_persisted(self):
        z, p = self.svc.store.load_cache()
        if {r["id"]: r for r in z} != {r["id"]: r for r in self.eng.zones.values()}: raise Fail("persisted zone cache != memory")
        if {r["id"]: r for r in p} != {r["id"]: r for r in self.eng.parts.values()}: raise Fail("persisted partition cache != memory")

    def all_events(self, after=0):
        db = self.svc.store.db
        rows = db.execute("SELECT seq,id,type,subject,from_json,to_json,zone_json FROM events WHERE seq>? ORDER BY seq", (after,)).fetchall()
        return [{"seq": r[0], "id": r[1], "type": r[2], "subject": r[3], "from": json.loads(r[4]), "to": json.loads(r[5]),
                 "zone": json.loads(r[6]) if r[6] else None} for r in rows]

    def check_replay(self):
        evs = self.all_events()
        seqs = [e["seq"] for e in evs]
        if seqs != list(range(1, len(seqs) + 1)): raise Fail(f"seq not gap-free/monotonic: {seqs[:3]}..{seqs[-3:]}")
        if any(e["id"] != f"evt-{e['seq']}" for e in evs): raise Fail("event id != evt-<seq>")
        got = reduce_events(({}, {}), evs)
        want = (proj(self.eng.zones), projp(self.eng.parts))
        if got != want: raise Fail(f"log replay != cache: {diff(want[0], got[0]) + diff(want[1], got[1])}")

    def check_step(self, before, a):
        """per-step: events written during the step, applied to `before`, give the cache after the step."""
        evs = self.all_events(self.last_seq)
        if evs and evs[0]["seq"] != self.last_seq + 1: raise Fail("gap at step start")
        if [e["seq"] for e in evs] != list(range(self.last_seq + 1, self.last_seq + 1 + len(evs))): raise Fail("gap inside step")
        self.last_seq += len(evs)
        got = reduce_events(before, evs)
        want = (proj(self.eng.zones), projp(self.eng.parts))
        if got != want: raise Fail(f"step {a}: events do not explain the cache change: {diff(want[0], got[0]) + diff(want[1], got[1])}")
        self.check_persisted()

    def run(self, actions):
        try:
            for i, a in enumerate(actions):
                before = (proj(self.eng.zones), projp(self.eng.parts))
                if a[0] == "restart":
                    self.last_seq = self.svc.store.last_seq()
                self.do(a)
                if a[0] == "restart":
                    # restore's own events are the step
                    pass
                self.check_step(before, a)
            before = (proj(self.eng.zones), projp(self.eng.parts)); self.settle(); self.check_step(before, ("end",))
            return None
        except Fail as e:
            return str(e)
        except Exception:
            return "EXC " + traceback.format_exc()[-600:]
        finally:
            try: self.svc.store.close()
            except Exception: pass
            shutil.rmtree(self.d, ignore_errors=True)


def diff(a, b):
    out = []
    for k in sorted(set(a) | set(b)):
        if a.get(k) != b.get(k): out.append((k, a.get(k), b.get(k)))
    return out[:3]


def gen(seed, n):
    r = random.Random(seed)
    acts = []
    for _ in range(n):
        x = r.random()
        if x < 0.10: acts.append(("connect", r.choice(SRC), r.random()))
        elif x < 0.18: acts.append(("disconnect", r.choice(SRC), r.random()))
        elif x < 0.24: acts.append(("resync", r.choice(SRC), r.random()))
        elif x < 0.64:
            acts.append(("mut", r.choice(["status"] * 4 + ["battery", "rename", "retype", "insert", "insert", "delete", "pstat", "padd", "premove", "prename", "zpart"]), r.random(), r.random()))
        elif x < 0.68: acts.append(("unknown", r.random()))
        elif x < 0.80: acts.append(("advance", r.choice([0.3, 1, 3, 6, 12])))
        elif x < 0.85: acts.append(("restart",))
        else: acts.append(("settle",))
    return acts


@pytest.mark.parametrize("seed", SEEDS)
def test_replaying_the_log_from_seq_0_reproduces_the_cache_exactly(seed):
    logging.disable(logging.CRITICAL)
    try:
        acts = gen(seed, STEPS)
        failure = Sim(acts).run(acts)
    finally:
        logging.disable(logging.NOTSET)
    assert failure is None, f"seed {seed}: {failure}"
