"""Server-side localization matches the page's own code (tests/parity/golden.json,
produced by running static/app.js and walls.js under node: make_golden.mjs)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from locate import Grid, Locator, walls_expected  # noqa: E402

GOLDEN = json.loads((Path(__file__).resolve().parent / "parity" / "golden.json").read_text())


def _models(inp):
    return {band: {**m, "offsets": inp["offsets"].get(band, {})} for band, m in inp["models"].items()}


@pytest.mark.parametrize("case", sorted(GOLDEN))
def test_matches_the_page(case):
    inp, want = GOLDEN[case]["input"], GOLDEN[case]["output"]
    g = Grid(inp["nodes"], inp["walls"])
    assert {k: getattr(g, k) for k in want["grid"]} == pytest.approx(want["grid"])
    for probe, expected in zip(inp["probes"], want["walls_probe"]):
        assert float(walls_expected(probe[0], probe[1], probe[2], probe[3], inp["walls"])) == pytest.approx(expected, abs=1e-9)
    loc = Locator()
    loc.configure(inp["nodes"], inp["walls"], _models(inp))
    for step, expected_step in zip(inp["steps"], want["steps"]):
        for dev, exp in zip(step, expected_step):
            got = loc.locate(dev, inp["nodes"])
            if exp is None:
                assert got is None, dev["id"]
                continue
            assert got["kind"] == exp["kind"] and got["K"] == exp["K"] and got["ids"] == exp["ids"]
            assert got["updated"] == exp["updated"], dev["id"]
            assert (got["x"], got["y"]) == pytest.approx((exp["x"], exp["y"]), abs=1e-4), dev["id"]
            assert (got["offset"], got["res"]) == pytest.approx((exp["offset"], exp["res"]), abs=1e-4), dev["id"]


def test_model_or_layout_change_restarts_smoothing():
    inp = GOLDEN["no_walls"]["input"]
    loc = Locator()
    loc.configure(inp["nodes"], [], _models(inp))
    dev = inp["steps"][0][0]
    assert loc.locate(dev, inp["nodes"])["updated"]
    assert not loc.locate(dev, inp["nodes"])["updated"]          # no new readings
    loc.configure(inp["nodes"], [], _models(inp))                  # unchanged: keeps the filter
    assert not loc.locate(dev, inp["nodes"])["updated"]
    moved = {**inp["nodes"], "wbt-1": [1, 1]}
    loc.configure(moved, [], _models(inp))                         # node moved: restart
    assert loc.locate(dev, moved)["updated"]
    loc.locate_all([], moved)                                      # device gone: its filter is dropped
    assert loc.filt == {}


def test_georeference_from_node_gps_points():
    import math
    from locate import EARTH_R_M, georeference, to_latlon
    nodes = {"wbt-1": [0, 0], "wbt-2": [100, 0], "wbt-3": [100, 50]}   # feet; page y points down
    lat0, lon0 = 45.0, 10.0   # synthetic
    k = math.pi / 180 * EARTH_R_M

    def gps(x_ft, y_ft, heading_deg):
        # page "up" (-y) points at heading_deg; x is 90 degrees clockwise from it
        e_up, n_up = math.sin(math.radians(heading_deg)), math.cos(math.radians(heading_deg))
        e_x, n_x = n_up, -e_up
        east = (x_ft * e_x - y_ft * e_up) * 0.3048
        north = (x_ft * n_x - y_ft * n_up) * 0.3048
        return [lat0 + north / k, lon0 + east / (k * math.cos(math.radians(lat0)))]

    pts = {n: gps(*xy, 30) for n, xy in nodes.items()}
    tf = georeference(nodes, {"points": {"wbt-1": pts["wbt-1"], "wbt-3": pts["wbt-3"]}})
    assert tf["method"] == "points" and tf["rms_m"] < 0.01 and tf["scale"] == 1.0 and tf["warnings"] == []
    assert tf["heading_deg"] == 30.0
    lat, lon = to_latlon(tf, 100, 0)                                  # wbt-2 was not given: predicted
    assert (lat, lon) == pytest.approx(pts["wbt-2"], abs=1e-7)
    # one node plus the floor plan's heading
    tf1 = georeference(nodes, {"points": {"wbt-2": pts["wbt-2"]}, "heading_deg": 30})
    assert tf1["method"] == "heading" and to_latlon(tf1, 0, 0) == pytest.approx(pts["wbt-1"], abs=1e-7)
    assert georeference(nodes, {"points": {"wbt-2": pts["wbt-2"]}}) is None    # one point, no heading
    # a coordinate typed wrong (about 20 m off) is flagged
    bad = {**pts, "wbt-2": [pts["wbt-2"][0] + 0.0002, pts["wbt-2"][1]]}
    tfb = georeference(nodes, {"points": bad})
    assert tfb["rms_m"] > 3 and len(tfb["warnings"]) == 2
