"""Shared fixtures: the mock enterprise services, run in-process (no server, no network)."""

from collections.abc import AsyncIterator

import httpx
import pytest
from release_copilot_mocks.app import create_app


@pytest.fixture
def mocks_app():
    """A fresh mock app per test (default scenario ``clean``; override per request with ``X-Mock-Scenario``)."""
    return create_app("clean")


@pytest.fixture
async def mocks_http(mocks_app) -> AsyncIterator[httpx.AsyncClient]:
    """An httpx client routed into the in-process mocks. Clients under test can reuse its transport."""
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=mocks_app), base_url="http://mocks") as client:
        yield client
