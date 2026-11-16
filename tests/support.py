"""Helpers shared by the endpoint tests."""

from __future__ import annotations

from typing import Any

import httpx
import jsonschema

from foood.contract import resolve_refs, response_schema
from foood.store import PASSWORD, QA_PASSWORD


def password_for(email: str) -> str:
    """The seeded accounts' passwords: the demo ones and the Delivery Plus E2E ones."""
    return QA_PASSWORD if email.endswith("@delivery-plus.test") else PASSWORD


async def login(client: httpx.AsyncClient, email: str) -> dict[str, str]:
    """Sign in as a seeded account and return the Authorization header."""
    response = await client.post(
        "/api/auth/login", json={"email": email, "password": password_for(email)}
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['accessToken']}"}


def assert_conforms(method: str, path: str, response: httpx.Response) -> Any:
    """Validate ``response`` against the contract's schema for its status code."""
    schema = response_schema(method, path, response.status_code)
    body = response.json() if response.content else None
    if schema is not None:
        jsonschema.validate(body, resolve_refs(schema))
    return body
