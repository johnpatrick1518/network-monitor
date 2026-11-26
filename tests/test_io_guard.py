"""Regression tests for the suite's no-real-network safety net."""

import socket
import subprocess

import pytest


def test_external_connections_are_blocked() -> None:
    """Even a mistakenly unmocked adapter cannot connect to an external host."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
        with pytest.raises(AssertionError, match="external networks"):
            connection.connect(("192.0.2.1", 443))
        with pytest.raises(AssertionError, match="external networks"):
            connection.connect_ex(("192.0.2.1", 443))


def test_real_processes_are_blocked() -> None:
    """A test cannot inadvertently execute ping, Nmap, or another utility."""
    with pytest.raises(AssertionError, match="real subprocesses"):
        subprocess.run(["this-must-never-run"], check=False)
