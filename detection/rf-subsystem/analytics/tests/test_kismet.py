"""Kismet REST adapter mapping (v1)."""

from __future__ import annotations

from rfanalytics.kismet import KismetPoller, _emitter_type, _node_id, _tpms_id
from rfanalytics.observation import parse_observation


def _wifi_ap() -> dict:
    return {
        "kismet.device.base.macaddr": "AA:BB:CC:DD:EE:FF",
        "kismet.device.base.phyname": "IEEE802.11",
        "kismet.device.base.type": "Wi-Fi AP",
        "kismet.device.base.manuf": "TestCo",
        "kismet.device.base.frequency": 5745000,
        "kismet.device.base.last_time": 1_700_000_000,
        "kismet.device.base.signal": {
            "kismet.common.signal.type": "dbm",
            "kismet.common.signal.last_signal": -67,
        },
        "kismet.device.base.seenby": [
            {
                "kismet.common.seenby.last_time": 1_700_000_000,
                "kismet.common.seenby.source": {"kismet.datasource.name": "wbt-1:wifi"},
                "kismet.common.seenby.signal": {"kismet.common.signal.type": "none", "kismet.common.signal.last_signal": 0},
            },
            {
                "kismet.common.seenby.last_time": 1_700_000_001,
                "kismet.common.seenby.source": {"kismet.datasource.name": "wbt-2:wifi"},
                "kismet.common.seenby.signal": {"kismet.common.signal.type": "none", "kismet.common.signal.last_signal": 0},
            },
        ],
        "dot11.device": {"dot11.device.last_beaconed_ssid_record": {"dot11.advertisedssid.ssid": "testnet"}},
    }


def _ble() -> dict:
    return {
        "kismet.device.base.macaddr": "6C:90:E8:EC:C0:52",
        "kismet.device.base.phyname": "Bluetooth",
        "kismet.device.base.type": "BTLE",
        "kismet.device.base.frequency": 2400000,
        "kismet.device.base.last_time": 1_700_000_010,
        "kismet.device.base.signal": {"kismet.common.signal.type": "dbm", "kismet.common.signal.last_signal": -80},
        "kismet.device.base.seenby": [
            {
                "kismet.common.seenby.last_time": 1_700_000_010,
                "kismet.common.seenby.source": {"kismet.datasource.name": "wbt-3:bt"},
                "kismet.common.seenby.signal": {"kismet.common.signal.type": "none", "kismet.common.signal.last_signal": 0},
            }
        ],
        "bluetooth.device": {},
    }


def _tpms() -> dict:
    return {
        "kismet.device.base.macaddr": "00:00:00:00:00:00",
        "kismet.device.base.phyname": "RTL433",
        "kismet.device.base.type": "sensor",
        "kismet.device.base.frequency": 315000,  # kHz-ish value handled by adapter
        "kismet.device.base.last_time": 1_700_000_020,
        "kismet.device.base.signal": {"kismet.common.signal.type": "dbm", "kismet.common.signal.last_signal": -55},
        "kismet.device.base.seenby": [
            {
                "kismet.common.seenby.last_time": 1_700_000_020,
                "kismet.common.seenby.source": {"kismet.datasource.name": "wbt-4:tpms"},
                "kismet.common.seenby.signal": {"kismet.common.signal.type": "none", "kismet.common.signal.last_signal": 0},
            }
        ],
        "rtl433.device": {"rtl433.device.id": 0x1A2B3C4D, "rtl433.device.model": "Schrader"},
    }


def test_node_id_mapping():
    assert _node_id("wbt-1:wifi") == "wbt-1"
    assert _node_id("wbt-4:tpms") == "wbt-4"
    assert _node_id("") is None


def test_emitter_typing():
    assert _emitter_type("IEEE802.11", "Wi-Fi AP", _wifi_ap()) == "wifi_ap"
    assert _emitter_type("Bluetooth", "BTLE", _ble()) == "ble"
    assert _emitter_type("RTL433", "sensor", _tpms()) == "tpms"


def test_tpms_id():
    assert _tpms_id(_tpms()) == "1a2b3c4d"


def test_wifi_emits_one_observation_per_seenby_node():
    poller = KismetPoller("http://example.invalid")
    recs = poller.observations_from_device(_wifi_ap())
    assert len(recs) == 2
    nodes = {r["node_id"] for r in recs}
    assert nodes == {"wbt-1", "wbt-2"}
    for r in recs:
        parsed = parse_observation(r)  # contract-valid
        assert parsed.emitter_type == "wifi_ap"
        assert parsed.emitter_id == "aa:bb:cc:dd:ee:ff"
        assert parsed.rssi_dbm == -67
        assert r["attributes"].get("rssi_is_aggregate") is True
        assert r["attributes"].get("ssid") == "testnet"


def test_dedupe_by_seenby_time():
    poller = KismetPoller("http://example.invalid")
    assert len(poller.observations_from_device(_ble())) == 1
    assert len(poller.observations_from_device(_ble())) == 0
    newer = _ble()
    newer["kismet.device.base.seenby"][0]["kismet.common.seenby.last_time"] = 1_700_000_011
    assert len(poller.observations_from_device(newer)) == 1


def test_tpms_observation_contract():
    poller = KismetPoller("http://example.invalid")
    recs = poller.observations_from_device(_tpms())
    assert len(recs) == 1
    r = recs[0]
    assert r["emitter_type"] == "tpms"
    assert r["emitter_id"] == "tpms:1a2b3c4d"
    parse_observation(r)
