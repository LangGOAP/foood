"""Every claimed operation exists; nothing is claimed that the contract lacks."""

from __future__ import annotations

from fastapi.routing import APIRoute

from foood.app import create_app
from foood.contract import CLAIMED, load_contract, operations


def _routes() -> set[tuple[str, str]]:
    app = create_app()
    found: set[tuple[str, str]] = set()
    for route in app.routes:
        if isinstance(route, APIRoute):
            for method in route.methods:
                found.add((method, route.path))
    return found


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
