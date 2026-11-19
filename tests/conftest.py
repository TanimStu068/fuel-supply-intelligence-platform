"""Cross-service test suite.

These tests need the docker-compose stack to be running. The fast unit suite
lives in ``backend/tests/`` and needs nothing running.

    pytest tests/ -m integration      # against a live stack
"""

from __future__ import annotations

import pytest


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "integration: requires a running docker-compose stack"
    )
