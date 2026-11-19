"""Shared pytest fixtures for the backend test suite."""

from __future__ import annotations

import httpx
import pytest

from app.config import Settings
from app.simulator_client import SimulatorClient


@pytest.fixture
def settings() -> Settings:
    """Fast settings: tiny backoff so retry tests stay quick."""
    return Settings(
        SIMULATOR_BASE_URL="http://simulator.test",
        SIMULATOR_REQUEST_TIMEOUT=1.0,
        SIMULATOR_CONNECT_TIMEOUT=0.5,
        SIMULATOR_MAX_RETRIES=2,
        SIMULATOR_RETRY_BASE_DELAY=0.001,
        SIMULATOR_RETRY_MAX_DELAY=0.01,
    )


@pytest.fixture
def transport() -> httpx.MockTransport:
    """A mock transport; individual tests replace its handler."""
    raise NotImplementedError("use the `mock_transport` factory instead")


def make_client(settings: Settings, handler) -> SimulatorClient:
    """Build a client whose HTTP layer is fully mocked."""
    client = httpx.AsyncClient(
        base_url=settings.simulator_base_url,
        transport=httpx.MockTransport(handler),
    )
    return SimulatorClient(settings=settings, client=client)


@pytest.fixture
def client_factory(settings: Settings):
    """Factory returning a client bound to a given mock handler."""

    def _factory(handler) -> SimulatorClient:
        return make_client(settings, handler)

    return _factory
