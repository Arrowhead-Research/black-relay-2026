"""WBT Console: site workspaces and sign-in."""

from __future__ import annotations

import io
import json
import sys
import threading
import time
import zipfile
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import server  # noqa: E402
from auth import Auth  # noqa: E402
from server import App, CalibrationStore, load_layout  # noqa: E402
from sites import SiteManager  # noqa: E402

LAYOUT = Path(__file__).resolve().parents[1] / "layout.example.yaml"


def manager(root: Path, **kw) -> SiteManager:
    return SiteManager(root, load_layout(LAYOUT), CalibrationStore, **kw)


def test_first_start_creates_default_site(tmp_path):
    m = manager(tmp_path)
    assert m.active_id == "default" and [s["id"] for s in m.list()] == ["default"]
    assert m.layout()["nodes"] == load_layout(LAYOUT)["nodes"]
    assert m.layout()["self_emitters"] == load_layout(LAYOUT)["self_emitters"]   # team hardware
    assert m.active().doc["periods"][-1][1] is None                               # active period open


def test_legacy_calibration_dir_is_migrated(tmp_path):
    old = tmp_path / "default"
    old.mkdir()
    (old / "calibration.json").write_text(json.dumps({"points": [], "fits": {}, "walls": [
        {"x1": 0, "y1": 0, "x2": 10, "y2": 0}]}))
    m = manager(tmp_path)
    assert not old.exists() and (tmp_path / "sites" / "default" / "calibration.json").exists()
    assert len(m.active().cal.data["walls"]) == 1


def test_new_site_copies_nodes_not_learning_and_switches(tmp_path):
    m = manager(tmp_path)
    m.active().cal.add_walls([{"x1": 0, "y1": 0, "x2": 5, "y2": 5}])
    m.set_node("wbt-1", [1, 2])
    s = m.create("Warehouse B")
    assert s["id"] == "warehouse-b" and m.active_id == "warehouse-b"
    assert m.layout()["nodes"]["wbt-1"] == [1.0, 2.0]          # node set carried over to re-place
    assert m.active().cal.data["walls"] == []                   # nothing learned at the old site
    assert m.create("Warehouse B", activate=False)["id"] == "warehouse-b-2"
    # The choice survives a restart; the old site's period was closed.
    m2 = manager(tmp_path)
    assert m2.active_id == "warehouse-b"
    assert m2.get("default").doc["periods"][-1][1] is not None


def test_nodes_validation(tmp_path):
    m = manager(tmp_path)
    m.set_node("wbt-9", [3, 4])
    assert m.layout()["nodes"]["wbt-9"] == [3.0, 4.0]
    m.set_node("wbt-9", None)
    assert "wbt-9" not in m.layout()["nodes"]
    with pytest.raises(ValueError):
        m.set_node("bad id!", [0, 0])
    with pytest.raises(ValueError):
        m.set_node("wbt-1", ["x", 0])
    for n in list(m.layout()["nodes"])[2:]:
        m.set_node(n, None)
    with pytest.raises(ValueError):
        m.set_node(next(iter(m.layout()["nodes"])), None)        # keep at least two


def test_wipe_export_clear_and_purge(tmp_path):
    m = manager(tmp_path)
    m.create("Old house", activate=False)
    old = m.get("old-house")
    old.history_dir.mkdir()
    now = time.time()
    day = lambda d: time.strftime("%Y-%m-%d", time.gmtime(now - d * 86400))  # noqa: E731
    (old.history_dir / f"{day(10)}-events.jsonl").write_text("{}\n")
    (old.history_dir / f"{day(1)}-events.jsonl").write_text("{}\n")
    assert m.purge(now) == 1                                    # 7-day default retention
    (old.history_dir / f"{day(2)}-events.jsonl").write_text("{}\n")
    m.set_retention("old-house", 1)
    # A day's file goes once the whole day is past retention: yesterday's stays.
    assert [f.name for f in old.history_dir.iterdir()] == [f"{day(1)}-events.jsonl"]
    (old.history_dir / f"{day(1)}-events.jsonl").unlink()

    (old.history_dir / f"{day(0)}-events.jsonl").write_text("{}\n")
    names = zipfile.ZipFile(io.BytesIO(m.export("old-house"))).namelist()
    assert "old-house/site.json" in names and any(n.startswith("old-house/history/") for n in names)
    assert m.clear_history("old-house") == 1 and not old.history_dir.exists()

    with pytest.raises(ValueError):
        m.wipe("default", "default")                            # active site cannot be wiped
    with pytest.raises(ValueError):
        m.wipe("old-house", "old house")                        # name must match exactly
    m.wipe("old-house", "Old house")
    assert not (tmp_path / "sites" / "old-house").exists() and [s["id"] for s in m.list()] == ["default"]
    with pytest.raises(ValueError):
        m.set_retention("default", 0)


def test_auth_tokens():
    a = Auth("op-pw", "admin-pw", None)
    assert a.login("op-pw") == "operator" and a.login("admin-pw") == "admin" and a.login("nope") is None
    tok = a.issue("operator", now=1000)
    assert a.verify(tok, now=1001) == "operator"
    assert a.verify(tok, now=1000 + 13 * 3600) is None                 # expired
    assert a.verify(tok.replace("operator", "admin"), now=1001) is None  # tampered role
    assert a.verify("garbage", now=1001) is None
    assert Auth(None, None, None).verify(None) == "admin"              # sign-in off
    assert Auth.allows("admin", "operator") and not Auth.allows("operator", "admin")


@pytest.fixture
def console(tmp_path):
    m = manager(tmp_path)
    app = App(None, None, window_s=180, poll_s=3, sites=m, auth=Auth("op-pw", "admin-pw", tmp_path / ".key"))
    server.Handler.app = app
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), partial(server.Handler, directory=str(server.HERE / "static")))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", m
    httpd.shutdown()


def call(base, path, body=None, cookie=None, ctype="application/json"):
    headers = {"Content-Type": ctype} if body is not None else {}
    if cookie:
        headers["Cookie"] = cookie
    req = Request(base + path, data=json.dumps(body).encode() if body is not None else None, headers=headers)
    try:
        with urlopen(req) as r:
            return r.status, r.headers, r.read()
    except HTTPError as e:
        return e.code, e.headers, e.read()


def login(base, pw):
    status, headers, _ = call(base, "/api/login", {"password": pw})
    assert status == 200
    return headers["Set-Cookie"].split(";", 1)[0]


def test_http_roles_and_site_switch(console):
    base, m = console
    assert call(base, "/")[0] == 200                                    # the page itself loads signed out
    assert call(base, "/api/state")[0] == 401
    assert json.loads(call(base, "/api/session")[2]) == {"enabled": True, "role": None}
    op, admin = login(base, "op-pw"), login(base, "admin-pw")
    st = json.loads(call(base, "/api/state", cookie=op)[2])
    assert st["site"]["id"] == "default"

    # Cross-site form posts (no JSON content type) are refused.
    assert call(base, "/api/sites", {"name": "x"}, cookie=op, ctype="text/plain")[0] == 415

    status, _, body = call(base, "/api/sites", {"name": "Safe house 2"}, cookie=op)
    assert status == 200 and m.active_id == "safe-house-2"
    status, _, body = call(base, "/api/site/nodes", {"node": "wbt-1", "x": 5, "y": 6}, cookie=op)
    assert status == 200 and json.loads(body)["nodes"]["wbt-1"] == [5.0, 6.0]
    assert json.loads(call(base, "/api/state", cookie=op)[2])["layout"]["nodes"]["wbt-1"] == [5.0, 6.0]

    # Operators cannot clear, wipe, export or change retention.
    for path in ("/api/sites/default/wipe", "/api/sites/default/clear", "/api/sites/default/retention"):
        assert call(base, path, {"confirm": "default", "days": 3}, cookie=op)[0] == 403
    assert call(base, "/api/sites/default/export", cookie=op)[0] == 403
    status, headers, data = call(base, "/api/sites/default/export", cookie=admin)
    assert status == 200 and headers["Content-Type"] == "application/zip" and data[:2] == b"PK"

    assert call(base, "/api/sites/default/wipe", {"confirm": "nope"}, cookie=admin)[0] == 400
    status, _, body = call(base, "/api/sites/default/wipe", {"confirm": "default"}, cookie=admin)
    assert status == 200 and [s["id"] for s in json.loads(body)["sites"]] == ["safe-house-2"]
    assert call(base, "/api/sites/nope/activate", {}, cookie=op)[0] == 404


def test_bad_password_is_slow_and_refused(console, monkeypatch):
    base, _ = console
    monkeypatch.setattr(server, "LOGIN_FAIL_DELAY_S", 0.2)
    t = time.time()
    assert call(base, "/api/login", {"password": "wrong"})[0] == 401
    assert time.time() - t >= 0.2
