"""The service answers the two health probes the Delivery Plus gateway exposes."""

from __future__ import annotations

import httpx
import pytest

from foood.app import create_app


@pytest.fixture
async def client():
    app = create_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client


async def test_health(client: httpx.AsyncClient) -> None:
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


async def test_health_live(client: httpx.AsyncClient) -> None:
    response = await client.get("/health/live")
    assert response.status_code == 200
