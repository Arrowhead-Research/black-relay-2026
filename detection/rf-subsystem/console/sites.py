"""Site workspaces for the WBT Console.

Every building the system is set up in is a *site*. Each site has its own
directory under ``<root>/sites/<id>/``, holding everything learned there:

    site.json                  name, node positions, node GPS points (geo), retention, active periods
    site.journal.jsonl         every change to site.json (who/when/before/after)
    calibration.json(+journal) calibration spots, fitted models, walls
    history/                   per-day history, e.g. <date>-positions.jsonl (purged after retention)

Nothing site-specific lives outside that directory, so wiping a site removes
it completely. Node hardware (each node's own radio addresses) belongs to
the team, not a site, and stays in the team template.

Older single-site data (``<root>/<name>/calibration.json``) is moved into
``<root>/sites/<name>/`` on start.
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import threading
import time
import zipfile
from pathlib import Path
from typing import Any, Callable

SITE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
DEFAULT_RETENTION_DAYS = 7
MAX_RETENTION_DAYS = 90
MAX_SITES = 50
MAX_NODES = 32
NODE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$")
HISTORY_FILE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})")  # history files start with their UTC date


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40].strip("-")
    return s or "site"


class Site:
    """One site: its document (site.json) and its calibration store."""

    def __init__(self, directory: Path, doc: dict[str, Any], cal: Any) -> None:
        self.dir = directory
        self.doc = doc
        self.cal = cal

    @property
    def id(self) -> str:
        return self.doc["id"]

    @property
    def history_dir(self) -> Path:
        return self.dir / "history"

    def summary(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.doc["name"], "created": self.doc["created"],
                "retention_days": self.doc["retention_days"], "nodes": len(self.doc["layout"]["nodes"]),
                "spots": len(self.cal.data.get("points", [])), "walls": len(self.cal.data.get("walls", [])),
                "history_bytes": sum(f.stat().st_size for f in self.history_dir.glob("**/*") if f.is_file())
                if self.history_dir.exists() else 0}


class SiteManager:
    def __init__(self, root: str | Path, template: dict[str, Any], cal_factory: Callable[[Path], Any],
                 initial: str | None = None) -> None:
        """``template``: a loaded layout (nodes, node_sd_ft, models, self_emitters)
        used for the first site and for node hardware; ``cal_factory(path)``
        opens a calibration store."""
        self.root = Path(root)
        self.sites_dir = self.root / "sites"
        self.template = template
        self.cal_factory = cal_factory
        self.lock = threading.RLock()
        self.sites: dict[str, Site] = {}
        self.version = 0  # bumps on any site change (switch, nodes, rename...)
        self.sites_dir.mkdir(parents=True, exist_ok=True)
        self._migrate_legacy()
        for d in sorted(self.sites_dir.iterdir()):
            if d.is_dir() and SITE_ID_RE.match(d.name):
                self._open(d)
        active = self._read_active()
        if active not in self.sites:
            active = slugify(initial) if initial else None
            if active not in self.sites:
                active = next(iter(self.sites), None) or self.create(initial or "default", activate=False)["id"]
            self._write_active(active)
        self.active_id = active
        self._period_start(active)

    # ---------- storage ----------
    def _migrate_legacy(self) -> None:
        for d in sorted(self.root.iterdir()):
            if d.is_dir() and d.name != "sites" and (d / "calibration.json").exists():
                target = self.sites_dir / slugify(d.name)
                if target.exists():
                    continue
                shutil.move(str(d), str(target))

    def _doc_path(self, d: Path) -> Path:
        return d / "site.json"

    def _new_doc(self, site_id: str, name: str, layout: dict[str, Any]) -> dict[str, Any]:
        return {"id": site_id, "name": name, "created": time.time(), "retention_days": DEFAULT_RETENTION_DAYS,
                "layout": {"nodes": {k: list(v) for k, v in layout["nodes"].items()},
                           "node_sd_ft": layout.get("node_sd_ft", 2.0),
                           "models": json.loads(json.dumps(layout["models"]))},
                "periods": []}

    def _open(self, d: Path) -> Site:
        path = self._doc_path(d)
        if path.exists():
            doc = json.loads(path.read_text())
        else:  # migrated legacy site: it was laid out by the template file
            doc = self._new_doc(d.name, d.name, self.template)
            self._save(d, doc, {"op": "create", "from": "legacy"})
        doc["id"] = d.name
        site = Site(d, doc, self.cal_factory(d / "calibration.json"))
        self.sites[d.name] = site
        return site

    def _save(self, d: Path, doc: dict[str, Any], entry: dict[str, Any]) -> None:
        d.mkdir(parents=True, exist_ok=True)
        with open(d / "site.journal.jsonl", "a") as j:
            j.write(json.dumps({"t": time.time(), **entry}) + "\n")
        tmp = d / "site.json.tmp"
        tmp.write_text(json.dumps(doc, indent=1))
        os.replace(tmp, self._doc_path(d))
        self.version += 1

    def _read_active(self) -> str | None:
        try:
            return json.loads((self.root / "active.json").read_text()).get("site")
        except (OSError, ValueError):
            return None

    def _write_active(self, site_id: str) -> None:
        tmp = self.root / "active.json.tmp"
        tmp.write_text(json.dumps({"site": site_id}))
        os.replace(tmp, self.root / "active.json")

    def _period_start(self, site_id: str) -> None:
        site = self.sites[site_id]
        periods = site.doc.setdefault("periods", [])
        if not periods or periods[-1][1] is not None:
            periods.append([time.time(), None])
            self._save(site.dir, site.doc, {"op": "period-start"})

    def _period_end(self, site_id: str) -> None:
        site = self.sites.get(site_id)
        if site and site.doc.get("periods") and site.doc["periods"][-1][1] is None:
            site.doc["periods"][-1][1] = time.time()
            self._save(site.dir, site.doc, {"op": "period-end"})

    # ---------- queries ----------
    def active(self) -> Site:
        with self.lock:
            return self.sites[self.active_id]

    def get(self, site_id: str) -> Site:
        with self.lock:
            if site_id not in self.sites:
                raise KeyError(site_id)
            return self.sites[site_id]

    def list(self) -> list[dict[str, Any]]:
        with self.lock:
            return [{**s.summary(), "active": s.id == self.active_id}
                    for s in sorted(self.sites.values(), key=lambda s: -s.doc["created"])]

    def layout(self) -> dict[str, Any]:
        """The active site's layout in the shape the page and tracker use."""
        site = self.active()
        lay = site.doc["layout"]
        nodes = {k: [float(v[0]), float(v[1])] for k, v in lay["nodes"].items()}
        xs, ys = [p[0] for p in nodes.values()] or [0], [p[1] for p in nodes.values()] or [0]
        return {
            "name": site.doc["name"],
            "units": "ft",
            "nodes": nodes,
            "outline": [[min(xs), min(ys)], [max(xs), min(ys)], [max(xs), max(ys)], [min(xs), max(ys)]],
            # Node hardware (radio MACs) travels with the team, not the site.
            "self_emitters": dict(self.template.get("self_emitters", {})),
            "node_sd_ft": float(lay.get("node_sd_ft", 2.0)),
            "models": lay["models"],
            # GPS of some nodes (and optionally the floor plan's heading): positions get lat/lon.
            "geo": json.loads(json.dumps(site.doc.get("geo") or {"points": {}, "heading_deg": None})),
        }

    # ---------- changes ----------
    def create(self, name: str, activate: bool = True, user: str | None = None) -> dict[str, Any]:
        name = str(name).strip()
        if not 0 < len(name) <= 60:
            raise ValueError("site name must be 1-60 characters")
        with self.lock:
            if len(self.sites) >= MAX_SITES:
                raise ValueError(f"at most {MAX_SITES} sites; wipe old ones first")
            base = slugify(name)
            site_id, i = base, 2
            while site_id in self.sites or (self.sites_dir / site_id).exists():
                site_id, i = f"{base[:36]}-{i}", i + 1
            # Start from the current site's node set (nodes are usually the same
            # hardware; positions get re-placed), but nothing learned there.
            src = self.layout() if getattr(self, "active_id", None) in self.sites else self.template
            doc = self._new_doc(site_id, name, src)
            d = self.sites_dir / site_id
            self._save(d, doc, {"op": "create", "user": user})
            site = Site(d, doc, self.cal_factory(d / "calibration.json"))
            self.sites[site_id] = site
            if activate:
                self.activate(site_id, user=user)
            return site.summary()

    def activate(self, site_id: str, user: str | None = None) -> None:
        with self.lock:
            if site_id not in self.sites:
                raise KeyError(site_id)
            if site_id == self.active_id:
                return
            self._period_end(self.active_id)
            self.active_id = site_id
            self._write_active(site_id)
            self._period_start(site_id)
            site = self.sites[site_id]
            self._save(site.dir, site.doc, {"op": "activate", "user": user})

    def rename(self, site_id: str, name: str, user: str | None = None) -> None:
        name = str(name).strip()
        if not 0 < len(name) <= 60:
            raise ValueError("site name must be 1-60 characters")
        with self.lock:
            site = self.get(site_id)
            before = site.doc["name"]
            site.doc["name"] = name
            self._save(site.dir, site.doc, {"op": "rename", "before": before, "after": name, "user": user})

    def set_retention(self, site_id: str, days: Any, user: str | None = None) -> None:
        if not isinstance(days, (int, float)) or not 1 <= days <= MAX_RETENTION_DAYS:
            raise ValueError(f"retention must be 1-{MAX_RETENTION_DAYS} days")
        with self.lock:
            site = self.get(site_id)
            before = site.doc["retention_days"]
            site.doc["retention_days"] = int(days)
            self._save(site.dir, site.doc, {"op": "retention", "before": before, "after": int(days), "user": user})
        self.purge()

    def set_node(self, node_id: str, xy: Any, user: str | None = None) -> dict[str, list[float]]:
        """Place (or move) one node on the active site; ``xy`` None removes it."""
        if not NODE_ID_RE.match(str(node_id)):
            raise ValueError("bad node id")
        with self.lock:
            site = self.active()
            nodes = site.doc["layout"]["nodes"]
            before = nodes.get(node_id)
            if xy is None:
                if node_id not in nodes:
                    return nodes
                if len(nodes) <= 2:
                    raise ValueError("a site needs at least two nodes")
                del nodes[node_id]
            else:
                if not (isinstance(xy, (list, tuple)) and len(xy) == 2
                        and all(isinstance(v, (int, float)) and abs(v) < 10000 for v in xy)):
                    raise ValueError("position must be [x, y] in feet")
                if node_id not in nodes and len(nodes) >= MAX_NODES:
                    raise ValueError(f"at most {MAX_NODES} nodes")
                nodes[node_id] = [round(float(xy[0]), 2), round(float(xy[1]), 2)]
            self._save(site.dir, site.doc, {"op": "node", "node": node_id, "before": before,
                                            "after": nodes.get(node_id), "user": user})
            return nodes

    def set_geo(self, body: dict[str, Any], user: str | None = None) -> dict[str, Any]:
        """Geo-reference the active site: ``{node, lat, lon}`` sets a node's GPS
        point, ``{node, remove: true}`` removes it, ``{heading_deg}`` (or null)
        sets the compass bearing of the floor plan's "up" direction."""
        with self.lock:
            site = self.active()
            geo = site.doc.setdefault("geo", {"points": {}, "heading_deg": None})
            before = json.loads(json.dumps(geo))
            if "heading_deg" in body:
                h = body["heading_deg"]
                if h is not None and not (isinstance(h, (int, float)) and not isinstance(h, bool) and 0 <= h < 360):
                    raise ValueError("heading_deg must be 0-360 or null")
                geo["heading_deg"] = None if h is None else round(float(h), 1)
            elif "node" in body:
                node = str(body["node"])
                if node not in site.doc["layout"]["nodes"]:
                    raise ValueError(f"{node} is not placed on this site")
                if body.get("remove"):
                    geo["points"].pop(node, None)
                else:
                    lat, lon = body.get("lat"), body.get("lon")
                    ok = all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (lat, lon))
                    if not ok or not (-90 <= lat <= 90 and -180 <= lon <= 180):
                        raise ValueError("lat must be -90..90 and lon -180..180 (decimal degrees)")
                    geo["points"][node] = [round(float(lat), 7), round(float(lon), 7)]
            else:
                raise ValueError("send {node, lat, lon}, {node, remove: true} or {heading_deg}")
            self._save(site.dir, site.doc, {"op": "geo", "before": before, "after": geo, "user": user})
            return geo

    def clear_history(self, site_id: str, user: str | None = None) -> int:
        """Delete the site's analytics history; keeps its map, walls and calibration."""
        with self.lock:
            site = self.get(site_id)
            n = 0
            if site.history_dir.exists():
                n = sum(1 for f in site.history_dir.glob("**/*") if f.is_file())
                shutil.rmtree(site.history_dir)
            self._save(site.dir, site.doc, {"op": "clear-history", "files": n, "user": user})
            return n

    def wipe(self, site_id: str, confirm: str) -> None:
        """Delete everything for a site. Not allowed for the active site."""
        with self.lock:
            site = self.get(site_id)
            if site_id == self.active_id:
                raise ValueError("switch to another site before wiping this one")
            if confirm != site.doc["name"]:
                raise ValueError("type the site's name exactly to confirm")
            shutil.rmtree(site.dir)
            del self.sites[site_id]
            self.version += 1

    def export(self, site_id: str) -> bytes:
        """Zip of everything stored for the site."""
        with self.lock:
            site = self.get(site_id)
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                for f in sorted(site.dir.glob("**/*")):
                    if f.is_file() and not f.name.endswith(".tmp"):
                        z.write(f, f"{site_id}/{f.relative_to(site.dir)}")
            return buf.getvalue()

    def purge(self, now: float | None = None) -> int:
        """Delete history files older than each site's retention period."""
        now = time.time() if now is None else now
        removed = 0
        with self.lock:
            for site in self.sites.values():
                if not site.history_dir.exists():
                    continue
                cutoff = time.strftime("%Y-%m-%d", time.gmtime(now - site.doc["retention_days"] * 86400))
                for f in site.history_dir.glob("**/*"):
                    m = HISTORY_FILE_RE.match(f.name)
                    if f.is_file() and m and m.group(1) < cutoff:
                        f.unlink()
                        removed += 1
        return removed
