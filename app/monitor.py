"""Cross-platform ping measurements, uptime aggregation, and state transitions."""

import ipaddress
import logging
import os
import platform
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import Device, HealthSample
from app.scanner import PRIVATE_NETWORKS, validate_subnet

logger = logging.getLogger(__name__)


@dataclass
class PingResult:
    """A ping result; errors are not silently treated as device outages."""

    is_up: bool
    latency_ms: float | None = None
    error: str | None = None


@dataclass
class Transition:
    """An alert-worthy debounced state change."""

    kind: str
    downtime_seconds: float | None = None


def parse_latency(output: str) -> float | None:
    """Extract Windows/Unix RTT, including localized decimal commas and sub-ms replies."""
    match = re.search(r"(?:time|temps|zeit|tiempo)\s*([=<])\s*([\d.,]+)\s*ms", output, re.I)
    if match:
        value = float(match.group(2).replace(",", "."))
        return value / 2 if match.group(1) == "<" else value
    summary = re.search(r"=\s*[\d.,]+/([\d.,]+)/[\d.,]+", output)
    if summary:
        return float(summary.group(1).replace(",", "."))
    # Windows translations retain the "number ms" form even if the label changes.
    reply = re.search(r"(?:ttl|hlim)[=:]\s*\d+", output, re.I)
    if reply:
        generic = re.search(r"([=<])\s*([\d.,]+)\s*ms", output, re.I)
        if generic:
            value = float(generic.group(2).replace(",", "."))
            return value / 2 if generic.group(1) == "<" else value
    return None


def ping_device(ip: str, timeout_seconds: int) -> PingResult:
    """Run one shell-free ICMP probe against a validated private IPv4 address."""
    try:
        address = ipaddress.ip_address(ip)
        if not isinstance(address, ipaddress.IPv4Address) or not any(
            address in network for network in PRIVATE_NETWORKS
        ):
            return PingResult(False, error="Refusing ping to a non-private IPv4 address.")
        system = platform.system()
        if system == "Windows":
            command = ["ping", "-n", "1", "-w", str(timeout_seconds * 1000), str(address)]
        elif system == "Darwin":
            command = ["ping", "-n", "-c", "1", "-W", str(timeout_seconds * 1000), str(address)]
        else:
            command = ["ping", "-n", "-c", "1", "-W", str(timeout_seconds), str(address)]
        environment = {**os.environ, "LC_ALL": "C", "LANG": "C"}
        completed = subprocess.run(
            command, capture_output=True, text=True, errors="replace",
            timeout=timeout_seconds + 3, env=environment, shell=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if system == "Windows" else 0,
        )
        output = completed.stdout + completed.stderr
        lowered = output.lower()
        if any(marker in lowered for marker in (
            "operation not permitted", "permission denied", "must be root", "cap_net_raw",
        )):
            return PingResult(False, error="System ping lacks ICMP permissions.")
        latency = parse_latency(output)
        # Windows may return code 0 for a gateway's "Destination host unreachable".
        reply = bool(re.search(r"(?:ttl|hlim)[=:]\s*\d+", output, re.I))
        is_up = completed.returncode == 0 and (system != "Windows" or reply or latency is not None)
        return PingResult(is_up, latency if is_up else None)
    except subprocess.TimeoutExpired:
        return PingResult(False)
    except (OSError, ValueError) as exc:
        return PingResult(False, error=f"System ping unavailable: {type(exc).__name__}.")


def apply_ping(
    device: Device, result: PingResult, now: datetime, threshold: int = 3
) -> Transition | None:
    """Debounce failures; record downtime from the first missed probe until recovery."""
    if result.error:
        return None
    previous = device.status
    device.last_checked = now
    device.latency_ms = result.latency_ms if result.is_up else None
    if result.is_up:
        downtime = (
            max(0.0, (now - device.failure_started_at).total_seconds())
            if device.failure_started_at else 0.0
        )
        device.status = "up"
        device.last_seen = now
        device.consecutive_failures = 0
        device.failure_started_at = None
        return Transition("device_up", downtime) if previous == "down" else None
    device.consecutive_failures += 1
    if device.failure_started_at is None:
        device.failure_started_at = now
    if device.consecutive_failures >= threshold:
        device.status = "down"
        if previous != "down":
            return Transition("device_down")
    return None


def calculate_uptime(up_count: int, total_count: int) -> float | None:
    """Return successful probes / all probes, or null when no observations exist."""
    return round(100.0 * up_count / total_count, 2) if total_count else None


def uptime_by_device(
    session: Session, now: datetime, device_ids: list[int] | None = None
) -> dict[int, dict[str, float | None]]:
    """Aggregate all three observation-based uptime windows in one indexed query."""
    windows = {"24h": now - timedelta(days=1), "7d": now - timedelta(days=7), "30d": now - timedelta(days=30)}
    columns = []
    for start in windows.values():
        columns.extend((
            func.sum(case((HealthSample.timestamp >= start, 1), else_=0)),
            func.sum(case(((HealthSample.timestamp >= start) & HealthSample.is_up, 1), else_=0)),
        ))
    query = select(HealthSample.device_id, *columns).where(
        HealthSample.timestamp >= windows["30d"]
    ).group_by(HealthSample.device_id)
    if device_ids is not None:
        if not device_ids:
            return {}
        query = query.where(HealthSample.device_id.in_(device_ids))
    result = {}
    for row in session.execute(query):
        result[row[0]] = {
            window: calculate_uptime(int(row[2 + index * 2] or 0), int(row[1 + index * 2] or 0))
            for index, window in enumerate(windows)
        }
    return result


def probe_devices(
    devices: list[tuple[int, str]], settings: Settings, subnet: str
) -> list[tuple[int, PingResult]]:
    """Probe known devices concurrently, strictly within the configured subnet."""
    network = validate_subnet(subnet)
    allowed = []
    for device_id, ip in devices:
        try:
            if ipaddress.ip_address(ip) in network:
                allowed.append((device_id, ip))
        except ValueError:
            logger.warning("Ignoring invalid stored device address for device %s.", device_id)
    with ThreadPoolExecutor(max_workers=settings.ping_workers, thread_name_prefix="ping") as pool:
        futures = [(device_id, pool.submit(ping_device, ip, settings.ping_timeout_seconds)) for device_id, ip in allowed]
        return [(device_id, future.result()) for device_id, future in futures]
