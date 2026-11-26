"""Demo safety and deterministic simulation tests with every real adapter forbidden."""

import os
from collections.abc import Generator
from datetime import datetime
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app import main
from app.alerts import AlertService
from app.config import Settings
from app.demo import main as launch_demo
from app.demo_data import DEMO_SUBNET, demo_probe_devices, seed_demo
from app.models import Device, Event, HealthSample
from app.scheduler import MonitorService


def demo_settings() -> Settings:
    """Use only in-memory storage and fake provider credentials for demo tests."""
    return Settings(
        _env_file=None, demo_mode=True, demo_database_url="sqlite:///:memory:",
        telegram_bot_token="fake-token", telegram_chat_id="fake-chat",
        smtp_host="smtp.invalid", smtp_from="sender@example.invalid", smtp_to="test@example.invalid",
    )


def test_demo_database_is_separate() -> None:
    """Enabling demo mode overrides an existing live SQLite configuration."""
    settings = Settings(
        _env_file=None, demo_mode=True, database_url="sqlite:///live.db",
        demo_database_url="sqlite:///demo.db",
    )
    assert settings.database_url == "sqlite:///demo.db"


def test_demo_rejects_shared_database() -> None:
    """An explicitly shared live/demo file must be rejected instead of seeded."""
    with pytest.raises(ValidationError, match="must differ"):
        Settings(
            _env_file=None, demo_mode=True, database_url="sqlite:///shared.db",
            demo_database_url="sqlite:///shared.db",
        )


def test_seed_is_idempotent_and_preserves_edits(sessions: sessionmaker[Session]) -> None:
    """Repeat launches must not duplicate fixtures or overwrite a reviewer's edits."""
    seed_demo(sessions, datetime(2026, 11, 26, 12))
    with sessions() as session:
        device = session.scalar(select(Device))
        device.friendly_name = "Reviewer edit"
        session.commit()
    seed_demo(sessions)
    with sessions() as session:
        assert session.scalar(select(func.count(Device.id))) == 8
        assert session.scalar(select(func.count(HealthSample.id))) == 8 * 2016
        assert session.scalar(select(Device)).friendly_name == "Reviewer edit"


def test_demo_simulation_is_repeatable() -> None:
    """Identical ticks produce identical observations without random state."""
    devices = [(1, "192.168.50.30"), (2, "192.168.50.10")]
    assert demo_probe_devices(devices, 0) == demo_probe_devices(devices, 0)
    assert demo_probe_devices(devices, 0)[0][1].is_up is False
    assert demo_probe_devices(devices, 4)[0][1].is_up is True


def test_demo_jobs_never_touch_real_adapters(sessions: sessionmaker[Session]) -> None:
    """Discovery, DNS/interface detection, pings, Telegram, and SMTP stay unused."""
    service = MonitorService(demo_settings(), sessions)
    seed_demo(sessions)
    with patch("app.scheduler.autodetect_subnet") as autodetect, patch(
        "app.scheduler.discover"
    ) as discover, patch("app.scheduler.probe_devices") as probe, patch(
        "app.scheduler.detect_network_info"
    ) as network_info, patch("app.alerts.httpx.Client") as telegram, patch(
        "app.alerts.smtplib.SMTP"
    ) as smtp, patch("app.alerts.smtplib.SMTP_SSL") as smtp_ssl:
        service.scan()
        service.refresh_network_info()
        for _ in range(8):
            service.monitor()
        for adapter in (autodetect, discover, probe, network_info, telegram, smtp, smtp_ssl):
            adapter.assert_not_called()
    snapshot = service.snapshot()
    assert snapshot["demo_mode"] is True
    assert snapshot["subnet"] == DEMO_SUBNET
    assert snapshot["wifi_ssid"] == "Signal Demo Wi-Fi"
    assert snapshot["last_scan_error"] is None
    with sessions() as session:
        assert session.scalar(select(func.count(HealthSample.id))) == 8 * (2016 + 9)
        assert session.scalar(select(Event).where(Event.kind == "device_up")).device_id


def test_demo_alerts_are_simulated_and_rate_limited(sessions: sessionmaker[Session]) -> None:
    """Configured real-looking channels cannot escape the demo alert boundary."""
    alerts = AlertService(demo_settings(), sessions)
    with patch.object(alerts, "_telegram") as telegram, patch.object(alerts, "_email") as email:
        assert alerts.notify("test", "Demo") == {"status": "simulated", "demo": "simulated"}
        assert alerts.notify("test", "Demo") == {"status": "cooldown"}
        telegram.assert_not_called()
        email.assert_not_called()
    assert alerts._telegram("Direct call") == "simulated"
    assert alerts._email("Direct call") == "simulated"


def test_demo_lifespan_and_api(sessions: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise actual startup orchestration and API rendering without scheduler threads."""
    settings = demo_settings()
    settings.dashboard_username = "demo-reviewer"
    settings.dashboard_password = "test-only-password"
    monkeypatch.setattr(main, "settings", settings)
    monkeypatch.setattr(main, "SessionLocal", sessions)
    monkeypatch.setattr(main, "engine", sessions.kw["bind"])

    def override_db() -> Generator[Session, None, None]:
        """Share the isolated test database with API handlers."""
        with sessions() as session:
            yield session

    from app.database import get_db

    main.app.dependency_overrides[get_db] = override_db
    try:
        with patch("app.scheduler.BackgroundScheduler.start"):
            with TestClient(main.app) as client:
                client.auth = ("demo-reviewer", "test-only-password")
                summary = client.get("/api/summary")
                assert summary.status_code == 200
                assert summary.json()["demo_mode"] is True
                assert summary.json()["total"] == 8
                devices = client.get("/api/devices").json()
                assert len(devices) == 8
                identifier = devices[0]["id"]
                assert client.get(f"/api/devices/{identifier}/history?range=7d").json()["samples"]
                assert client.post("/api/alerts/test").json()["status"] == "simulated"
    finally:
        main.app.dependency_overrides.clear()


def test_demo_launcher_is_loopback_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """The quickstart generates temporary credentials without changing the real .env."""
    monkeypatch.setattr("sys.argv", ["app.demo", "--port", "8123"])
    with patch.dict(os.environ, {}), patch(
        "app.demo.secrets.token_urlsafe", return_value="temporary-test-password"
    ), patch("uvicorn.run") as run:
        launch_demo()
        assert os.environ["DEMO_MODE"] == "true"
        assert os.environ["DASHBOARD_USERNAME"] == "demo"
        assert os.environ["DASHBOARD_PASSWORD"] == "temporary-test-password"
        run.assert_called_once_with(
            "app.main:app", host="127.0.0.1", port=8123, workers=1
        )
