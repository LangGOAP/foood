"""Helpers shared by the endpoint tests."""

from __future__ import annotations

from typing import Any

import httpx
import jsonschema

from foood.contract import resolve_refs, response_schema


def assert_conforms(method: str, path: str, response: httpx.Response) -> Any:
    """Validate ``response`` against the contract's schema for its status code."""
    schema = response_schema(method, path, response.status_code)
    body = response.json() if response.content else None
    if schema is not None:
        jsonschema.validate(body, resolve_refs(schema))
    return body
