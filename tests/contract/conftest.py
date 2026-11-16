"""Response validation against the contract, for the endpoint tests to reuse."""

from __future__ import annotations

from typing import Any

import httpx
import jsonschema
import pytest

from foood.app import create_app
from foood.contract import resolve_refs, response_schema


@pytest.fixture
async def client():
    app = create_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client


def assert_conforms(method: str, path: str, response: httpx.Response) -> Any:
    """Validate ``response`` against the contract's schema for its status code."""
    schema = response_schema(method, path, response.status_code)
    body = response.json() if response.content else None
    if schema is not None:
        jsonschema.validate(body, resolve_refs(schema))
    return body
