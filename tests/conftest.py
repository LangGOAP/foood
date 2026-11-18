"""Shared fixtures: an ASGI client against a freshly seeded app."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import httpx
import pytest

from foood.app import create_app
from foood.scenario import Scenario
from foood.store import CLOCK


@pytest.fixture(autouse=True)
def _real_time() -> Iterator[None]:
    CLOCK.reset()
    yield
    CLOCK.reset()


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    app = create_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        client.store = app.state.store  # type: ignore[attr-defined]  # tests reach the domain directly
        yield client


@pytest.fixture
async def live() -> AsyncIterator[httpx.AsyncClient]:
    """A real uvicorn server for what the ASGI transport cannot do: streamed responses."""
    import asyncio
    import socket

    import uvicorn

    app = create_app(simulate=False)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
        client.store = app.state.store  # type: ignore[attr-defined]
        yield client
    server.should_exit = True
    await task


async def _client(scenario: Scenario) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(simulate=False, scenario=scenario)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        c.store = app.state.store  # type: ignore[attr-defined]
        c.engine = app.state.store.engine  # type: ignore[attr-defined]
        yield c


@pytest.fixture
def scenario_client():
    """Builds a client around a scenario: ``async with scenario_client(Scenario(...)) as c``."""
    from contextlib import asynccontextmanager

    return asynccontextmanager(_client)
