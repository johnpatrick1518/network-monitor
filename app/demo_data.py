"""Deterministic synthetic inventory and health observations; never accesses the network."""

import math
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.database import utcnow
from app.models import Device, Event, HealthSample
from app.monitor import PingResult
from app.scanner import DiscoveredDevice

DEMO_SUBNET = "192.168.50.0/24"
# Deliberately fictional hosts and locally administered MACs, not a real network capture.
DEMO_DEVICES = (
    (1, "Demo Gateway", "gateway.demo.invalid", ["router"], True, 1.0),
    (10, "Home Server", "server.demo.invalid", ["server"], True, 1.8),
    (20, "Backup NAS", "nas.demo.invalid", ["storage"], True, 2.5),
    (30, "Office Printer", "printer.demo.invalid", ["printer"], True, 8.0),
    (40, "Work Laptop", "laptop.demo.invalid", ["computer"], True, 4.0),
    (50, "Demo Phone", "phone.demo.invalid", ["phone"], True, 14.0),
    (60, "Living Room Speaker", "speaker.demo.invalid", ["iot"], False, 11.0),
    (70, "Unrecognized Device", "unknown.demo.invalid", [], False, 6.0),
)


def demo_discover() -> list[DiscoveredDevice]:
    """Return fictional neighbors without invoking Nmap, Scapy, or DNS."""
    return [
        DiscoveredDevice(
            ip=f"192.168.50.{suffix}", mac=f"02:00:00:00:50:{suffix:02X}",
            hostname=hostname, vendor="Synthetic demo vendor",
        )
        for suffix, _, hostname, _, _, _ in DEMO_DEVICES
    ]


def demo_probe_devices(devices: list[tuple[int, str]], tick: int) -> list[tuple[int, PingResult]]:
    """Simulate deterministic latency and printer outages in eight-check cycles."""
    latencies = {suffix: latency for suffix, _, _, _, _, latency in DEMO_DEVICES}
    results = []
    for device_id, ip in devices:
        suffix = int(ip.rsplit(".", 1)[-1])
        is_up = not (suffix == 30 and tick % 8 < 4)
        # One isolated lost packet demonstrates why debouncing is useful.
        if suffix == 50 and tick % 12 == 6:
            is_up = False
        latency = round(max(0.3, latencies.get(suffix, 5.0) + math.sin(tick + suffix) * 0.8), 2)
        results.append((device_id, PingResult(is_up, latency if is_up else None)))
    return results


def seed_demo(sessions: sessionmaker[Session], now: datetime | None = None) -> None:
    """Seed seven days of synthetic history once, preserving later demo edits."""
    now = now or utcnow()
    with sessions() as session:
        if session.scalar(select(Device.id).limit(1)) is not None:
            return
        devices = []
        samples = []
        for suffix, name, hostname, tags, trusted, latency in DEMO_DEVICES:
            down = suffix == 30
            device = Device(
                ip=f"192.168.50.{suffix}", mac=f"02:00:00:00:50:{suffix:02X}",
                hostname=hostname, vendor="Synthetic demo vendor",
                friendly_name=name, tags=tags, trusted=trusted,
                first_seen=now - timedelta(days=7),
                last_seen=now - timedelta(minutes=20) if down else now,
                last_checked=now, status="down" if down else "up",
                latency_ms=None if down else latency, consecutive_failures=3 if down else 0,
                failure_started_at=now - timedelta(minutes=20) if down else None,
            )
            session.add(device)
            session.flush()
            devices.append(device)
            for index in range(2016):
                timestamp = now - timedelta(minutes=(2015 - index) * 5)
                missed = (down and index >= 2011) or (suffix == 40 and 2009 <= index < 2013)
                samples.append(HealthSample(
                    device_id=device.id, timestamp=timestamp, is_up=not missed,
                    latency_ms=None if missed else round(max(0.3, latency + math.sin(index / 9 + suffix) * 0.8), 2),
                ))
        session.add_all(samples)
        session.add_all([
            Event(
                kind="scan_completed", created_at=now - timedelta(minutes=25),
                message="DEMO: discovered 8 fictional devices; no network scan was performed.",
            ),
            Event(
                device_id=devices[3].id, kind="device_down", created_at=now - timedelta(minutes=18),
                message="DEMO: Office Printer went offline after three simulated failed pings.",
                notification_status="simulated",
            ),
            Event(
                device_id=devices[4].id, kind="device_up", created_at=now - timedelta(minutes=10),
                message="DEMO: Work Laptop recovered from a simulated 20-minute outage.",
                notification_status="simulated",
            ),
            Event(
                device_id=devices[7].id, kind="new_device", created_at=now - timedelta(minutes=3),
                message="DEMO: fictional Unknown device joined. Try marking it Trusted.",
                notification_status="simulated",
            ),
        ])
        session.commit()
