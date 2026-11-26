"""Loopback-only demo launcher: python -m app.demo."""

import argparse
import logging
import os
import secrets


def main() -> None:
    """Launch an isolated demo with temporary credentials and no real monitoring."""
    parser = argparse.ArgumentParser(description="Run Signal with synthetic data and no LAN access.")
    parser.add_argument("--port", type=int, default=8000, help="Loopback HTTP port (default: 8000)")
    arguments = parser.parse_args()
    if not 1 <= arguments.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    password = secrets.token_urlsafe(16)
    # Set environment before importing application modules or constructing their engine.
    os.environ.update({
        "DEMO_MODE": "true",
        "DASHBOARD_USERNAME": "demo",
        "DASHBOARD_PASSWORD": password,
        "PING_INTERVAL_SECONDS": "10",
        "SCAN_INTERVAL_SECONDS": "60",
        "DOWN_THRESHOLD": "3",
        "ALERT_COOLDOWN_SECONDS": "60",
    })
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    logger = logging.getLogger(__name__)
    logger.info("DEMO ONLY: synthetic devices, no LAN access, no external alert delivery.")
    logger.info("Open http://localhost:%s · username: demo", arguments.port)
    logger.info("Temporary local demo password: %s", password)

    import uvicorn

    uvicorn.run("app.main:app", host="127.0.0.1", port=arguments.port, workers=1)


if __name__ == "__main__":
    main()
