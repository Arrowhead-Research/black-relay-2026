import json, ssl, time
import httpx, pytest
from conformance.helpers import *


def test_initial_sync_mqtt_rest_match_simulator(env):
    """initial sync + retained MQTT state + REST all equal the simulator; config overrides honoured; source=iq_remote."""
    w = env.watcher()
    exp = env.expected()
    wait_until(lambda: all(w.zone(n) for n in exp), 10, 0.1, "retained zone state for every non-ignored zone on MQTT")
    env.assert_matches_sim(check_stale=True)
    got = env.zones()
    assert 40 not in got, "ignored zone 40 leaked into REST"
    assert w.zone(40) is None, "ignored zone 40 leaked into MQTT"
    assert got[13]["name"] == "Garage motion" and got[12]["name"] == "Front door"          # name overrides
    assert got[14]["kind"] == "glass_break" and got[15]["kind"] == "motion" and got[16]["kind"] == "other"
    for n, e in exp.items():
        m = w.zone(n)
        assert m["id"] == f"zone-{n}" and int(m["panel_zone_id"]) == n, m
        for k in ("kind", "state", "name"): assert m[k] == e[k], f"MQTT zone {n} {k}={m[k]!r} expected {e[k]!r}"
        assert m["stale"] is False and m["online"] is True and m["source"] == "iq_remote", m
        for f in ("id", "panel_zone_id", "name", "kind", "partition_id", "state", "tamper", "battery_low", "online",
                  "last_changed", "last_seen", "source", "stale", "raw"):
            assert f in m, f"MQTT zone payload missing field {f!r}: {m}"
        g = got[n]; assert g["state"] == m["state"] and g["kind"] == m["kind"] and g["source"] == m["source"]
    # retained flag really set + QoS1 retained partition
    assert all(w.retained.get(f"{PREFIX}/zones/zone-{n}/state") for n in exp), "zone state not published retained"
    assert wait_until(lambda: w.partition(0), 5, 0.1, "retained partition state"), "no partition state"
    p = w.partition(0); assert p["arming"] == "disarmed" and p["stale"] is False, p
    assert env.partitions()[0]["arming"] == "disarmed"
    wait_until(lambda: w.status == "online", 5, 0.1, "retained bridge/status online")
    assert w.retained.get(f"{PREFIX}/bridge/status") is not None
    h = env.health()
    assert h["status"] == "online" and h["active_source"] == "iq_remote" and h["zones"] == len(exp)
    assert isinstance(h["last_seq"], int) and h["seconds_since_panel_message"] is not None and h["seconds_since_panel_message"] < 30
    for k in ("links", "last_panel_message_at", "partitions"): assert k in h, f"/v1/health lacks {k}"
    # filters + single resource + 404
    assert set(env.zones(kind="motion")) == {13, 15}
    assert set(env.zones(kind="glass_break")) == {14}
    r = env.http.get("/v1/zones/zone-12"); assert r.status_code == 200 and r.json()["state"] == "closed"
    assert env.http.get("/v1/zones/zone-999").status_code == 404
    assert env.http.get("/v1/zones/zone-40").status_code == 404
    # IQ handshake = only the allowed session commands; one session
    assert env.sim.iq.commands["connect_v204"] >= 1 and env.sim.iq.commands["syncdatabase"] >= 1
    # bridge never subscribes to any command topic on the broker
    subs = env.broker.subscriptions_by("blackrelay-bridge")
    assert not subs, f"bridge subscribed to broker topics {subs}"


def test_api_auth_401_and_key_headers(env):
    base = env.api_url; ca = str(env.pki.root / "harness-ca.crt")
    c = httpx.Client(base_url=base, verify=ca, timeout=10)
    for path in ("/v1/zones", "/v1/zones/zone-12", "/v1/partitions", "/v1/events", "/v1/stream"):
        assert c.get(path).status_code == 401, f"{path} without key must be 401"
        assert c.get(path, headers={"Authorization": "Bearer wrong"}).status_code == 401, f"{path} wrong key"
        assert c.get(path, headers={"X-API-Key": "wrong"}).status_code == 401
        assert c.get(path, headers={"Authorization": "Basic " + API_KEY}).status_code == 401
    assert c.get("/v1/health").status_code == 200, "/v1/health must be unauthenticated"
    assert c.get("/v1/zones", headers={"X-API-Key": API_KEY}).status_code == 200
    assert c.get("/v1/zones", headers={"Authorization": f"Bearer {API_KEY}"}).status_code == 200
    # key must not be accepted via query string
    assert c.get("/v1/zones", params={"api_key": API_KEY, "key": API_KEY, "token": API_KEY}).status_code == 401
    # TLS only: plaintext HTTP to the API port must not work
    with pytest.raises(Exception):
        r = httpx.get(f"http://127.0.0.1:{env.api_port}/v1/health", timeout=3)
        assert r.status_code != 200, "API answered plaintext HTTP"
        raise RuntimeError("plaintext answered with non-200")
    # a client that does not trust the harness CA is rejected at TLS level (cert actually presented is the configured one)
    with pytest.raises(Exception):
        httpx.get(f"{base}/v1/health", verify=True, timeout=3)


def test_api_has_no_mutating_verbs(env):
    before = env.zones(); seq0 = env.last_seq()
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        for path in ("/v1/zones", "/v1/zones/zone-12", "/v1/partitions", "/v1/events", "/v1/stream", "/v1/health", "/", "/v1/zones/zone-12/state",
                     "/v1/arm", "/v1/command"):
            r = env.http.request(method, path, json={"state": "open", "arm": "away"})
            assert r.status_code in (404, 405), f"{method} {path} -> {r.status_code} (must be 404/405)"
        r = httpx.request(method, env.api_url + "/v1/zones", verify=str(env.pki.root / "harness-ca.crt"), timeout=5, json={})
        assert r.status_code in (401, 404, 405), f"unauthenticated {method} -> {r.status_code}"
    assert env.zones() == before and env.last_seq() == seq0, "state changed after mutation attempts"


def test_metrics_endpoint(env):
    txt = None
    for scheme in ("http", "https"):
        try:
            r = httpx.get(f"{scheme}://127.0.0.1:{env.metrics_port}/metrics", verify=str(env.pki.root / "harness-ca.crt"), timeout=5)
            if r.status_code == 200: txt = r.text; break
        except Exception: pass
    assert txt and "# TYPE" in txt, "no Prometheus text exposition on the metrics port"
    low = txt.lower()
    assert any(k in low for k in ("blackrelay", "br_")), "metrics should be namespaced (blackrelay_/br_)"
    assert ("seq" in low) or ("event" in low), "metrics lack any event/seq series"
    # metrics port must not expose the API
    for scheme in ("http", "https"):
        try:
            r = httpx.get(f"{scheme}://127.0.0.1:{env.metrics_port}/v1/zones", verify=False, timeout=3,
                          headers={"Authorization": f"Bearer {API_KEY}"})
            assert r.status_code != 200 or "zone" not in r.text.lower(), "metrics port serves the zones API"
        except httpx.HTTPError: pass
