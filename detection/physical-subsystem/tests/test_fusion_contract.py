"""Schema + contract tests for every team message type (offline, no broker)."""
import json, re, uuid
import pytest
from jsonschema import Draft202012Validator
from br2.outbound import formats as F
from br2.outbound.outbox import Outbox, Item, EVENT, STATUS
from fusion_helpers import validate, zone, log_event, TOPIC_FOR, SCHEMAS, ROOT

SITE = {"id": "home-1", "name": "Home"}
NOW = 1767268800.0      # 2026-01-01T12:00:00Z
TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
FORBIDDEN = ("code", "token", "password", "secret", "key", "pin", "arming", "arm_", "disarm", "pki", "partition_id")


def build_all():
    """One example of every message type / event_type the bridge can emit."""
    out = []
    cases = [("zone.state", "contact", "closed", "open"), ("zone.state", "contact", "open", "closed"),
             ("zone.state", "motion", "idle", "active"), ("zone.state", "motion", "active", "idle"),
             ("zone.state", "glass_break", "idle", "active"), ("zone.state", "glass_break", "active", "idle"),
             ("zone.state", "other", "idle", "active"), ("zone.state", "other", "active", "idle"),
             ("zone.tamper", "contact", False, True), ("zone.tamper", "contact", True, False),
             ("zone.added", "motion", None, "idle"), ("zone.removed", "motion", "idle", None),
             ("zone.battery", "contact", False, True), ("zone.battery", "contact", True, False),
             ("zone.stale", "contact", False, True), ("zone.stale", "contact", True, False), ("zone.renamed", "contact", "Old", "New")]
    for i, (typ, kind, frm, to) in enumerate(cases, 1):
        e = log_event(i, typ, "zone-7", frm, to)
        z = zone(7, kind=kind, state=to if isinstance(to, str) else "idle", stale=(typ == "zone.stale" and bool(to)))
        et = F.map_log_event(e, kind)
        assert et, (typ, kind, frm, to)
        _, b = F.event_builder(SITE, e, z, et, NOW, "db1", prev_seq=(i - 1) or None)
        out.append(F.encode(b()))
    view = F.panel_view({"status": "degraded", "active_source": "control4", "links": {"iq_remote": "down", "control4": "up"},
                         "stale_zones": 2, "zones": 9})
    _, b = F.panel_builder(SITE, view, "2026-01-01T11:59:59.000Z", NOW); out.append(F.encode(b()))
    _, b = F.panel_builder(SITE, F.panel_view({}), None, NOW); out.append(F.encode(b()))
    _, b = F.zone_status_builder(SITE, zone(7, kind="glass_break", state="active", tamper=True), NOW); out.append(F.encode(b()))
    _, b = F.zone_status_builder(SITE, zone(8, last_changed=None, source=None), NOW); out.append(F.encode(b()))
    return out


def test_schemas_are_valid_draft_2020_12():
    for p in (ROOT / "schemas").glob("*.v0.schema.json"):
        s = json.loads(p.read_text())
        assert s["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        Draft202012Validator.check_schema(s)
    assert len(list((ROOT / "schemas").glob("*.v0.schema.json"))) == 3


def test_every_message_type_validates_against_its_schema():
    seen_schemas, seen_events = set(), set()
    for raw in build_all():
        m = validate(raw)
        seen_schemas.add(m["schema"])
        if m["schema"] == "physical.event.v0":
            seen_events.add(m["event_type"])
    assert seen_schemas == set(SCHEMAS)
    assert seen_events == set(F.EVENT_TYPES)


def test_schemas_reject_malformed_messages():
    good = json.loads(build_all()[0])
    for mut in (lambda m: m.update(id="not-a-uuid"), lambda m: m.update(timestamp="2026-01-01T12:00:00Z"),
                lambda m: m.update(timestamp="2026-01-01T12:00:00.123+00:00"), lambda m: m.pop("site"),
                lambda m: m.update(schema="physical.event.v1"), lambda m: m["zone"].update(kind="siren"),
                lambda m: m.update(id=str(uuid.uuid1()))):
        bad = json.loads(json.dumps(good)); mut(bad)
        with pytest.raises(Exception):
            SCHEMAS["physical.event.v0"].validate(bad)


def test_envelope_fields_and_encoding():
    for raw in build_all():
        assert isinstance(raw, bytes)
        raw.decode("utf-8")
        m = validate(raw)
        assert list(m)[:3] == ["schema", "id", "timestamp"]
        assert uuid.UUID(m["id"]).version == 4 and m["id"] == m["id"].lower()
        assert TS_RE.match(m["timestamp"])
        assert m["site"] == SITE
        if m["schema"] == "physical.event.v0":
            assert isinstance(m["seq"], int) and m["seq"] >= 1
        else:
            assert "seq" not in m                      # status messages carry no seq
        assert "retain" not in m


def test_site_default_id():
    assert F.site_block(None) == {"id": "default", "name": "default"}
    assert F.site_block({"name": "X"}) == {"id": "default", "name": "X"}


def test_topic_per_type_and_prefix():
    assert F.topic_events("detection/physical") == "detection/physical/events"
    assert F.topic_panel("detection/physical/") == "detection/physical/status/panel"
    assert F.topic_zones("/detection/physical") == "detection/physical/status/zones"
    assert F.topic_events("x/y") == "x/y/events"
    for bad in ("", "a/#", "a/+/b", "a//b"):
        if bad == "":
            assert F.clean_prefix(bad) == "detection/physical"      # empty = default
        else:
            with pytest.raises(ValueError):
                F.clean_prefix(bad)
    assert F.QOS == 1 and F.RETAIN is False


def test_topic_is_chosen_by_schema_through_the_outbox(tmp_path):
    """The publisher's topics line up with each message's schema (contract: topic per type)."""
    from br2.outbound.fusion import FusionPublisher
    from br2 import config as C
    from br2.outbound.state import OutState
    cfg = C.parse({"storage": {"data_dir": str(tmp_path)}, "fusion": {"enabled": True}})
    p = FusionPublisher(cfg, OutState(), reader=None, sender=object())
    assert (p.t_events, p.t_panel, p.t_zones) == tuple(TOPIC_FOR[s] for s in
                                                       ("physical.event.v0", "physical.panel-status.v0", "physical.zone-status.v0"))


def test_timestamp_is_event_time_not_publish_time():
    e = log_event(1, "zone.state", "zone-1", "closed", "open", ts="2025-06-30T01:02:03.004Z")
    _, b = F.event_builder(SITE, e, zone(1, kind="contact", state="open"), "zone_opened", NOW + 9999, "db")
    assert b()["timestamp"] == "2025-06-30T01:02:03.004Z"
    assert F.rfc3339(0.0015) == "1970-01-01T00:00:00.001Z"        # ms precision, truncated, always Z
    assert F.norm_ts("2026-01-01T12:00:00+02:00", 0) == "2026-01-01T10:00:00.000Z"
    assert F.norm_ts("garbage", NOW) == F.rfc3339(NOW)


def test_event_id_is_stable_and_valid_uuid4():
    e = log_event(5, "zone.state", "zone-1", "closed", "open")
    i1 = F.event_builder(SITE, e, zone(1), "zone_opened", NOW, "db")[0]
    i2 = F.event_builder(SITE, e, zone(1), "zone_opened", NOW + 100, "db")[0]
    i3 = F.event_builder(SITE, dict(e, seq=6, id="evt-6"), zone(1), "zone_opened", NOW, "db")[0]
    i4 = F.event_builder(SITE, e, zone(1), "zone_opened", NOW, "other-db")[0]
    assert i1 == i2 and len({i1, i3, i4}) == 3
    assert uuid.UUID(i1).version == 4 and uuid.UUID(i1).variant == uuid.RFC_4122


def test_id_and_timestamp_stable_on_resend(tmp_path):
    ob = Outbox(str(tmp_path / "ob"))
    e = log_event(5, "zone.state", "zone-1", "closed", "open")
    mid, b = F.event_builder(SITE, e, zone(1, state="open"), "zone_opened", NOW, "db")
    ob.append([Item(mid, "detection/physical/events", EVENT, lambda _n: F.encode(b()))])
    first = ob.pending(10)[0]
    ob.close()
    ob2 = Outbox(str(tmp_path / "ob"))                    # "restart": the message is re-sent from disk
    again = ob2.pending(10)[0]
    assert first.payload == again.payload and first.id == again.id == mid
    assert json.loads(again.payload)["timestamp"] == "2026-01-01T12:00:00.123Z"
    # ...and re-enqueueing the same log row (crash between enqueue and cursor save) does not duplicate it
    assert ob2.append([Item(mid, "detection/physical/events", EVENT, lambda _n: F.encode(b()))]) == 0
    assert len(ob2.pending(10)) == 1


def test_event_mapping_table():
    m = F.map_log_event
    assert m({"type": "zone.state", "to": "open"}, "contact") == "zone_opened"
    assert m({"type": "zone.state", "to": "closed"}, "contact") == "zone_closed"
    assert m({"type": "zone.state", "to": "active"}, "motion") == "motion_detected"
    assert m({"type": "zone.state", "to": "idle"}, "motion") == "motion_cleared"
    assert m({"type": "zone.state", "to": "active"}, "glass_break") == "glass_break_detected"
    assert m({"type": "zone.state", "to": "unknown"}, "contact") is None
    assert m({"type": "zone.tamper", "to": True}, "contact") == "tamper"
    assert m({"type": "zone.tamper", "to": False}, "contact") == "tamper_cleared"
    assert m({"type": "zone.battery", "to": True}, "motion") == "battery_low"
    assert m({"type": "zone.battery", "to": False}, "motion") == "battery_ok"
    assert m({"type": "zone.stale", "to": True}, "motion") == "zone_stale"
    assert m({"type": "zone.stale", "to": False}, "motion") == "zone_fresh"
    for t in ("zone.online", "partition.arming", "partition.alarm", "bridge.link"):
        assert m({"type": t, "to": "x"}, None) is None


def test_event_seq_is_the_log_seq_and_prev_seq_chains():
    e = log_event(1042, "zone.state", "zone-7", "closed", "open")
    _, b = F.event_builder(SITE, e, zone(7, state="open"), "zone_opened", NOW, "db", prev_seq=1040)
    m = json.loads(F.encode(b()))
    assert m["seq"] == 1042 and m["prev_seq"] == 1040 and "log_seq" not in m
    validate(m)
    _, b = F.event_builder(SITE, e, zone(7, state="open"), "zone_opened", NOW, "db")
    assert b()["prev_seq"] is None
    # the SAME log row always yields the same seq and id, whatever the outbox numbering is
    assert F.event_builder(SITE, e, zone(7), "zone_opened", NOW + 5, "db")[0] == F.event_builder(SITE, e, zone(7), "zone_opened", NOW, "db")[0]


def test_status_messages_carry_no_seq_and_the_schemas_forbid_it():
    _, b = F.panel_builder(SITE, F.panel_view({}), None, NOW)
    _, c = F.zone_status_builder(SITE, zone(7), NOW)
    for m in (b(), c()):
        assert "seq" not in m
        bad = dict(m, seq=5)
        with pytest.raises(Exception):
            SCHEMAS[m["schema"]].validate(bad)


def test_stale_and_battery_events_describe_the_zone_not_a_transition():
    e = log_event(9, "zone.stale", "zone-7", False, True)
    _, b = F.event_builder(SITE, e, zone(7, state="open", stale=False), "zone_stale", NOW, "db")
    m = b()
    assert m["event_type"] == "zone_stale" and m["stale"] is True and m["state"] == "open" and m["previous_state"] is None
    e = log_event(10, "zone.battery", "zone-7", False, True)
    m = F.event_builder(SITE, e, zone(7), "battery_low", NOW, "db")[1]()
    assert m["battery_low"] is True and m["previous_state"] is None
    validate(m)


def test_zone_block_normalizes_unknown_kind_and_partition():
    assert F.zone_block({"id": "zone-1", "name": "N", "kind": "weird", "partition_id": "2"}) == \
        {"id": "zone-1", "name": "N", "kind": "other", "partition": 2}
    assert F.zone_block(None, "zone-9") == {"id": "zone-9", "name": "zone-9", "kind": "other", "partition": 0}


def test_no_secret_or_arming_fields_in_any_payload():
    def keys(o, acc):
        if isinstance(o, dict):
            for k, v in o.items():
                acc.add(k); keys(v, acc)
        elif isinstance(o, list):
            [keys(v, acc) for v in o]
        return acc
    allk = set()
    for raw in build_all():
        keys(json.loads(raw), allk)
    assert not [k for k in allk if any(f == k or k.startswith(f) for f in FORBIDDEN)], allk
    # the raw panel record (which carries vendor fields) is never forwarded
    assert "raw" not in allk and "panel_zone_id" not in allk


def test_example_payloads_in_contract_doc_validate():
    """Every ```json example in MQTT-CONTRACT.md is a valid message (so the doc cannot drift from the schemas)."""
    doc = (ROOT / "MQTT-CONTRACT.md")
    if not doc.exists():
        pytest.skip("MQTT-CONTRACT.md not written yet")
    blocks = re.findall(r"```json\n(.*?)```", doc.read_text(), re.S)
    msgs = [json.loads(b) for b in blocks]
    msgs = [m for m in msgs if "schema" in m]
    assert {m["schema"] for m in msgs} == set(SCHEMAS)
    for m in msgs:
        validate(m)
