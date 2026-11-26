"""Isolated SQLite fixtures; automated tests never scan the real network."""

import os
from collections.abc import Generator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ.setdefault("DASHBOARD_USERNAME", "test-user")
os.environ.setdefault("DASHBOARD_PASSWORD", "test-only-password")

from app.database import Base  # noqa: E402
from app import models  # noqa: E402, F401


@pytest.fixture
def sessions() -> Generator[sessionmaker[Session], None, None]:
    """Provide a separate shared in-memory database for each test."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()
