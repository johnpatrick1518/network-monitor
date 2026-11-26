# Portfolio notes

Suggested copy below describes implemented behavior, not production scale or unmeasured performance. Review and adapt it to your own experience before using it with employers.

## GitHub repository description

Private-network monitoring with FastAPI, health charts, Telegram alerts, and safe demo mode.

## Topics

`python` · `fastapi` · `network-monitoring` · `sqlite` · `sqlalchemy` · `docker` · `pytest` · `github-actions`

## Resume bullets

- Built a FastAPI/SQLAlchemy network dashboard with private-subnet discovery, cross-platform ICMP checks, searchable inventory, and 24h/7d/30d uptime reporting.
- Implemented debounced outage/recovery detection, persisted alert cooldowns, Telegram/SMTP notifications, and a no-build JavaScript interface with latency charts.
- Added isolated synthetic demo mode, 78 automated tests, Ruff checks, and a six-environment GitHub Actions matrix covering Python 3.11/3.13 on Linux, Windows, and macOS.

## Interview talking points

1. **Correctness versus noisy signals:** explain why scan failure is not device failure, raw ping loss differs from debounced status, and no observations should not imply full uptime.
2. **Networking and portability:** discuss ARP versus routed discovery, Nmap/Npcap permissions, Windows ping exit-code behavior, and MAC/DHCP identity limitations.
3. **Persistence and concurrency:** walk through per-job SQLAlchemy sessions, SQLite WAL/indexes, bounded probes, non-overlap locks, and the single-worker constraint.
4. **Alert reliability and security:** distinguish debounce, cooldown, and failure backoff; explain private-range enforcement, secret exclusion, Basic Auth/TLS, and the missing durable outbox.
5. **Reproducible evaluation:** show the synthetic adapter boundary, separate demo database, disabled provider delivery, egress-blocking tests, and why CI mocks cannot replace real platform integration tests.

## Validation and publishing checklist

- Local verification: Python 3.13 on Windows; Ruff passes; 78 pytest cases pass with `DEMO_MODE=true`.
- The GitHub Actions matrix is configured; check the repository's Actions page for actual hosted results.
- `LICENSE` is MIT; third-party dependencies retain their own licenses.
- Repository and CI links target `https://github.com/johnpatrick1518/network-monitor`.
- README screenshots are actual browser captures of synthetic demo data; regenerate them with `scripts/capture_screenshots.py` after visible UI changes.
- Real `.env`, local databases, logs, virtual environments, and caches must remain untracked.
- Conventional commits and the annotated local `v1.0.0` tag identify the release.
- No remote repository creation, push, hosted deployment, or GitHub Release is implied by the local tag.
