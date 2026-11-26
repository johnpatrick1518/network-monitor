"""Private-subnet security and discovery fallback tests."""

from unittest.mock import patch
from pathlib import Path

import pytest

from app.scanner import DiscoveredDevice, DiscoveryError, discover, find_nmap_executable, normalize_mac, validate_subnet


@pytest.mark.parametrize("subnet", [
    "8.8.8.0/24", "0.0.0.0/0", "127.0.0.0/8", "169.254.1.0/24",
    "100.64.0.0/24", "192.168.0.0/15", "::1/128", "10.0.0.0/8", "not-a-network",
])
def test_unsafe_subnets_refused(subnet: str) -> None:
    """Reject public, non-RFC1918, overly broad, and malformed ranges."""
    with pytest.raises(DiscoveryError):
        validate_subnet(subnet)


@pytest.mark.parametrize("subnet", ["192.168.1.123/24", "10.1.0.0/16", "172.16.1.0/24"])
def test_private_subnets_accepted(subnet: str) -> None:
    """Allow only bounded private IPv4 targets, normalizing host bits."""
    assert validate_subnet(subnet).is_private


def test_mac_normalization() -> None:
    """MAC formatting cannot split identities or merge invalid addresses."""
    assert normalize_mac("AA-BB-CC-11-22-33") == "AA:BB:CC:11:22:33"
    assert normalize_mac("ff:ff:ff:ff:ff:ff") is None
    assert normalize_mac("00:00:00:00:00:00") is None
    assert normalize_mac("not-a-mac") is None


def test_nmap_missing_uses_scapy() -> None:
    """An absent Nmap executable should transparently fall back to ARP."""
    network = validate_subnet("192.168.1.0/24")
    with patch("app.scanner.nmap_scan", side_effect=FileNotFoundError), patch(
        "app.scanner.scapy_scan", return_value=[DiscoveredDevice("192.168.1.2")]
    ) as fallback, patch("app.scanner.reverse_hostname", return_value=None):
        assert discover(network)[0].ip == "192.168.1.2"
        fallback.assert_called_once_with(network)


def test_no_raw_socket_permissions_is_recoverable() -> None:
    """Failure of both methods returns a helpful recoverable discovery error."""
    with patch("app.scanner.nmap_scan", side_effect=FileNotFoundError), patch(
        "app.scanner.scapy_scan", side_effect=PermissionError
    ):
        with pytest.raises(DiscoveryError, match="Administrator/root"):
            discover(validate_subnet("192.168.1.0/24"))


def test_nmap_on_path() -> None:
    """Prefer the executable explicitly available on PATH."""
    with patch("app.scanner.shutil.which", return_value="installed-nmap"):
        assert find_nmap_executable() == "installed-nmap"


def test_nmap_in_windows_install_directory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A stale process PATH must not hide a newly installed Windows Nmap."""
    monkeypatch.setenv("ProgramFiles", str(tmp_path))
    expected = tmp_path / "Nmap" / "nmap.exe"
    with patch("app.scanner.shutil.which", return_value=None), patch(
        "app.scanner.Path.is_file", autospec=True, side_effect=lambda candidate: candidate == expected
    ):
        assert find_nmap_executable() == str(expected)


def test_nmap_missing() -> None:
    """Missing binaries raise a recoverable error for the Scapy fallback."""
    with patch("app.scanner.shutil.which", return_value=None), patch(
        "app.scanner.Path.is_file", return_value=False
    ):
        with pytest.raises(FileNotFoundError):
            find_nmap_executable()
