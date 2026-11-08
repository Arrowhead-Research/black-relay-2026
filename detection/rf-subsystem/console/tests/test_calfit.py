"""Server-side calibration fit and model selection match the page's code
(tests/parity/cal_golden.json, produced by make_cal_golden.mjs)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from calfit import Calibrator, fit_signature, fit_to_save, fixed  # noqa: E402

GOLDEN = json.loads((Path(__file__).resolve().parent / "parity" / "cal_golden.json").read_text())


def test_fixed_matches_javascript_tofixed():
    assert fixed(2.345, 2) == 2.35 or fixed(2.345, 2) == 2.34     # binary value decides, as in JS
    assert fixed(-2.5, 0) == -3 and fixed(0.125, 2) == 0.13 and fixed(1.005, 2) == 1.0


@pytest.mark.parametrize("band", ["wifi", "bt"])
def test_model_selection_matches_the_page(band):
    inp, want = GOLDEN["input"], GOLDEN["output"][band]
    cal = Calibrator(inp["nodes"], inp["walls"], inp["models"], inp["node_sd_ft"])
    r = cal.best_model(band, inp["spots"], inp["links"])
    assert [p["id"] for p in r["spots"]] == want["spots"] and [p["id"] for p in r["links"]] == want["links"]
    assert r["defMedian"] == pytest.approx(want["defMedian"], abs=1e-6)
    assert len(r["candidates"]) == len(want["candidates"])
    for got, exp in zip(r["candidates"], want["candidates"]):
        assert got["opts"] == exp["opts"]
        assert got["fit"] == exp["fit"], got["opts"]
        assert got["errs"] == pytest.approx(exp["errs"], abs=1e-6), got["opts"]
        assert got["problems"] == exp["problems"] and got["params"] == exp["params"]
    assert r["adopted"] == want["adopted"] and r["reason"] == want["reason"]
    assert (r["candidates"].index(r["best"]) if r["best"] else None) == want["best"]


def test_saved_fit_and_signature():
    inp = GOLDEN["input"]
    cal = Calibrator(inp["nodes"], inp["walls"], inp["models"], inp["node_sd_ft"])
    r = cal.best_model("wifi", inp["spots"], inp["links"])
    sig = fit_signature("wifi", inp["spots"], inp["links"], inp["nodes"], inp["walls"], inp["models"], inp["node_sd_ft"])
    saved = fit_to_save(r, sig)
    assert saved["sig"] == sig and saved["by"] == "server" and saved["loo_median_ft"] == r["best"]["median"]
    moved = {**inp["nodes"], "wbt-1": [1, 0]}
    assert fit_signature("wifi", inp["spots"], inp["links"], moved, inp["walls"], inp["models"], 1.5) != sig
    assert fit_signature("bt", inp["spots"], inp["links"], inp["nodes"], inp["walls"], inp["models"], 1.5) == \
        fit_signature("bt", inp["spots"], [], inp["nodes"], inp["walls"], inp["models"], 1.5)   # links: Wi-Fi only


def test_server_selects_and_saves_fits(tmp_path):
    from server import App, CalibrationStore, load_layout
    from test_server import LAYOUT
    inp = GOLDEN["input"]
    layout = {**load_layout(LAYOUT), "nodes": inp["nodes"], "models": inp["models"], "node_sd_ft": inp["node_sd_ft"]}
    store = CalibrationStore(tmp_path / "cal.json")
    for p in inp["spots"]:
        store.add_point({**p, "band": p["band"]})
    store.add_walls([{k: w[k] for k in ("x1", "y1", "x2", "y2", "sd_ft")} for w in inp["walls"]])
    app = App(None, layout, window_s=60, poll_s=3, cal_store=store)
    app._links["default"] = {"links": [{"node": l["node"], "device": l["device"], "rssi": l["rssi"]} for l in inp["links"]]}
    # add_point gives new ids: compare the chosen model, not ids
    assert app.refresh_fits() == {"wifi": "saved", "bt": "saved", "tpms": "unchanged"}
    for band in ("wifi", "bt"):
        want = GOLDEN["output"][band]
        best = want["candidates"][want["best"]]["fit"]
        saved = store.data["fits"][band]
        assert {k: saved[k] for k in best} == best and saved["by"] == "server"
    assert app.refresh_fits() == {"wifi": "unchanged", "bt": "unchanged", "tpms": "unchanged"}
    # a spot deleted: re-selected
    store.delete_points([store.data["points"][0]["id"]])
    assert app.refresh_fits()["wifi"] in ("saved", "removed")
