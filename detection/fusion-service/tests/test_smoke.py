def test_missing_token_is_401_with_error_body(client):
    response = client.get("/api/v1/events")
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert response.json()["error"]["code"] == "unauthorized"


def test_wrong_token_is_401(client):
    response = client.get("/api/v1/health", headers={"Authorization": "Bearer nope"})
    assert response.status_code == 401


def test_health_reports_storage_ready_and_broker_degraded(client, auth):
    body = client.get("/api/v1/health", headers=auth).json()
    assert body["status"] == "degraded"
    assert body["storage"]["ready"] is True
    assert [b["id"] for b in body["brokers"]] == ["test"]
    assert body["brokers"][0]["connected"] is False


def test_events_empty_page(client, auth):
    body = client.get("/api/v1/events", headers=auth).json()
    assert body == {"events": [], "next_cursor": 0, "has_more": False}


def test_events_cursor_ahead_is_400(client, auth):
    response = client.get("/api/v1/events", params={"after": 5}, headers=auth)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "cursor_ahead"


def test_invalid_query_is_422(client, auth):
    response = client.get("/api/v1/events", params={"limit": 0}, headers=auth)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"


def test_nodes_empty(client, auth):
    assert client.get("/api/v1/nodes", headers=auth).json() == {"nodes": []}


def test_unknown_route_is_404(client, auth):
    assert client.get("/api/v1/nothing", headers=auth).status_code == 404
