"""Read the monitoring host's Wi-Fi name without probing other networks."""

import ipaddress
import logging
import os
import platform
import re
import socket
import subprocess
from dataclasses import dataclass

import psutil

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class NetworkInfo:
    """Best-effort identity of the local interface on the monitored subnet."""

    interface: str | None = None
    ssid: str | None = None
    signal_percent: int | None = None


def run_network_command(command: list[str]) -> str:
    """Read OS network metadata with a timeout and no shell or elevation."""
    try:
        result = subprocess.run(
            command, capture_output=True, text=True,
            encoding="oem" if os.name == "nt" else "utf-8", errors="replace",
            timeout=5, shell=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return result.stdout if result.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        logger.debug("Local Wi-Fi metadata unavailable from %s.", command[0])
        return ""


def subnet_interfaces(subnet: str) -> list[str]:
    """Find active local IPv4 interfaces that actually belong to the monitored subnet."""
    network = ipaddress.ip_network(subnet, strict=False)
    stats = psutil.net_if_stats()
    interfaces = []
    for name, addresses in psutil.net_if_addrs().items():
        if name not in stats or not stats[name].isup:
            continue
        for address in addresses:
            if address.family == socket.AF_INET and ipaddress.ip_address(address.address) in network:
                interfaces.append(name)
                break
    return interfaces


def parse_windows_wifi(output: str, interfaces: list[str]) -> NetworkInfo:
    """Extract the connected SSID, never BSSID or a different subnet's adapter."""
    records: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in output.splitlines():
        match = re.match(r"^\s*(Name|SSID|Signal)\s*:\s*(.*?)\s*$", line, re.I)
        if not match:
            continue
        key, value = match.group(1).lower(), match.group(2)
        if key == "name":
            if current:
                records.append(current)
            current = {"name": value}
        else:
            current[key] = value
    if current:
        records.append(current)
    for record in records:
        if record.get("name") not in interfaces or not record.get("ssid"):
            continue
        signal = re.fullmatch(r"(\d{1,3})\s*%", record.get("signal", ""))
        percent = int(signal.group(1)) if signal else None
        return NetworkInfo(
            interface=record["name"], ssid=record["ssid"],
            signal_percent=percent if percent is not None and 0 <= percent <= 100 else None,
        )
    return NetworkInfo(interface=interfaces[0] if interfaces else None)


def detect_network_info(subnet: str) -> NetworkInfo:
    """Read the local SSID where supported; gracefully fall back to the interface name."""
    try:
        interfaces = subnet_interfaces(subnet)
        if not interfaces:
            return NetworkInfo()
        system = platform.system()
        if system == "Windows":
            return parse_windows_wifi(
                run_network_command(["netsh", "wlan", "show", "interfaces"]), interfaces
            )
        for interface in interfaces:
            ssid = ""
            if system == "Linux":
                ssid = run_network_command(["iwgetid", interface, "--raw"]).strip()
                if not ssid:
                    output = run_network_command([
                        "nmcli", "-t", "--escape", "no", "-f", "IN-USE,SSID",
                        "device", "wifi", "list", "ifname", interface, "--rescan", "no",
                    ])
                    ssid = next((line[2:] for line in output.splitlines() if line.startswith("*:")), "")
            elif system == "Darwin":
                output = run_network_command(["/usr/sbin/ipconfig", "getsummary", interface])
                match = re.search(r"^\s*SSID\s*:\s*(.+?)\s*$", output, re.M)
                if match:
                    ssid = match.group(1)
                if not ssid:
                    output = run_network_command(["/usr/sbin/networksetup", "-getairportnetwork", interface])
                    match = re.search(r"^Current Wi-Fi Network:\s*(.+)$", output, re.M)
                    ssid = match.group(1).strip() if match else ""
            if ssid and ssid.casefold() not in {"<redacted>", "redacted", "<hidden>", "(null)"}:
                return NetworkInfo(interface=interface, ssid=ssid[:128])
        return NetworkInfo(interface=interfaces[0])
    except Exception:
        logger.debug("Could not read local Wi-Fi identity.", exc_info=True)
        return NetworkInfo()
