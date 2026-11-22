"""Follow-ups from the first real-hardware run: partial zone updates stay complete, pairing material is found under
either spelling of the pki dir (and so is the pin), and the zone rows / event sequence seen on a real IQ5 Hub."""
import asyncio
import io
import json

from br2 import config as C, field as FLD
from br2.adapters import iq_remote as IQ
from br2.normalizer.core import Engine, Clock, PRIMARY
from br2.pin import PanelPin

DOOR = {"zoneid": "1", "sensorname": "Front Door", "sensortype": "Door_Window", "sensorgroup": "entryexitdelay",
        "sensorstatus": "Closed", "battery_status": "Normal", "sensorstate": "0", "time": "1794708135242",
        "current_capability": "POWERG", "shortID": "10", "zone_type": "1", "zone_physical_type": "1", "partition_id": 0}
KEYPAD = {"zoneid": "2", "sensorname": "IQ Remote 1", "sensortype": "Keypad", "sensorgroup": "fixedintrusion",
          "sensorstatus": "Active", "battery_status": "Normal", "sensorstate": "0", "time": "1641379136",
          "current_capability": "WiFi", "shortID": "0", "zone_type": "104", "zone_physical_type": "4", "partition_id": 0}
FULL = [{"uri": IQ.URI_SENSOR, "resultSet": [DOOR, KEYPAD]},
        {"uri": IQ.URI_PARTITION, "resultSet": [{"partition_id": "0", "name": "Main"}]}]
URI_POWERG = "content://com.qolsys.qolsysprovider.PowerGDeviceContentProvider/powerg_device"


class FC(Clock):
    t = 1000.0
    def mono(s): return s.t
    def wall(s): return 1.8e9 + s.t


def engine(**raw):
    e = Engine(C.parse({"iq_remote": {"enabled": True, "panel_ip": "1.1.1.1"}, **raw}), FC())
    return e


def feed(e, m):
    b = e.handle(m); e.commit(); return b


def adapter():
    a = IQ.IQRemoteAdapter(C.parse({"iq_remote": {"panel_ip": "x"}}), "/tmp/none")
    a.up = True
    sent = []
    async def emit(**m): sent.append(m)
    a.emit = emit
    return a, sent


def sensor_update(cv):
    return {"eventName": "dbChanged", "dbOperation": "update", "uri": IQ.URI_SENSOR, "contentValues": cv,
            "selection": "zoneid=?", "selectionArgs": [cv.get("zoneid", "1")]}


def powerg_update(open_: int):
    return {"eventName": "dbChanged", "dbOperation": "update", "uri": URI_POWERG,
            "contentValues": {"status_data": json.dumps({"82": 0, "80": 0, "1": open_, "92": "FE0B8D"})},
            "selection": "shortID=?", "selectionArgs": ["10"]}


# ---------------------------------------------------------------- real zone rows
def test_real_rows_classify_and_keypad_is_excluded_by_default():
    zones, _ = IQ.build_model(FULL)
    assert zones[1]["raw"]["current_capability"] == "POWERG" and zones[2]["type"] == "Keypad"
    e = engine()
    b = feed(e, {"t": "snapshot", "src": PRIMARY, "zones": [zones[1], zones[2]], "partitions": []})
    assert list(e.zones) == [1] and e.zones[1]["kind"] == "contact" and e.zones[1]["name"] == "Front Door"
    assert e.zones[1]["state"] == "closed" and e.health()["zones"] == 1
    assert all(z["kind"] != "other" for z in b.zones.values()) and 2 not in e.zones
    assert not any(ev["subject"].endswith("-2") for ev in b.events)
    assert feed(e, {"t": "zone", "src": PRIMARY, "zone": {"panel_zone_id": 2, "status": "Idle"}}).events == []
    e2 = engine(zones={"include_keypads": True})            # opt-in still works, as kind other
    feed(e2, {"t": "snapshot", "src": PRIMARY, "zones": [zones[1], zones[2]], "partitions": []})
    assert e2.zones[2]["kind"] == "other"


def test_live_pair_of_updates_gives_exactly_one_open_and_one_close():
    a, sent = adapter()
    a.zones, a.parts = IQ.build_model(FULL)
    e = engine()
    feed(e, {"t": "snapshot", "src": PRIMARY, "zones": list(a.zones.values()), "partitions": []})
    events = []

    async def run():
        for op, status in ((1, "Open"), (0, "Closed")):
            await a.on_iq2meid(powerg_update(op))                        # device table first: no zone event
            assert not sent or sent[-1]["zone"]["status"] != status
            await a.on_iq2meid(sensor_update({"zoneid": "1", "sensorstatus": status}))
            while sent:
                events.extend(feed(e, {**sent.pop(0), "src": PRIMARY}).events)
    asyncio.run(run())
    assert [(x["type"], x["subject"], x["to"]) for x in events] == [("zone.state", "zone-1", "open"),
                                                                    ("zone.state", "zone-1", "closed")]


# ---------------------------------------------------------------- partial updates
def test_adapter_emits_the_merged_zone_not_the_bare_delta():
    a, sent = adapter()
    a.zones, a.parts = IQ.build_model(FULL)
    asyncio.run(a.on_iq2meid(sensor_update({"zoneid": "1", "sensorstatus": "Open"})))
    z = sent[0]["zone"]
    assert z["status"] == "Open" and z["name"] == "Front Door" and z["battery"] == "Normal" and z["partition_id"] == 0
    assert z["type"] == "Door_Window" and z["raw"]["current_capability"] == "POWERG"
    z["raw"]["x"] = 1                                                    # a copy: the receiver cannot corrupt the model
    assert "x" not in a.zones[1]["raw"]


def test_watch_lines_are_complete_on_partial_updates():
    zones, parts = IQ.build_model(FULL)
    lines = []
    seen = {1: ("Closed", "Normal", "Front Door", 0)}
    model = FLD.WatchModel(zones, parts, lambda m: lines.append(FLD._event_line(m)))
    asyncio.run(model.on_iq2meid(sensor_update({"zoneid": "1", "sensorstatus": "Open"})))
    assert lines[0]["battery"] == "Normal" and lines[0]["name"] == "Front Door" and lines[0]["partition_id"] == 0
    assert lines[0]["status"] == "Open" and seen


# ---------------------------------------------------------------- pki layouts and pin location
def _material(d, id12="f2163e1a2b3c"):
    d.mkdir(parents=True)
    for ext in ("key", "secure", "qolsys"):
        (d / f"{id12}.{ext}").write_text("x")
    return d


def test_find_pki_accepts_the_reference_tool_layout_and_pin_follows_the_material(tmp_path):
    ref = tmp_path / "ref"                                   # tools/pair_reference.py --pki-dir ref  ->  ref/pki/<12hex>/
    folder = _material(ref / "pki" / "f2163e1a2b3c")
    a = IQ.find_pki(str(ref), None)
    b = IQ.find_pki(str(ref / "pki"), None)
    assert a == b and a[0] == "f2163e1a2b3c" and a[1] == str(folder / "f2163e1a2b3c.key")
    assert PanelPin(ref).path == PanelPin(ref / "pki").path == ref / "pki" / "panel_pin.json"
    PanelPin(ref).persist("ab" * 32, "repin")                # recorded via one spelling, found via the other
    assert PanelPin(ref / "pki").stored() == "ab" * 32 and PanelPin(ref).stored() == "ab" * 32


def test_existing_layouts_are_unchanged(tmp_path):
    nested = tmp_path / "n"; _material(nested / "f2163e1a2b3c")
    assert IQ.find_pki(str(nested), None)[1].startswith(str(nested / "f2163e1a2b3c"))
    assert PanelPin(nested).path == nested / "panel_pin.json"
    flat = tmp_path / "flat"; flat.mkdir()
    for ext in ("key", "secure", "qolsys"):
        (flat / f"f2163e1a2b3c.{ext}").write_text("x")
    assert IQ.find_pki(str(flat), None)[1] == str(flat / "f2163e1a2b3c.key")
    assert PanelPin(flat).path == flat / "panel_pin.json"
    both = tmp_path / "b"; _material(both / "f2163e1a2b3c"); _material(both / "pki" / "aaaaaaaaaaaa", "aaaaaaaaaaaa")
    assert IQ.find_pki(str(both), None)[0] == "f2163e1a2b3c"          # direct folder wins over pki/
    empty = tmp_path / "e"; empty.mkdir()
    assert PanelPin(empty).path == empty / "panel_pin.json"
