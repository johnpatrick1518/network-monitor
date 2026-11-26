# Demo screenshots

These PNGs are actual headless Microsoft Edge captures of the running application, taken with Playwright:

- [`dashboard.png`](dashboard.png): full device inventory, health cards, activity feed, and visible **DEMO MODE** banner.
- [`device-detail.png`](device-detail.png): Work Laptop details, seven-day latency chart, availability timeline, uptime, and editing controls.

They are not generated UI illustrations or evidence of a hosted deployment. All displayed device identities, private addresses, MACs, Wi-Fi identity, and measurements are synthetic fixtures. No real `.env` credentials, network inventory, or Telegram chat is displayed.

## Reproduce

Run from the project root using the virtual environment's Python:

```bash
python -m pip install -r requirements-screenshots.txt
python scripts/capture_screenshots.py
```

On Windows, the script uses installed Microsoft Edge without opening your personal browser profile. If Edge is unavailable, or on Linux/macOS, install Playwright's Chromium first:

```bash
python -m playwright install chromium
```

Linux may require `python -m playwright install --with-deps chromium` to install browser system dependencies. This capture tooling is optional; running the dashboard itself still requires no frontend build or browser-automation dependencies.

The script starts its own loopback-only server on a free port, forces `DEMO_MODE=true`, uses a temporary database and random credentials, clears Telegram/SMTP environment settings, and checks the API inventory is synthetic before capturing. It waits for real device rows and a populated Chart.js chart; it does not inject fake DOM or chart content. The temporary server and isolated browser are stopped afterward, and the real `.env` and existing databases remain unchanged.

Chart.js must be reachable from its configured CDN during capture. Review images before committing future captures, especially if changing the script to use different data or a personal browser session.
