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


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "huge" * 3, "9" * 5000])
def test_a_bad_number_stops_startup(value):
    with pytest.raises(SettingsError, match="RELAY_STALE_AFTER_S"):
        Settings.from_env({**ENV, "RELAY_STALE_AFTER_S": value})
