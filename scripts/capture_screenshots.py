"""Capture real dashboard screenshots using temporary synthetic data and an isolated browser."""

import logging
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
from playwright.sync_api import Browser, Playwright, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
LOGGER = logging.getLogger(__name__)


def available_port() -> int:
    """Reserve an available loopback port for the temporary screenshot server."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
        connection.bind(("127.0.0.1", 0))
        return int(connection.getsockname()[1])


def launch_browser(playwright: Playwright) -> Browser:
    """Use installed Edge on Windows, or Playwright-managed Chromium elsewhere."""
    if os.name == "nt":
        for variable in ("ProgramFiles(x86)", "ProgramFiles"):
            directory = os.environ.get(variable)
            if directory and (Path(directory) / "Microsoft/Edge/Application/msedge.exe").is_file():
                return playwright.chromium.launch(channel="msedge", headless=True)
    return playwright.chromium.launch(headless=True)


def wait_until_ready(
    client: httpx.Client, process: subprocess.Popen[bytes], timeout: float = 45
) -> None:
    """Wait for a healthy synthetic inventory, never accepting real monitoring data."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("Temporary screenshot server exited before startup completed.")
        try:
            response = client.get("/api/summary")
            response.raise_for_status()
            summary = response.json()
            if not summary.get("demo_mode"):
                raise RuntimeError("Refusing to capture a real network.")
            if summary.get("total") == 8 and summary.get("wifi_ssid") == "Signal Demo Wi-Fi":
                return
        except (httpx.TransportError, httpx.HTTPStatusError):
            pass
        time.sleep(0.1)
    raise TimeoutError("Temporary demo server did not become ready.")


def capture() -> None:
    """Start a private demo, capture rendered UI, and stop all temporary processes."""
    output = ROOT / "docs" / "images"
    output.mkdir(parents=True, exist_ok=True)
    port = available_port()
    url = f"http://127.0.0.1:{port}"
    username, password = "screenshot", secrets.token_urlsafe(24)
    with tempfile.TemporaryDirectory(prefix="signal-screenshots-") as temporary:
        directory = Path(temporary)
        environment = {
            **os.environ,
            "DEMO_MODE": "true",
            "DEMO_DATABASE_URL": f"sqlite:///{(directory / 'demo.db').as_posix()}",
            "DASHBOARD_USERNAME": username,
            "DASHBOARD_PASSWORD": password,
            "TELEGRAM_BOT_TOKEN": "",
            "TELEGRAM_CHAT_ID": "",
            "SMTP_HOST": "",
            "SCAN_INTERVAL_SECONDS": "300",
            "PING_INTERVAL_SECONDS": "60",
            "PYTHONUTF8": "1",
        }
        with (directory / "server.log").open("wb") as server_log:
            process = subprocess.Popen(
                [
                    sys.executable, "-m", "uvicorn", "app.main:app",
                    "--host", "127.0.0.1", "--port", str(port), "--workers", "1",
                ],
                cwd=ROOT, env=environment, stdout=server_log, stderr=subprocess.STDOUT,
            )
            try:
                with httpx.Client(base_url=url, auth=(username, password), timeout=3) as client:
                    wait_until_ready(client, process)
                    inventory = client.get("/api/devices").json()
                    if not all(
                        device["ip"].startswith("192.168.50.")
                        and device["vendor"] == "Synthetic demo vendor"
                        for device in inventory
                    ):
                        raise RuntimeError("Refusing to capture non-synthetic inventory.")
                    device = next(item for item in inventory if item["friendly_name"] == "Work Laptop")
                with sync_playwright() as playwright:
                    browser = launch_browser(playwright)
                    try:
                        context = browser.new_context(
                            viewport={"width": 1720, "height": 1200},
                            device_scale_factor=1,
                            color_scheme="dark",
                            http_credentials={"username": username, "password": password},
                        )
                        page = context.new_page()
                        errors: list[str] = []
                        page.on("pageerror", lambda error: errors.append(str(error)))
                        page.goto(url, wait_until="networkidle")
                        page.locator("#demo-banner").wait_for(state="visible")
                        page.wait_for_function(
                            "() => document.querySelectorAll('#device-rows tr').length === 8"
                        )
                        page.wait_for_function(
                            "() => document.querySelector('#network-name').textContent.includes('Signal Demo Wi-Fi')"
                        )
                        if errors:
                            raise RuntimeError(f"Dashboard JavaScript errors: {errors}")
                        page.screenshot(path=str(output / "dashboard.png"), full_page=True)
                        page.locator(f'tr[data-device-id="{device["id"]}"]').click()
                        page.locator("#device-dialog").wait_for(state="visible")
                        page.wait_for_function(
                            "() => typeof Chart !== 'undefined' && "
                            "Chart.getChart(document.getElementById('latency-chart'))"
                            "?.data.datasets[0].data.length > 0"
                        )
                        page.locator("#chart-message").wait_for(state="hidden")
                        page.locator('[data-range="7d"]').click()
                        page.wait_for_function(
                            "() => {"
                            " const chart = Chart.getChart(document.getElementById('latency-chart'));"
                            " return chart && chart.options.scales.x.max - chart.options.scales.x.min"
                            " > 600000000 && chart.data.datasets[0].data.length > 1000;"
                            "}"
                        )
                        if errors:
                            raise RuntimeError(f"Detail-view JavaScript errors: {errors}")
                        page.screenshot(path=str(output / "device-detail.png"), full_page=False)
                        context.close()
                    finally:
                        browser.close()
                LOGGER.info("Captured docs/images/dashboard.png and docs/images/device-detail.png")
            finally:
                if os.name == "nt":
                    # The Windows venv redirector can own a child Python process.
                    # Terminate only this temporary server's process tree.
                    subprocess.run(
                        ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                        check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    )
                else:
                    process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    capture()
