"""Tests for observation-based uptime and window boundaries."""

from datetime import datetime, timedelta

import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.models import Device, HealthSample
from app.monitor import calculate_uptime, uptime_by_device


@pytest.mark.parametrize(
    ("up", "total", "expected"),
    [(0, 0, None), (0, 10, 0.0), (10, 10, 100.0), (2, 3, 66.67), (1, 4, 25.0)],
)
def test_uptime_ratio(up: int, total: int, expected: float | None) -> None:
    """Zero observations must not be reported as either zero or full uptime."""
    assert calculate_uptime(up, total) == expected


def test_uptime_windows(sessions: sessionmaker[Session]) -> None:
    """Each window includes only measurements within its own retention boundary."""
    now = datetime(2026, 11, 26, 12)
    with sessions() as session:
        device = Device(ip="192.168.1.2")
        session.add(device)
        session.flush()
        for age, up in [(0, True), (1, False), (6, True), (29, False), (31, False)]:
            session.add(HealthSample(
                device_id=device.id, timestamp=now - timedelta(days=age), is_up=up
            ))
        session.commit()
        actual = uptime_by_device(session, now, [device.id])[device.id]
    assert actual == {"24h": 50.0, "7d": 66.67, "30d": 50.0}


def test_empty_inventory(sessions: sessionmaker[Session]) -> None:
    """An empty filtered device set must not aggregate the entire database."""
    with sessions() as session:
        assert uptime_by_device(session, datetime(2026, 11, 26), []) == {}
