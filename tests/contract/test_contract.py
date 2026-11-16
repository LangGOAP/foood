"""Every claimed operation exists; nothing is claimed that the contract lacks."""

from __future__ import annotations

import jsonschema
import pytest

from foood.app import create_app
from foood.contract import CLAIMED, load_contract, operations, resolve_refs


def _routes() -> set[tuple[str, str]]:
    """Every (METHOD, path) the app serves, read from its own OpenAPI document.

    FastAPI registers included routers lazily, so walking ``app.routes`` right
    after ``create_app()`` misses them; the generated document does not.
    """
    app = create_app()
    return {
        (method.upper(), path)
        for path, methods in app.openapi()["paths"].items()
        for method in methods
        if method in {"get", "post", "put", "patch", "delete"}
    }


def test_contract_file_is_the_delivery_plus_public_api() -> None:
    contract = load_contract()
    assert contract["info"]["title"] == "Delivery Plus Public API"
    assert len(contract["paths"]) >= 90


def test_every_claimed_operation_is_in_the_contract() -> None:
    known = {(op.method, op.path) for op in operations()}
    assert CLAIMED <= known, sorted(CLAIMED - known)


def test_every_claimed_operation_has_a_route() -> None:
    routes = _routes()
    missing = sorted(op for op in CLAIMED if op not in routes)
    assert missing == []


def test_no_contract_route_is_served_without_being_claimed() -> None:
    """A route under /api that is not claimed is a silent, untested promise."""
    contract_paths = {op.path for op in operations()}
    unclaimed = sorted(
        (method, path)
        for method, path in _routes()
        if path in contract_paths and (method, path) not in CLAIMED
    )
    assert unclaimed == []


def test_nullable_fields_accept_null() -> None:
    """OpenAPI 3.0 ``nullable`` must be honoured, or every optional field would fail."""
    schema = resolve_refs(
        {"type": "object", "properties": {"x": {"type": "string", "nullable": True}}}
    )
    jsonschema.validate({"x": None}, schema)
    jsonschema.validate({"x": "ok"}, schema)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"x": 1}, schema)
