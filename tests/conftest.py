"""Shared fixtures: an ASGI client against a freshly seeded app."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest

from foood.app import create_app


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    app = create_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        client.store = app.state.store  # type: ignore[attr-defined]  # tests reach the domain directly
        yield client
