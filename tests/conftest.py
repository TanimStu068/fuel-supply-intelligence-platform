"""Cross-service test suite.

These tests need the docker-compose stack to be running. The fast unit suite
lives in ``backend/tests/`` and needs nothing running.

    pytest tests/ -m integration      # against a live stack
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "integration: requires a running docker-compose stack"
    )


def load_env_file(path: Path) -> dict[str, str]:
    """Parse a ``KEY=value`` env file into a dict.

    A tiny parser instead of a dependency: the file is ours, and it keeps the
    integration suite runnable with no extra install step.
    """
    values: dict[str, str] = {}
    if not path.exists():
        return values

    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip("'\"")
    return values


def resolve_base_urls() -> tuple[str, str]:
    """Resolve the simulator and backend URLs the way the compose stack does.

    Environment variables win over the ``.env`` file, and the ``.env`` file
    wins over the committed defaults — so the suite follows a local port
    remapping instead of talking to whatever else happens to own port 8000.
    """
    env_file = load_env_file(REPO_ROOT / ".env")

    def pick(variable: str, default: str) -> str:
        return os.getenv(variable) or env_file.get(variable) or default

    simulator_port = pick("SIMULATOR_PORT", "8000")
    backend_port = pick("BACKEND_PORT", "8001")

    simulator_url = os.getenv("SIMULATOR_URL") or f"http://localhost:{simulator_port}"
    backend_url = os.getenv("BACKEND_URL") or f"http://localhost:{backend_port}"
    return simulator_url.rstrip("/"), backend_url.rstrip("/")


SIMULATOR_URL, BACKEND_URL = resolve_base_urls()
