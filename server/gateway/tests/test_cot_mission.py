"""The CoT that decides what TAK puts in a mission package."""

from datetime import datetime, timezone
from xml.etree import ElementTree as ET

from pytak_gateway import cot

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)


def test_identify_event_names_the_connection_with_a_contact_endpoint():
    ev = ET.fromstring(cot.identify_event("pytak-gateway", NOW))
    assert ev.get("uid") == "pytak-gateway"
    assert ev.get("type") == "t-x-c-t"  # a ping: nothing on a map
    contact = ev.find("detail/contact")
    assert contact.get("endpoint") == "*:-1:stcp"  # what TAK reads the client uid from
    assert ev.find("detail/takv") is not None


def test_a_delete_carries_no_mission():
    """TAK 5.6 would add the delete itself to the mission as an item."""
    ev = ET.fromstring(cot.delete_event("sh1-site", NOW))
    assert ev.find("detail/marti") is None
    assert ev.find("detail/link").get("uid") == "sh1-site"


def test_the_site_event_is_addressed_to_its_mission_package():
    ev = ET.fromstring(cot.site_event("sh1", 38.0, -77.0, NOW, mission="sh1-mission"))
    assert ev.find("detail/marti/dest").get("mission") == "sh1-mission"


# The plugin's fixture android/app/src/test/resources/gateway-cot-asked/boundary.xml (safehouse-plugin repo): the
# boundary as the plugin asked the gateway to send it. Copied, not read, so this repo's tests stand alone.
PLUGIN_BOUNDARY = (
    '<event version="2.0" uid="sh1-boundary" type="u-d-f" how="h-e" time="2026-11-18T14:00:00Z" '
    'start="2026-11-18T14:00:00Z" stale="2026-11-18T14:05:00Z"><point lat="38.889300" lon="-77.035300" hae="0" '
    'ce="0" le="9999999" /><detail><contact callsign="SH1 boundary" /><__safehouse site="sh1" role="boundary" '
    'updated="2026-11-18T14:00:00.000Z" /><link relation="c" type="b-m-p-w" point="38.889600,-77.035700" />'
    '<link relation="c" type="b-m-p-w" point="38.889600,-77.034900" /><link relation="c" type="b-m-p-w" '
    'point="38.889000,-77.034900" /><link relation="c" type="b-m-p-w" point="38.889000,-77.035700" />'
    '<link relation="c" type="b-m-p-w" point="38.889600,-77.035700" /><strokeColor value="-256" />'
    '<strokeWeight value="3.0" /><strokeStyle value="dashed" /><fillColor value="16711680" />'
    '<labels_on value="true" /></detail></event>'
)
CORNERS = [(38.8896, -77.0357), (38.8896, -77.0349), (38.8890, -77.0349), (38.8890, -77.0357)]


def test_the_boundary_is_the_shape_the_plugin_asked_for():
    sent = cot.boundary_event("sh1", CORNERS, datetime(2026, 11, 18, 14, 0, tzinfo=timezone.utc)).decode()
    assert sent[sent.index("<event") :] == PLUGIN_BOUNDARY


def test_the_boundary_is_addressed_to_its_mission_package():
    ev = ET.fromstring(cot.boundary_event("sh1", CORNERS, NOW, mission="sh1-mission"))
    assert ev.find("detail/marti/dest").get("mission") == "sh1-mission"
    assert len(ev.findall("detail/link")) == len(CORNERS) + 1  # closed: the first corner again
