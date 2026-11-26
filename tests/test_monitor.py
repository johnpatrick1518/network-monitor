"""Tests for debouncing, recovery timing, and cross-platform ping parsing."""

from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from app.models import Device
from app.monitor import PingResult, apply_ping, parse_latency, ping_device


def make_device(status: str = "up") -> Device:
    """Construct a detached model with explicit defaults for pure transition tests."""
    return Device(
        ip="192.168.1.3", status=status, consecutive_failures=0,
        failure_started_at=None, latency_ms=2.0,
    )


def test_three_failures_then_one_recovery() -> None:
    """Emit exactly one down event and include the first missed probe in downtime."""
    device = make_device()
    start = datetime(2026, 11, 26, 10)
    assert apply_ping(device, PingResult(False), start) is None
    assert apply_ping(device, PingResult(False), start + timedelta(seconds=60)) is None
    assert device.status == "up"
    down = apply_ping(device, PingResult(False), start + timedelta(seconds=120))
    assert down and down.kind == "device_down"
    assert device.status == "down"
    assert apply_ping(device, PingResult(False), start + timedelta(seconds=180)) is None
    up = apply_ping(device, PingResult(True, 1.5), start + timedelta(seconds=240))
    assert up and up.kind == "device_up" and up.downtime_seconds == 240
    assert device.status == "up"
    assert device.consecutive_failures == 0
    assert device.failure_started_at is None
    assert device.latency_ms == 1.5
    assert apply_ping(device, PingResult(True, 1.0), start + timedelta(seconds=300)) is None


def test_single_failure_is_reset_by_success() -> None:
    """An intermittent lost packet must not trigger an outage or recovery alert."""
    device = make_device()
    now = datetime(2026, 11, 26)
    apply_ping(device, PingResult(False), now)
    assert apply_ping(device, PingResult(True, 2), now + timedelta(seconds=60)) is None
    assert device.consecutive_failures == 0
    assert device.status == "up"


def test_initial_success_is_not_a_recovery() -> None:
    """A newly discovered device does not produce a misleading back-online message."""
    device = make_device("unknown")
    assert apply_ping(device, PingResult(True, 1), datetime(2026, 11, 26)) is None
    assert device.status == "up"


def test_local_ping_error_does_not_change_health() -> None:
    """Missing binaries and permission errors must not masquerade as outages."""
    device = make_device()
    assert apply_ping(device, PingResult(False, error="Missing ping"), datetime(2026, 11, 26)) is None
    assert device.status == "up"
    assert device.consecutive_failures == 0


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("64 bytes from 192.168.1.1: icmp_seq=1 ttl=64 time=1.23 ms", 1.23),
        ("Reply from 192.168.1.1: bytes=32 time<1ms TTL=128", 0.5),
        ("Antwort von 192.168.1.1: Zeit=2,5ms TTL=64", 2.5),
        ("rtt min/avg/max/mdev = 1.000/2.000/3.000/0.100 ms", 2.0),
        ("Request timed out.", None),
    ],
)
def test_parse_ping(output: str, expected: float | None) -> None:
    """Handle common Windows, Linux, and macOS ping output forms."""
    assert parse_latency(output) == expected


def test_windows_unreachable_is_not_online() -> None:
    """Windows return code zero alone does not prove an echo reply was received."""
    with patch("app.monitor.platform.system", return_value="Windows"), patch(
        "app.monitor.subprocess.run"
    ) as run:
        run.return_value.returncode = 0
        run.return_value.stdout = "Reply from 192.168.1.1: Destination host unreachable."
        run.return_value.stderr = ""
        assert ping_device("192.168.1.7", 1).is_up is False


def test_ping_refuses_public_address() -> None:
    """Even persisted or corrupted inventory cannot cause a public ping."""
    with patch("app.monitor.subprocess.run") as run:
        assert ping_device("8.8.8.8", 1).error
        run.assert_not_called()


def test_ping_missing_binary() -> None:
    """A missing system utility produces a recoverable local error."""
    with patch("app.monitor.subprocess.run", side_effect=FileNotFoundError):
        assert ping_device("192.168.1.2", 1).error
