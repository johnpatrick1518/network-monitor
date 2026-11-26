"""Isolated SQLite fixtures; automated tests never scan the real network."""

import os
import socket
import subprocess
from collections.abc import Generator
from typing import NoReturn

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ.setdefault("DASHBOARD_USERNAME", "test-user")
os.environ.setdefault("DASHBOARD_PASSWORD", "test-only-password")

from app import models  # noqa: E402, F401
from app.database import Base  # noqa: E402


@pytest.fixture(autouse=True)
def deny_external_io(monkeypatch: pytest.MonkeyPatch) -> None:
    """Forbid external socket connections and processes, preserving event-loop plumbing."""
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def is_local(connection: socket.socket, address: object) -> bool:
        """Allow only local IPC needed by some platform event loops."""
        return connection.family == getattr(socket, "AF_UNIX", None) or (
            isinstance(address, tuple) and address[0] in {"127.0.0.1", "::1"}
        )

    def guarded_connect(connection: socket.socket, address: object) -> None:
        """Reject any accidental LAN or Internet socket connection."""
        if not is_local(connection, address):
            raise AssertionError("Tests cannot connect to external networks.")
        return original_connect(connection, address)

    def guarded_connect_ex(connection: socket.socket, address: object) -> int:
        """Reject alternate socket connection calls to external networks."""
        if not is_local(connection, address):
            raise AssertionError("Tests cannot connect to external networks.")
        return original_connect_ex(connection, address)

    def forbidden_process(*args: object, **kwargs: object) -> NoReturn:
        """Require every platform subprocess adapter to be mocked explicitly."""
        raise AssertionError("Tests cannot launch real subprocesses.")

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
    monkeypatch.setattr(subprocess, "Popen", forbidden_process)


@pytest.fixture
def sessions() -> Generator[sessionmaker[Session], None, None]:
    """Provide a separate shared in-memory database for each test."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()
