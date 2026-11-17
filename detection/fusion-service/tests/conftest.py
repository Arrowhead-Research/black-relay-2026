from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fusion_service.app import create_app
from fusion_service.settings import Settings

DETECTION = Path(__file__).resolve().parents[2]
TOKEN = "test-token-0123456789abcdef"
FIXED_NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def fixed_clock() -> datetime:
    return FIXED_NOW


@pytest.fixture
def settings(tmp_path, monkeypatch) -> Settings:
    monkeypatch.setenv("FUSION_API_TOKEN", TOKEN)
    return Settings(
        database=tmp_path / "fusion.sqlite3",
        camera_schemas=DETECTION / "camera-subsystem/bridge/schemas",
        rf_schemas=DETECTION / "rf-subsystem/schemas",
        physical_schemas=DETECTION / "physical-subsystem/schemas",
        brokers=[
            {
                "id": "test",
                "namespace": "test",
                "host": "127.0.0.1",
                "port": 1,
                "client_id": "fusion-test",
                "subsystems": ["camera", "rf", "physical"],
                "physical_node_id": "bridge-test",
                "site_id": "site-test",
            }
        ],
    )


@pytest.fixture
def client(settings):
    app = create_app(settings, start_mqtt=False, clock=fixed_clock)
    with TestClient(app) as client:
        yield client


@pytest.fixture
def auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}
