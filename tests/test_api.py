"""Authenticated REST tests using an isolated database and disabled real jobs."""

from collections.abc import Generator
from datetime import timedelta
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app import main
from app.database import get_db, utcnow
from app.models import Device, HealthSample
from app.scheduler import MonitorService


@pytest.fixture
def client(sessions: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient, None, None]:
    """Disable scheduler startup and override every database dependency."""
    def override_db() -> Generator[Session, None, None]:
        """Use the test's SQLite session in every API request."""
        with sessions() as session:
            yield session

    monkeypatch.setattr(main, "SessionLocal", sessions)
    monkeypatch.setattr(main, "engine", sessions.kw["bind"])
    monkeypatch.setattr(main.settings, "dashboard_username", "test-user")
    monkeypatch.setattr(main.settings, "dashboard_password", "test-only-password")
    main.app.dependency_overrides[get_db] = override_db
    with patch.object(MonitorService, "start"), patch.object(MonitorService, "stop"):
        with TestClient(main.app) as test_client:
            yield test_client
    main.app.dependency_overrides.clear()


def auth() -> tuple[str, str]:
    """Return test-only Basic authentication credentials."""
    return "test-user", "test-only-password"


@pytest.mark.parametrize("path", ["/", "/style.css", "/app.js", "/docs", "/openapi.json", "/api/devices"])
def test_every_resource_requires_auth(client: TestClient, path: str) -> None:
    """Static files, docs, and API endpoints must all be protected."""
    assert client.get(path).status_code == 401
    assert client.get(path, auth=auth()).status_code == 200


def test_edit_and_history(client: TestClient, sessions: sessionmaker[Session]) -> None:
    """Inventory edits, aggregate history, and uptime serialization work together."""
    now = utcnow()
    with sessions() as session:
        device = Device(ip="192.168.1.10", status="up")
        session.add(device)
        session.flush()
        identifier = device.id
        session.add_all([
            HealthSample(device_id=identifier, timestamp=now - timedelta(minutes=2), is_up=True, latency_ms=2),
            HealthSample(device_id=identifier, timestamp=now - timedelta(minutes=1), is_up=False),
        ])
        session.commit()
    response = client.patch(
        f"/api/devices/{identifier}", auth=auth(),
        json={"friendly_name": " Office server ", "tags": ["Server", " server "], "trusted": True},
    )
    assert response.status_code == 200
    assert response.json()["friendly_name"] == "Office server"
    assert response.json()["tags"] == ["server"]
    assert response.json()["uptime_24h"] == 50
    assert response.json()["last_seen"].endswith("+00:00")
    history = client.get(f"/api/devices/{identifier}/history?range=1h", auth=auth())
    assert history.status_code == 200
    assert len(history.json()["samples"]) == 2
    assert client.get(f"/api/devices/{identifier}/history?range=bad", auth=auth()).status_code == 422
    summary = client.get("/api/summary", auth=auth()).json()
    assert summary["total"] == 1 and summary["unknown"] == 0
    assert client.get("/api/devices?tag=server&trusted=true", auth=auth()).json()[0]["id"] == identifier


def test_csv_formula_protection(client: TestClient, sessions: sessionmaker[Session]) -> None:
    """Exports must not turn inventory names into executable spreadsheet formulas."""
    with sessions() as session:
        session.add(Device(ip="192.168.1.3", friendly_name="=1+1"))
        session.commit()
    response = client.get("/api/export/devices.csv", auth=auth())
    assert response.status_code == 200
    assert "'=1+1" in response.text


def test_cross_site_mutation_rejected(client: TestClient) -> None:
    """Basic credentials do not authorize cross-origin browser actions."""
    response = client.post(
        "/api/scan", auth=auth(), headers={"Origin": "https://untrusted.example"}
    )
    assert response.status_code == 403


def test_missing_device_and_null_trust(client: TestClient, sessions: sessionmaker[Session]) -> None:
    """Invalid edits and missing IDs produce stable error responses."""
    assert client.get("/api/devices/9999", auth=auth()).status_code == 404
    with sessions() as session:
        device = Device(ip="192.168.1.4")
        session.add(device)
        session.commit()
        identifier = device.id
    assert client.patch(f"/api/devices/{identifier}", auth=auth(), json={"trusted": None}).status_code == 422
