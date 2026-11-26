"""Cooldown, retry-backoff, and notification failure tests."""

from datetime import datetime, timedelta
from unittest.mock import patch

import httpx
import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.alerts import AlertService, cooldown_allows
from app.config import Settings
from app.models import AlertCooldown


@pytest.mark.parametrize(
    ("elapsed", "allowed"), [(0, False), (59, False), (60, True), (61, True)]
)
def test_cooldown_boundary(elapsed: int, allowed: bool) -> None:
    """The cooldown opens exactly at its configured boundary."""
    start = datetime(2026, 11, 26)
    assert cooldown_allows(start, start + timedelta(seconds=elapsed), 60) is allowed
    assert cooldown_allows(None, start, 60)


def alert_settings() -> Settings:
    """Use fake credentials and explicitly disable SMTP in notification tests."""
    return Settings(
        _env_file=None, demo_mode=False, telegram_bot_token="fake-token", telegram_chat_id="fake-chat",
        smtp_host="", alert_cooldown_seconds=60,
    )


def test_cooldown_persists_across_service_restart(sessions: sessionmaker[Session]) -> None:
    """A second service instance must respect an already delivered notification."""
    first = AlertService(alert_settings(), sessions)
    with patch.object(first, "_telegram", return_value="sent") as send:
        assert first.notify("device_down:1", "Down")["status"] == "sent"
        assert first.notify("device_down:1", "Down")["status"] == "cooldown"
        send.assert_called_once()
    second = AlertService(alert_settings(), sessions)
    with patch.object(second, "_telegram") as send:
        assert second.notify("device_down:1", "Down")["status"] == "cooldown"
        send.assert_not_called()


def test_failed_delivery_does_not_start_success_cooldown(sessions: sessionmaker[Session]) -> None:
    """A failed request uses short retry backoff, not a full successful-send cooldown."""
    service = AlertService(alert_settings(), sessions)
    with patch.object(service, "_telegram", return_value="failed"):
        assert service.notify("device_down:2", "Down")["status"] == "failed"
        assert service.notify("device_down:2", "Down")["status"] == "backoff"
    with sessions() as session:
        record = session.get(AlertCooldown, "device_down:2")
        assert record and record.last_sent_at is None
        record.last_attempt_at -= timedelta(seconds=31)
        session.commit()
    with patch.object(service, "_telegram", return_value="sent"):
        assert service.notify("device_down:2", "Down")["status"] == "sent"


def test_telegram_unreachable(sessions: sessionmaker[Session]) -> None:
    """A network failure is logged and returned rather than escaping the job."""
    service = AlertService(alert_settings(), sessions)
    with patch("app.alerts.httpx.Client.post", side_effect=httpx.ConnectError("offline")):
        assert service.notify("test", "Test")["status"] == "failed"
