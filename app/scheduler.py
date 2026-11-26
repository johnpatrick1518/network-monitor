"""In-process jobs and coordination for discovery, health checks, and retention."""

import logging
import threading
from datetime import timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from app.alerts import AlertService
from app.config import Settings
from app.database import iso_utc, utcnow
from app.demo_data import DEMO_SUBNET, demo_discover, demo_probe_devices, seed_demo
from app.models import AlertCooldown, Device, Event, HealthSample
from app.monitor import apply_ping, probe_devices
from app.network_info import NetworkInfo, detect_network_info
from app.scanner import DiscoveryError, autodetect_subnet, discover, validate_subnet

logger = logging.getLogger(__name__)


class MonitorService:
    """Own one scheduler, with non-overlapping discovery and ping jobs."""

    def __init__(self, settings: Settings, sessions: sessionmaker[Session]) -> None:
        """Create services; no threads or scans start before the app lifespan."""
        self.settings = settings
        self.sessions = sessions
        self.alerts = AlertService(settings, sessions)
        self.scheduler = BackgroundScheduler(
            timezone="UTC", job_defaults={"max_instances": 1, "coalesce": True, "misfire_grace_time": 60}
        )
        self._scan_lock = threading.Lock()
        self._ping_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self.subnet: str | None = None
        self.network_info = NetworkInfo()
        self.last_scan_at: str | None = None
        self.last_scan_error: str | None = None
        self._demo_tick = 0

    def start(self) -> None:
        """Start periodic jobs and schedule the first discovery immediately."""
        if self.settings.demo_mode:
            seed_demo(self.sessions)
            logger.info("Demo mode: synthetic devices only; network discovery and probes disabled.")
        try:
            self._resolve_subnet()
        except DiscoveryError as exc:
            self.last_scan_error = str(exc)
            logger.error("%s", exc)
        self.scheduler.add_job(
            self.scan, "interval", seconds=self.settings.scan_interval_seconds,
            id="discovery", next_run_time=utcnow(),
        )
        self.scheduler.add_job(
            self.monitor, "interval", seconds=self.settings.ping_interval_seconds,
            id="health", next_run_time=utcnow() + timedelta(seconds=5),
        )
        self.scheduler.add_job(
            self.cleanup, "interval", hours=1, id="retention", next_run_time=utcnow()
        )
        self.scheduler.add_job(
            self.refresh_network_info, "interval", seconds=30,
            id="network-info", next_run_time=utcnow(),
        )
        self.scheduler.start()

    def stop(self) -> None:
        """Wait for bounded running jobs before closing the database."""
        if self.scheduler.running:
            self.scheduler.shutdown(wait=True)

    def _resolve_subnet(self) -> str:
        """Resolve and validate a configured subnet or the current private interface."""
        if self.settings.demo_mode:
            network = validate_subnet(DEMO_SUBNET)
        else:
            network = validate_subnet(self.settings.subnet) if self.settings.subnet.strip() else autodetect_subnet()
        with self._state_lock:
            self.subnet = str(network)
        return str(network)

    def snapshot(self) -> dict[str, object]:
        """Read background job status for the summary endpoint."""
        with self._state_lock:
            return {
                "subnet": self.subnet,
                "demo_mode": self.settings.demo_mode,
                "wifi_ssid": self.network_info.ssid,
                "network_interface": self.network_info.interface,
                "wifi_signal_percent": self.network_info.signal_percent,
                "scan_running": self._scan_lock.locked(),
                "last_scan_at": self.last_scan_at,
                "last_scan_error": self.last_scan_error,
                "scheduler_running": self.scheduler.running,
            }

    def refresh_network_info(self) -> None:
        """Cache local Wi-Fi metadata off the API request path every 30 seconds."""
        with self._state_lock:
            subnet = self.subnet
        if self.settings.demo_mode:
            info = NetworkInfo(interface="simulated", ssid="Signal Demo Wi-Fi", signal_percent=96)
        else:
            info = detect_network_info(subnet) if subnet else NetworkInfo()
        with self._state_lock:
            self.network_info = info

    def request_scan(self) -> bool:
        """Reserve a scan before enqueueing it, rejecting overlapping requests."""
        if not self.scheduler.running or not self._scan_lock.acquire(blocking=False):
            return False
        try:
            self.scheduler.add_job(self._scan_reserved, id="manual-scan", replace_existing=True)
        except Exception:
            self._scan_lock.release()
            logger.exception("Could not enqueue discovery.")
            return False
        return True

    def scan(self) -> None:
        """Run scheduled discovery only when no other scan is queued or running."""
        if self._scan_lock.acquire(blocking=False):
            self._scan_reserved()

    def _scan_reserved(self) -> None:
        """Persist discoveries atomically and release the scan reservation on any error."""
        notifications: list[tuple[str, str, int | None]] = []
        try:
            subnet = self._resolve_subnet()
            found = demo_discover() if self.settings.demo_mode else discover(validate_subnet(subnet))
            now = utcnow()
            with self.sessions() as session:
                for neighbor in found:
                    device = session.scalar(select(Device).where(Device.mac == neighbor.mac)) if neighbor.mac else None
                    current = session.scalar(
                        select(Device).where(Device.ip == neighbor.ip, Device.active.is_(True))
                        .order_by(Device.id.desc())
                    )
                    if device is None and current and (
                        not neighbor.mac or not current.mac or current.mac == neighbor.mac
                    ):
                        device = current
                    if current and current is not device:
                        current.active = False
                    is_new = device is None
                    if is_new:
                        device = Device(ip=neighbor.ip, mac=neighbor.mac, first_seen=now, last_seen=now)
                        session.add(device)
                    device.ip = neighbor.ip
                    device.active = True
                    device.last_seen = now
                    if neighbor.mac:
                        device.mac = neighbor.mac
                    if neighbor.hostname:
                        device.hostname = neighbor.hostname
                    if neighbor.vendor:
                        device.vendor = neighbor.vendor
                    session.flush()
                    label = device.friendly_name or device.hostname or device.ip
                    if is_new:
                        message = f"Unknown device joined: {label} ({device.ip}), MAC {device.mac or 'unavailable'}."
                        event = Event(device_id=device.id, kind="new_device", message=message)
                        session.add(event)
                        session.flush()
                        notifications.append((f"unknown:{device.id}", message, event.id))
                    elif not device.trusted:
                        notifications.append((
                            f"unknown:{device.id}",
                            f"Device still Unknown: {label} ({device.ip}). Mark it Trusted in the dashboard.",
                            None,
                        ))
                prefix = "DEMO simulated scan" if self.settings.demo_mode else "Scan"
                session.add(Event(kind="scan_completed", message=f"{prefix} completed: {len(found)} device(s) found in {subnet}."))
                session.commit()
            with self._state_lock:
                self.last_scan_at = iso_utc(now)
                self.last_scan_error = None
            logger.info("Discovery completed: %s device(s) in %s.", len(found), subnet)
            # Populate health immediately instead of waiting for the next minute tick.
            self.monitor()
            for key, message, event_id in notifications:
                # Trust may have been changed while a large discovery was in progress.
                device_id = int(key.split(":")[1])
                with self.sessions() as session:
                    device = session.get(Device, device_id)
                    if not device or device.trusted or not device.active:
                        continue
                self.alerts.notify(key, message, event_id)
        except Exception as exc:
            message = str(exc) if isinstance(exc, DiscoveryError) else f"Discovery failed ({type(exc).__name__}); see server logs."
            logger.exception("Discovery failed: %s", message)
            with self._state_lock:
                self.last_scan_error = message
            self._record_failure("scan_failed", message)
        finally:
            self._scan_lock.release()

    def _record_failure(self, kind: str, message: str) -> None:
        """Record a background error without letting database errors stop scheduling."""
        try:
            with self.sessions() as session:
                session.add(Event(kind=kind, message=message))
                session.commit()
        except Exception:
            logger.exception("Could not persist background job failure.")

    def monitor(self) -> None:
        """Ping active devices, persist raw observations, and notify on state changes."""
        if not self._ping_lock.acquire(blocking=False):
            return
        try:
            with self._state_lock:
                subnet = self.subnet
            if not subnet:
                return
            with self.sessions() as session:
                devices = [
                    (device_id, ip)
                    for device_id, ip in session.execute(
                        select(Device.id, Device.ip).where(Device.active.is_(True))
                    )
                ]
            original_ips = dict(devices)
            if self.settings.demo_mode:
                results = demo_probe_devices(devices, self._demo_tick)
                self._demo_tick += 1
            else:
                results = probe_devices(devices, self.settings, subnet)
            now = utcnow()
            notifications: list[tuple[str, str, int]] = []
            errors: set[str] = set()
            with self.sessions() as session:
                for device_id, result in results:
                    if result.error:
                        errors.add(result.error)
                        continue
                    device = session.get(Device, device_id)
                    if not device or not device.active:
                        continue
                    # An address may have changed during a concurrent discovery.
                    if device.ip != original_ips.get(device_id):
                        continue
                    transition = apply_ping(device, result, now, self.settings.down_threshold)
                    session.add(HealthSample(
                        device_id=device.id, timestamp=now,
                        is_up=result.is_up, latency_ms=result.latency_ms,
                    ))
                    if transition:
                        label = device.friendly_name or device.hostname or device.ip
                        if transition.kind == "device_down":
                            message = f"Device DOWN: {label} ({device.ip}) after {device.consecutive_failures} failed pings."
                        else:
                            duration = timedelta(seconds=round(transition.downtime_seconds or 0))
                            message = f"Device UP: {label} ({device.ip}); down for {duration}."
                        event = Event(device_id=device.id, kind=transition.kind, message=message)
                        session.add(event)
                        session.flush()
                        notifications.append((f"{transition.kind}:{device.id}", message, event.id))
                session.commit()
            for error in errors:
                logger.error("%s No outage samples recorded for affected probes.", error)
            for key, message, event_id in notifications:
                self.alerts.notify(key, message, event_id)
        except Exception:
            logger.exception("Health monitoring cycle failed; the next cycle will retry.")
        finally:
            self._ping_lock.release()

    def cleanup(self) -> None:
        """Delete expired samples, events, and unused cooldown state hourly."""
        cutoff = utcnow() - timedelta(days=self.settings.history_retention_days)
        try:
            with self.sessions() as session:
                session.execute(delete(HealthSample).where(HealthSample.timestamp < cutoff))
                session.execute(delete(Event).where(Event.created_at < cutoff))
                session.execute(delete(AlertCooldown).where(AlertCooldown.last_attempt_at < cutoff))
                session.commit()
        except Exception:
            logger.exception("History retention failed; the next hourly job will retry.")
