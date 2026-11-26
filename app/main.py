"""Authenticated FastAPI application and its single-process lifespan."""

import base64
import binascii
import logging
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from app.api.routes import router
from app.config import get_settings
from app.database import Base, SessionLocal, engine
from app.scheduler import MonitorService

settings = get_settings()
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
# HTTPX request logs would expose Telegram's token-bearing URL.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    """Initialize storage and start exactly one in-process monitoring scheduler."""
    if not settings.dashboard_username or not settings.dashboard_password:
        raise RuntimeError("Set DASHBOARD_USERNAME and a strong DASHBOARD_PASSWORD in .env before starting.")
    Base.metadata.create_all(engine)
    service = MonitorService(settings, SessionLocal)
    application.state.service = service
    service.start()
    try:
        yield
    finally:
        # Job completion is blocking; keep it off the ASGI event loop.
        from starlette.concurrency import run_in_threadpool

        await run_in_threadpool(service.stop)
        engine.dispose()


app = FastAPI(
    title="Private Network Monitor",
    description="Authenticated inventory, ICMP health history, and alerts for your own RFC1918 subnet.",
    version="1.0.0",
    lifespan=lifespan,
)


def valid_credentials(header: str) -> bool:
    """Verify HTTP Basic credentials using constant-time comparisons."""
    try:
        scheme, encoded = header.split(" ", 1)
        if scheme.lower() != "basic":
            return False
        username, password = base64.b64decode(encoded, validate=True).decode("utf-8").split(":", 1)
        user_ok = secrets.compare_digest(username.encode("utf-8"), settings.dashboard_username.encode("utf-8"))
        password_ok = secrets.compare_digest(password.encode("utf-8"), settings.dashboard_password.encode("utf-8"))
        return bool(settings.dashboard_password) and user_ok and password_ok
    except (ValueError, UnicodeError, binascii.Error):
        return False


@app.middleware("http")
async def protect_dashboard(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Protect every page/API/doc and reject cross-origin browser mutations."""
    if not valid_credentials(request.headers.get("Authorization", "")):
        return JSONResponse(
            status_code=401, content={"detail": "Authentication required."},
            headers={"WWW-Authenticate": 'Basic realm="Network Monitor", charset="UTF-8"'},
        )
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        origin = request.headers.get("origin")
        parsed = urlsplit(origin) if origin else None
        if request.headers.get("sec-fetch-site") == "cross-site" or (
            parsed and (parsed.scheme != request.url.scheme or parsed.netloc != request.url.netloc)
        ):
            return JSONResponse(status_code=403, content={"detail": "Cross-origin requests are not allowed."})
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self' https://cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
        "img-src 'self' data: https://fastapi.tiangolo.com; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    ) if request.url.path not in {"/docs", "/redoc"} else (
        "default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
        "img-src 'self' data: https://fastapi.tiangolo.com; "
        "connect-src 'self'; frame-ancestors 'none'; worker-src blob:"
    )
    return response


app.include_router(router)
app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="dashboard")
