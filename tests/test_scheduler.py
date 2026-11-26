"""Coordination tests for discovery identity, failure safety, and retention."""

from datetime import timedelta
from unittest.mock import patch

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.database import utcnow
from app.models import Device, Event, HealthSample
from app.monitor import PingResult
from app.scanner import DiscoveredDevice, DiscoveryError
from app.scheduler import MonitorService


def service_for(sessions: sessionmaker[Session]) -> MonitorService:
    """Create a service without starting threads or performing real networking."""
    return MonitorService(Settings(_env_file=None, demo_mode=False, subnet="192.168.1.0/24"), sessions)


def test_failed_scan_does_not_mark_devices_down(sessions: sessionmaker[Session]) -> None:
    """A discovery-tool failure must preserve existing health state."""
    service = service_for(sessions)
    with sessions() as session:
        session.add(Device(ip="192.168.1.2", status="up"))
        session.commit()
    with patch("app.scheduler.discover", side_effect=DiscoveryError("Missing permissions")):
        service.scan()
    with sessions() as session:
        assert session.scalar(select(Device)).status == "up"
        assert session.scalar(select(Event)).kind == "scan_failed"
    assert service.snapshot()["scan_running"] is False


def test_mac_tracks_address_change_and_ip_reuse(sessions: sessionmaker[Session]) -> None:
    """MAC identities survive DHCP moves without inheriting another device's trust."""
    service = service_for(sessions)
    mac = "AA:BB:CC:11:22:33"
    with patch.object(service, "monitor"), patch.object(service.alerts, "notify"), patch(
        "app.scheduler.discover", return_value=[DiscoveredDevice("192.168.1.2", mac)]
    ):
        service.scan()
    with sessions() as session:
        original = session.scalar(select(Device))
        original.trusted = True
        identifier = original.id
        session.commit()
    with patch.object(service, "monitor"), patch.object(service.alerts, "notify"), patch(
        "app.scheduler.discover", return_value=[DiscoveredDevice("192.168.1.3", mac)]
    ):
        service.scan()
    with sessions() as session:
        assert session.get(Device, identifier).ip == "192.168.1.3"
        assert session.get(Device, identifier).trusted is True
    with patch.object(service, "monitor"), patch.object(service.alerts, "notify"), patch(
        "app.scheduler.discover", return_value=[DiscoveredDevice("192.168.1.3", "AC:BB:CC:44:55:66")]
    ):
        service.scan()
    with sessions() as session:
        active = session.scalar(select(Device).where(Device.active.is_(True)))
        assert active.id != identifier and active.trusted is False
        assert session.get(Device, identifier).active is False


def test_retention_keeps_recent_data(sessions: sessionmaker[Session]) -> None:
    """Hourly cleanup removes history older than 30 days and leaves newer samples."""
    service = service_for(sessions)
    with sessions() as session:
        device = Device(ip="192.168.1.2")
        session.add(device)
        session.flush()
        for age in (29, 31):
            session.add(HealthSample(
                device_id=device.id, timestamp=utcnow() - timedelta(days=age), is_up=True
            ))
        session.commit()
    service.cleanup()
    with sessions() as session:
        assert session.scalar(select(func.count(HealthSample.id))) == 1


def test_health_cycle_persists_and_alerts_once(sessions: sessionmaker[Session]) -> None:
    """Real service orchestration records each miss but not duplicate transitions."""
    service = service_for(sessions)
    service.subnet = "192.168.1.0/24"
    with sessions() as session:
        device = Device(ip="192.168.1.20", status="up")
        session.add(device)
        session.commit()
        identifier = device.id
    with patch("app.scheduler.probe_devices", return_value=[(identifier, PingResult(False))]), patch.object(
        service.alerts, "notify"
    ) as notify:
        for _ in range(4):
            service.monitor()
        notify.assert_called_once()
        assert notify.call_args.args[0] == f"device_down:{identifier}"
    with patch("app.scheduler.probe_devices", return_value=[(identifier, PingResult(True, 1.2))]), patch.object(
        service.alerts, "notify"
    ) as notify:
        service.monitor()
        notify.assert_called_once()
        assert notify.call_args.args[0] == f"device_up:{identifier}"
    with sessions() as session:
        assert session.scalar(select(func.count(HealthSample.id))) == 5
        assert session.scalar(select(func.count(Event.id))) == 2
        assert session.get(Device, identifier).status == "up"


def test_invalid_subnet_is_logged_not_executed(sessions: sessionmaker[Session]) -> None:
    """An invalid configured network leaves the dashboard alive without scanning."""
    service = MonitorService(Settings(_env_file=None, demo_mode=False, subnet="8.8.8.0/24"), sessions)
    with patch("app.scheduler.discover") as scan:
        service.scan()
        scan.assert_not_called()
    assert "refused" in str(service.snapshot()["last_scan_error"])
    assert service.snapshot()["scan_running"] is False
