import hmac
import logging
import re

import pytest
from conftest import TOKEN
from fastapi.testclient import TestClient

from relayapi.settings import Settings, SettingsError

ENV = {
    "RELAY_API_TOKEN": TOKEN,
    "RELAY_BROKER_USER": "relay",
    "RELAY_BROKER_PASSWORD": "pw",
    "RELAY_MEDIAMTX_API_URL": "http://mediamtx:9997",
    "RELAY_MEDIAMTX_API_USER": "relay-api",
    "RELAY_MEDIAMTX_API_PASSWORD": "ctl",
    "RELAY_SITE_SUBNET": "192.168.8.0/24",
    "RELAY_API_BROKER_USER": "relay-api",
    "RELAY_API_BROKER_PASSWORD": "api-pw",
    "RELAY_MQTT_CLIENT_ID": "relay-api-secret",
}


def concrete(path):
    return re.sub(r"\{[^}]+\}", "x", path)


def test_every_route_requires_a_login(relay):
    routes = [r for r in relay.app.routes if getattr(r, "methods", None)]
    assert {concrete(r.path) for r in routes} >= {
        "/nodes",
        "/events",
        "/events/stream",
        "/nodes/x/snapshot/x.jpg",
        "/nodes/x/frigate/x",
    }
    anonymous = TestClient(relay.app)
    for route in routes:
        for method in route.methods:
            url = concrete(route.path)
            for headers in (
                {},
                {"Authorization": "Bearer wrong"},
                {"Authorization": TOKEN},
                {"Authorization": f"Basic {TOKEN}"},
                {"Authorization": "Bearer t\xf6ken".encode("latin-1")},
            ):
                response = anonymous.request(method, url, headers=headers)
                assert response.status_code == 401, (method, url, headers)
    assert relay.node.requests == []


def test_no_documentation_route_is_left_open(relay):
    anonymous = TestClient(relay.app)
    for path in ("/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"):
        assert anonymous.get(path).status_code in (401, 404)
        assert relay.http.get(path).status_code == 404


def test_the_right_token_reads(relay):
    assert relay.http.get("/nodes").status_code == 200


def test_a_lowercase_bearer_scheme_is_accepted(relay):
    client = TestClient(relay.app)
    assert client.get("/nodes", headers={"Authorization": f"bearer {TOKEN}"}).status_code == 200


def test_a_trailing_slash_is_not_redirected_past_the_login(relay):
    response = TestClient(relay.app).get("/nodes/", follow_redirects=False)
    assert response.status_code in (401, 404)


@pytest.mark.parametrize("missing", ["", "   ", None])
def test_a_missing_token_stops_startup(missing):
    env = {k: v for k, v in ENV.items() if k != "RELAY_API_TOKEN"}
    if missing is not None:
        env["RELAY_API_TOKEN"] = missing
    with pytest.raises(SettingsError, match="RELAY_API_TOKEN"):
        Settings.from_env(env)


def test_the_factory_refuses_to_build_an_open_app(monkeypatch):
    from relayapi.app import create_app

    monkeypatch.delenv("RELAY_API_TOKEN", raising=False)
    with pytest.raises(SettingsError, match="RELAY_API_TOKEN"):
        create_app()


@pytest.mark.parametrize(
    "name",
    [
        "RELAY_BROKER_USER",
        "RELAY_BROKER_PASSWORD",
        "RELAY_MEDIAMTX_API_URL",
        "RELAY_MEDIAMTX_API_USER",
        "RELAY_MEDIAMTX_API_PASSWORD",
        "RELAY_SITE_SUBNET",
        "RELAY_API_BROKER_USER",
        "RELAY_API_BROKER_PASSWORD",
        "RELAY_MQTT_CLIENT_ID",
    ],
)
def test_other_required_settings_stop_startup(name):
    env = {k: v for k, v in ENV.items() if k != name}
    with pytest.raises(SettingsError, match=name):
        Settings.from_env(env)


def test_settings_error_and_repr_hold_no_secret():
    settings = Settings.from_env(ENV)
    text = repr(settings)
    assert TOKEN not in text
    assert "ctl" not in text
    with pytest.raises(SettingsError) as error:
        Settings.from_env({**ENV, "RELAY_BROKER_PORT": "not-a-port"})
    assert TOKEN not in str(error.value)


def test_the_token_is_compared_in_constant_time(relay, monkeypatch):
    # Timing cannot be measured here, so this checks that the comparison used is the right one.
    calls = []
    real = hmac.compare_digest

    def spy(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr("relayapi.auth.hmac.compare_digest", spy)
    assert TestClient(relay.app).get("/nodes").status_code == 401
    client = TestClient(relay.app)
    client.get("/nodes", headers={"Authorization": f"Bearer {TOKEN}"})
    assert (TOKEN.encode(), TOKEN.encode()) in calls


@pytest.mark.parametrize(
    "subnet",
    [
        "",
        "banana",
        "192.168.8.1/24",  # host bits set
        "192.168.8.5",
        "192.168.8.0/31",
        "192.168.8.0/32",
        "fd00::/64",
        "0.0.0.0/0",
        "0.0.0.0/1",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "224.0.0.0/4",
        "240.0.0.0/4",
        "8.8.8.0/24",
        "8.0.0.0/8",
        "100.64.0.0/10",
        "11.0.0.0/8",
        "172.32.0.0/16",
        "192.167.0.0/16",
        "192.0.0.0/8",
        "172.0.0.0/8",
        "192.168.8.0/24\n",
    ],
)
def test_an_unusable_site_subnet_stops_startup(subnet):
    with pytest.raises(SettingsError, match="RELAY_SITE_SUBNET"):
        Settings.from_env({**ENV, "RELAY_SITE_SUBNET": subnet})


@pytest.mark.parametrize("subnet", ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "10.1.2.0/24"])
def test_every_private_range_can_be_a_site(subnet):
    assert str(Settings.from_env({**ENV, "RELAY_SITE_SUBNET": subnet}).site_subnet) == subnet


def test_a_site_subnet_overlapping_the_bundles_networks_stops_startup():
    with pytest.raises(SettingsError, match="RELAY_SITE_SUBNET"):
        Settings.from_env(
            {
                **ENV,
                "RELAY_SITE_SUBNET": "172.31.0.0/16",
                "RELAY_INTERNAL_SUBNETS": "172.31.250.0/29",
            }
        )


@pytest.mark.parametrize("value", ["banana", "172.31.250.0/99", "fd00::/64", "172.31.250.1/29"])
def test_a_bad_internal_subnet_stops_startup(value):
    with pytest.raises(SettingsError, match="RELAY_INTERNAL_SUBNETS"):
        Settings.from_env({**ENV, "RELAY_INTERNAL_SUBNETS": value})


@pytest.mark.parametrize("value", ["banana", "fd00::1", "192.168.8.1/32"])
def test_a_bad_central_ip_stops_startup(value):
    with pytest.raises(SettingsError, match="RELAY_CENTRAL_IP"):
        Settings.from_env({**ENV, "RELAY_CENTRAL_IP": value})


def test_the_api_needs_a_broker_login_of_its_own():
    with pytest.raises(SettingsError, match="RELAY_API_BROKER_USER"):
        Settings.from_env({**ENV, "RELAY_API_BROKER_USER": ENV["RELAY_BROKER_USER"]})


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "huge" * 3, "9" * 5000])
def test_a_bad_number_stops_startup(value):
    with pytest.raises(SettingsError, match="RELAY_STALE_AFTER_S"):
        Settings.from_env({**ENV, "RELAY_STALE_AFTER_S": value})


def test_urls_and_node_addresses_stay_out_of_the_log(relay):
    # httpx logs each request's URL, with its query, at INFO.
    assert logging.getLogger("httpx").level == logging.WARNING
