"""Private-subnet discovery with Nmap and raw-ARP fallback."""

import ipaddress
import logging
import os
import re
import shutil
import socket
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import dns.exception
import dns.resolver
import psutil

logger = logging.getLogger(__name__)
PRIVATE_NETWORKS = tuple(
    ipaddress.ip_network(value) for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)


class DiscoveryError(RuntimeError):
    """A recoverable configuration, permission, or discovery-tool failure."""


@dataclass
class DiscoveredDevice:
    """A positively discovered network neighbor."""

    ip: str
    mac: str | None = None
    hostname: str | None = None
    vendor: str | None = None


def validate_subnet(value: str) -> ipaddress.IPv4Network:
    """Accept only RFC1918 IPv4 networks, with at most 65,536 addresses."""
    try:
        network = ipaddress.ip_network(value.strip(), strict=False)
    except ValueError as exc:
        raise DiscoveryError("Invalid SUBNET; use a private CIDR such as 192.168.1.0/24.") from exc
    if not isinstance(network, ipaddress.IPv4Network):
        raise DiscoveryError("Only private IPv4 subnets are supported.")
    if not any(network.subnet_of(private) for private in PRIVATE_NETWORKS):
        raise DiscoveryError("Public, loopback, link-local, and non-RFC1918 ranges are refused.")
    if network.prefixlen < 16:
        raise DiscoveryError("Subnet is too large; configure /16 or smaller (prefer /24).")
    return network


def autodetect_subnet() -> ipaddress.IPv4Network:
    """Prefer the default-route interface, then another active RFC1918 interface."""
    preferred_ip: str | None = None
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as connection:
            # UDP connect selects a route; it does not send a packet or scan this address.
            connection.connect(("192.0.2.1", 9))
            preferred_ip = connection.getsockname()[0]
    except OSError:
        pass
    candidates: list[tuple[bool, str, ipaddress.IPv4Network]] = []
    stats = psutil.net_if_stats()
    for interface, addresses in psutil.net_if_addrs().items():
        if interface not in stats or not stats[interface].isup:
            continue
        for address in addresses:
            if address.family != socket.AF_INET or not address.netmask:
                continue
            try:
                network = validate_subnet(f"{address.address}/{address.netmask}")
            except DiscoveryError:
                continue
            candidates.append((address.address != preferred_ip, interface, network))
    if not candidates:
        raise DiscoveryError("No active private IPv4 subnet found. Set SUBNET in .env.")
    candidates.sort(key=lambda item: (item[0], item[1]))
    network = candidates[0][2]
    logger.info("Auto-detected subnet %s on %s.", network, candidates[0][1])
    return network


def normalize_mac(value: str | None) -> str | None:
    """Normalize a valid unicast MAC; do not merge devices with invalid addresses."""
    if not value:
        return None
    cleaned = re.sub(r"[^0-9a-fA-F]", "", value)
    if len(cleaned) != 12 or cleaned in {"000000000000", "ffffffffffff"}:
        return None
    if int(cleaned[:2], 16) & 1:
        return None
    return ":".join(cleaned[index:index + 2] for index in range(0, 12, 2)).upper()


def reverse_hostname(ip: str) -> str | None:
    """Resolve a reverse-DNS name using the system DNS configuration and a deadline."""
    try:
        answer = dns.resolver.resolve_address(ip, lifetime=1.0, search=False)
        return str(answer[0]).rstrip(".")[:255]
    except (dns.exception.DNSException, OSError):
        return None


def find_nmap_executable() -> str:
    """Find Nmap even when a running Windows process has an older PATH."""
    executable = shutil.which("nmap")
    if executable:
        return executable
    candidates = [
        Path("/usr/bin/nmap"), Path("/usr/local/bin/nmap"),
        Path("/opt/homebrew/bin/nmap"), Path("/opt/local/bin/nmap"),
    ]
    for variable in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        directory = os.environ.get(variable)
        if directory:
            candidates.append(Path(directory) / "Nmap" / "nmap.exe")
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    raise FileNotFoundError("Nmap is not installed or is not in a standard installation directory.")


def nmap_scan(network: ipaddress.IPv4Network) -> list[DiscoveredDevice]:
    """Perform host discovery only; Nmap uses ARP on directly attached Ethernet."""
    import nmap

    scanner = nmap.PortScanner(nmap_search_path=(find_nmap_executable(),))
    scanner.scan(hosts=str(network), arguments="-sn -n", timeout=90)
    devices = []
    for ip in scanner.all_hosts():
        host = scanner[ip]
        if host.state() != "up" or ipaddress.ip_address(ip) not in network:
            continue
        mac = normalize_mac(host.get("addresses", {}).get("mac"))
        devices.append(DiscoveredDevice(ip=ip, mac=mac))
    return devices


def scapy_scan(network: ipaddress.IPv4Network) -> list[DiscoveredDevice]:
    """Broadcast ARP on a directly attached subnet; requires raw-socket privileges."""
    from scapy.all import ARP, Ether, conf, srp

    probe = str(next(network.hosts(), network.network_address))
    interface, _, gateway = conf.route.route(probe)
    if gateway != "0.0.0.0":
        raise DiscoveryError("Scapy ARP fallback only works on a directly attached subnet.")
    answered, _ = srp(
        Ether(dst="ff:ff:ff:ff:ff:ff") / ARP(pdst=str(network)),
        iface=interface, timeout=3, retry=1, verbose=False,
    )
    found: dict[str, DiscoveredDevice] = {}
    for _, reply in answered:
        try:
            address = ipaddress.ip_address(reply.psrc)
        except ValueError:
            continue
        if address in network:
            found[str(address)] = DiscoveredDevice(str(address), normalize_mac(reply.hwsrc))
    return list(found.values())


def discover(network: ipaddress.IPv4Network) -> list[DiscoveredDevice]:
    """Discover hosts, fall back safely, and enrich results using offline OUI data."""
    # Validate again at the execution boundary, not only at configuration load.
    network = validate_subnet(str(network))
    try:
        devices = nmap_scan(network)
    except Exception as exc:
        logger.warning("Nmap unavailable or failed (%s); trying Scapy ARP.", type(exc).__name__)
        try:
            devices = scapy_scan(network)
        except Exception as fallback_exc:
            raise DiscoveryError(
                "Discovery failed. Install Nmap or allow raw ARP sockets "
                "(Administrator/root and Npcap on Windows). "
                f"Fallback error: {type(fallback_exc).__name__}."
            ) from fallback_exc
    parser = None
    try:
        from manuf import manuf

        parser = manuf.MacParser(update=False)
    except Exception:
        logger.warning("Offline OUI database unavailable; vendor fields will be empty.")
    with ThreadPoolExecutor(max_workers=16, thread_name_prefix="reverse-dns") as pool:
        names = pool.map(reverse_hostname, [device.ip for device in devices])
        for device, hostname in zip(devices, names):
            device.hostname = hostname
            if parser and device.mac:
                device.vendor = parser.get_manuf_long(device.mac) or parser.get_manuf(device.mac)
    return devices
