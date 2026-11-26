"""Wi-Fi identity parsing and safe fallback tests; no real commands execute."""

import subprocess
from unittest.mock import patch

from app.network_info import NetworkInfo, detect_network_info, parse_windows_wifi, run_network_command


def test_windows_wifi_name_and_signal() -> None:
    """SSID must be distinct from both the adapter name and access-point MAC."""
    output = """
    Name                   : Wi-Fi
    State                  : connected
    SSID                   : My Home: Wi-Fi
    AP BSSID               : 58:72:c9:77:99:05
    Signal                 : 87%
    """
    assert parse_windows_wifi(output, ["Wi-Fi"]) == NetworkInfo("Wi-Fi", "My Home: Wi-Fi", 87)


def test_windows_ignores_unrelated_adapter() -> None:
    """Do not label a wired or different-subnet network with another adapter's SSID."""
    output = "Name : Wi-Fi\nSSID : Unrelated network\nSignal : 95%"
    assert parse_windows_wifi(output, ["Ethernet"]) == NetworkInfo(interface="Ethernet")


def test_windows_multiple_adapters() -> None:
    """Choose the Wi-Fi adapter belonging to the configured subnet."""
    output = "Name : Wi-Fi\nSSID : Other\nSignal : 50%\nName : Wi-Fi 2\nSSID : Owned LAN\nSignal : 99%"
    assert parse_windows_wifi(output, ["Wi-Fi 2"]) == NetworkInfo("Wi-Fi 2", "Owned LAN", 99)


def test_disconnected_wifi_has_no_ssid() -> None:
    """A disconnected interface must not invent a network name."""
    assert parse_windows_wifi("Name : Wi-Fi\nState : disconnected", ["Wi-Fi"]) == NetworkInfo("Wi-Fi")


def test_windows_detection() -> None:
    """The platform detector reads only local connection metadata."""
    with patch("app.network_info.subnet_interfaces", return_value=["Wi-Fi"]), patch(
        "app.network_info.platform.system", return_value="Windows"
    ), patch("app.network_info.run_network_command", return_value="Name : Wi-Fi\nSSID : Home"):
        assert detect_network_info("192.168.1.0/24").ssid == "Home"


def test_linux_nmcli_fallback() -> None:
    """Missing iwgetid may fall back to NetworkManager without requesting a Wi-Fi scan."""
    with patch("app.network_info.subnet_interfaces", return_value=["wlan0"]), patch(
        "app.network_info.platform.system", return_value="Linux"
    ), patch("app.network_info.run_network_command", side_effect=["", ":Other\n*:Home:Network"]) as run:
        assert detect_network_info("192.168.1.0/24").ssid == "Home:Network"
        assert run.call_args.args[0][-2:] == ["--rescan", "no"]


def test_unavailable_tools_fall_back_to_interface() -> None:
    """An unsupported environment leaves monitoring functional without a Wi-Fi name."""
    with patch("app.network_info.subnet_interfaces", return_value=["eth0"]), patch(
        "app.network_info.platform.system", return_value="Linux"
    ), patch("app.network_info.run_network_command", return_value=""):
        assert detect_network_info("192.168.1.0/24") == NetworkInfo("eth0")


def test_metadata_timeout_is_recoverable() -> None:
    """The metadata subprocess cannot block a scheduler worker indefinitely."""
    with patch("app.network_info.subprocess.run", side_effect=subprocess.TimeoutExpired("netsh", 5)):
        assert run_network_command(["netsh", "wlan", "show", "interfaces"]) == ""


def test_redacted_macos_name_is_not_displayed_as_ssid() -> None:
    """Respect OS privacy restrictions rather than fabricating a readable SSID."""
    with patch("app.network_info.subnet_interfaces", return_value=["en0"]), patch(
        "app.network_info.platform.system", return_value="Darwin"
    ), patch("app.network_info.run_network_command", return_value="SSID : <redacted>"):
        assert detect_network_info("192.168.1.0/24") == NetworkInfo("en0")
