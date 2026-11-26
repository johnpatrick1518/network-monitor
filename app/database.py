"""SQLAlchemy engine, sessions, and UTC helpers."""

from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    """Base class for persisted models."""


def utcnow() -> datetime:
    """Return naive UTC for consistent SQLite comparisons."""
    return datetime.now(UTC).replace(tzinfo=None)


def iso_utc(value: datetime | None) -> str | None:
    """Serialize a stored UTC timestamp with an explicit timezone."""
    return value.replace(tzinfo=UTC).isoformat() if value else None


def build_engine(url: str) -> Engine:
    """Create a SQLite engine and its parent data directory."""
    parsed = make_url(url)
    if parsed.get_backend_name() != "sqlite":
        raise ValueError("This application currently supports SQLite only.")
    if parsed.database and parsed.database != ":memory:":
        Path(parsed.database).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        url, connect_args={"check_same_thread": False, "timeout": 30}
    )

    @event.listens_for(engine, "connect")
    def configure_sqlite(connection: object, record: object) -> None:
        """Enable referential integrity, WAL, and a busy timeout."""
        cursor = connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()

    return engine


engine = build_engine(get_settings().database_url)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    """Provide a request-scoped database session."""
    with SessionLocal() as session:
        yield session
