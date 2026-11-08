"""Replay a past time window of Kismet's log through the console's pipeline.

Re-runs what the console does live (readings credited to transmitters ->
per-node smoothing -> server positions with the site's saved model, walls and
geo-reference -> baseline -> alert rules with the site's areas and marks) on
the Kismet logs, every POLL_S of log time, and writes what it would have
produced. Use it to tune rules or models against what was actually seen.
Reads site data only; writes only to --out.

    docker compose exec rf-console python replay.py --site default \\
        --start 2026-11-08T18:00:00Z --end 2026-11-08T20:00:00Z --out /data/replay

Writes ``positions.jsonl`` (each placed device per poll, when its estimate
had new readings), ``alerts.jsonl`` and ``summary.json``. TPMS (not in the
log) and node-offline alerts (need live capture-source state) are not
replayed.
"""

from __future__ import annotations

import argparse
import calendar
import json
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Iterator

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from alerting import AlertEngine, Marks, _read_json  # noqa: E402
from baseline import LEARN_S, Baseline  # noqa: E402
from locate import Locator, band_models, georeference, to_latlon  # noqa: E402
from server import (FRAME_HEADER_BYTES, LOG_BANDS, KismetLog, Tracker, frame_transmitter, load_layout)  # noqa: E402

POLL_S = 3.0
WINDOW_S = 180.0


def parse_time(s: str) -> float:
    try:
        return float(s)
    except ValueError:
        return float(calendar.timegm(time.strptime(s.rstrip("Z"), "%Y-%m-%dT%H:%M:%S")))


def readings(klog: KismetLog, start: float, end: float) -> Iterator[tuple[float, str, str, int]]:
    """(t, mac, node, rssi) in time order, Wi-Fi credited to the frame's transmitter (as LogTail does)."""
    rows: list[tuple[float, str, str, int]] = []

    def collect(db: Any) -> None:
        names = {u: str(n).split(":", 1)[0].lower() for u, n in db.execute("select uuid, name from datasources")}
        frames = klog._has_frames(db)
        q = (f"select ts_sec, ts_usec, lower(sourcemac), datasource, signal{', dlt, substr(packet, 1, ?)' if frames else ''} "
             "from packets where ts_sec between ? and ? and signal != 0")
        args = ((FRAME_HEADER_BYTES,) if frames else ()) + (int(start), int(end) + 1)
        for r in db.execute(q, args):
            mac = r[2]
            if frames:
                ta = frame_transmitter(r[5], r[6])
                if ta is None:
                    continue
                if ta is not False:
                    mac = ta
            node = names.get(r[3])
            if node and -130 <= r[4] < 0:
                rows.append((r[0] + (r[1] or 0) / 1e6, mac, node, r[4]))
        if klog._has_data_signal(db):
            for t, mac, src, sig in db.execute("select ts_sec, lower(devmac), datasource, signal from data "
                                               "where ts_sec between ? and ? and signal != 0", (int(start), int(end) + 1)):
                node = names.get(src)
                if node and -130 <= sig < 0:
                    rows.append((float(t), mac, node, sig))

    for f in klog._files():
        klog._read(f, collect)
    rows.sort()
    yield from rows


def device_json(klog: KismetLog) -> dict[str, dict[str, Any]]:
    """Kismet's own device records (as logged) by MAC: names, types, manufacturers."""
    out: dict[str, dict[str, Any]] = {}

    def collect(db: Any) -> None:
        for mac, blob in db.execute("select lower(devmac), device from devices"):
            try:
                out[mac] = json.loads(blob)
            except (TypeError, ValueError):
                continue

    for f in klog._files():
        klog._read(f, collect)
    return out


def replay(site_dir: Path, data_dir: Path | None, logs: Path, start: float, end: float, template: dict[str, Any],
           learn_s: float = LEARN_S) -> dict[str, Any]:
    site = json.loads((site_dir / "site.json").read_text())
    cal = _read_json(site_dir / "calibration.json", {})
    nodes = {k: [float(v[0]), float(v[1])] for k, v in site["layout"]["nodes"].items()}
    layout = {**template, "nodes": nodes, "models": site["layout"]["models"], "geo": site.get("geo")}
    models = band_models(layout, cal.get("fits", {}))
    locator = Locator()
    locator.configure(nodes, cal.get("walls", []), models)
    tf = georeference(nodes, layout.get("geo"))
    areas = _read_json(site_dir / "areas.json", [])
    marks = Marks(data_dir / "marks.json" if data_dir else None).merged(site["id"], site_dir / "marks.json")
    radios = set(layout.get("self_emitters", {}).values())
    klog = KismetLog(logs)
    devjson = device_json(klog)
    tracker, engine = Tracker(WINDOW_S), AlertEngine()
    base = Baseline(None, learn_s)
    positions_out: list[dict[str, Any]] = []
    alerts_out: list[dict[str, Any]] = []
    placed: set[str] = set()
    it = readings(klog, start - WINDOW_S, end)
    pending: list[tuple[float, str, str, int]] = []
    nxt = next(it, None)
    now = start
    polls = 0
    while now <= end:
        while nxt is not None and nxt[0] <= now:
            pending.append(nxt)
            nxt = next(it, None)
        macs = {r[1] for r in pending}
        missing = [devjson[m] for m in macs if m not in tracker.meta and m in devjson]
        tracker.update(missing, now, log_bands=LOG_BANDS, prune=False)
        tracker.add_log([(m, n, t, s) for t, m, n, s in pending], now)
        pending = []
        tracker.prune(now)
        snap = [d for d in tracker.snapshot(now) if d["band"] in LOG_BANDS]
        by_id = {d["id"]: d for d in snap}
        est = {k: e for k, e in locator.locate_all(snap, nodes).items() if e["kind"] == "fix"}
        pos = {}
        for k, e in est.items():
            p = {"x": round(e["x"], 2), "y": round(e["y"], 2), "nodes": e["K"], "ids": e["ids"], "updated": e["updated"]}
            if tf:
                p["lat"], p["lon"] = (round(v, 7) for v in to_latlon(tf, e["x"], e["y"]))
            pos[k] = p
            if e["updated"]:
                positions_out.append({"t": round(now, 1), "id": k, "band": by_id[k]["band"], **{q: p[q] for q in p if q != "updated"}})
                placed.add(k)
        base.observe(snap, now)
        for a in engine.evaluate(site["id"], now, by_id, pos, marks, areas, {}, radios):
            alerts_out.append({**a, "name": (by_id.get(a["device"]) or {}).get("name") if a["device"] else None})
        now += POLL_S
        polls += 1
    return {"site": site["id"], "start": start, "end": end, "polls": polls, "devices_placed": len(placed),
            "positions": positions_out, "alerts": alerts_out,
            "alerts_by_rule": dict(Counter(a["rule_id"] for a in alerts_out)), "model": {b: m["calibrated"] for b, m in models.items()}}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--site", required=True, help="site id (directory under <data>/sites)")
    ap.add_argument("--start", required=True, help="UTC, e.g. 2026-11-08T18:00:00Z, or unix seconds")
    ap.add_argument("--end", required=True)
    ap.add_argument("--data", default="/data")
    ap.add_argument("--logs", default="/kismet-data/logs")
    ap.add_argument("--layout", default="/site/layout.yaml", help="team template (node radios)")
    ap.add_argument("--learn-s", type=float, default=LEARN_S, help="baseline learning period from --start")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    template_path = Path(a.layout) if Path(a.layout).is_file() else HERE / "layout.example.yaml"
    res = replay(Path(a.data) / "sites" / a.site, Path(a.data), Path(a.logs), parse_time(a.start), parse_time(a.end),
                 load_layout(template_path), a.learn_s)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    for name in ("positions", "alerts"):
        with open(out / f"{name}.jsonl", "w") as f:
            for rec in res[name]:
                f.write(json.dumps(rec, separators=(",", ":")) + "\n")
    summary = {k: v for k, v in res.items() if k not in ("positions", "alerts")}
    (out / "summary.json").write_text(json.dumps(summary, indent=1))
    print(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
