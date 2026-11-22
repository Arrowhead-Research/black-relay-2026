from __future__ import annotations

from datetime import datetime, timezone

import pytest

from conftest import obs
from rfanalytics.friendly import FriendlyEntry, FriendlyList, load_friendly


def test_matches_exact_emitter_id_case_insensitively():
    fl = FriendlyList([FriendlyEntry("team phone", emitter_id="02:00:00:00:00:01".upper())])
    assert fl.match(obs(0, "n-west", -60)) is not None
    assert fl.match(obs(0, "n-west", -60, emitter_id="02:00:00:00:00:02")) is None


def test_ssid_matches_access_points_only():
    fl = FriendlyList([FriendlyEntry("safehouse AP", ssid="RELAY-OPS")])
    assert fl.match(obs(0, "n-west", -60, emitter_type="wifi_ap", ssid="RELAY-OPS")) is not None
    # A client probing for our SSID is not thereby friendly.
    assert fl.match(obs(0, "n-west", -60, emitter_type="wifi_client", ssid="RELAY-OPS")) is None


def test_expired_entries_do_not_match():
    expires = datetime(2026, 11, 2, 12, 0, 30, tzinfo=timezone.utc)
    fl = FriendlyList([FriendlyEntry("visitor", emitter_id="02:00:00:00:00:01", expires=expires)])
    assert fl.match(obs(10, "n-west", -60)) is not None
    assert fl.match(obs(31, "n-west", -60)) is None


@pytest.mark.parametrize("kwargs", [
    {},  # neither id nor ssid
    {"emitter_id": "02:00:00:00:00:01", "ssid": "X"},
    {"emitter_id": "02:00:00:00:00:01", "source": "rumor"},
])
def test_invalid_entries_rejected(kwargs):
    with pytest.raises(ValueError):
        FriendlyEntry("bad", **kwargs)


def test_yaml_round_trip(tmp_path):
    fl = FriendlyList([
        FriendlyEntry("ap", ssid="RELAY-OPS"),
        FriendlyEntry("node", source="node-registration", emitter_id="d8:3a:dd:00:00:01",
                      expires=datetime(2027, 1, 1, tzinfo=timezone.utc)),
    ])
    path = tmp_path / "friendly.yaml"
    path.write_text(fl.to_yaml())
    loaded = load_friendly(path)
    assert {e.to_dict()["label"] for e in loaded.entries()} == {"ap", "node"}
    assert len(loaded) == 2
