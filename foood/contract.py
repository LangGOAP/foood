"""The Delivery Plus public API contract and which operations foood claims.

``CLAIMED`` is the single list of operations this service implements. The
contract tests fail when a claimed operation has no route or returns a body
that does not validate against the contract's response schema, and
``scripts/contract_table.py`` renders the implemented / pending table from it.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contracts" / "delivery-plus-public.json"

#: (method, path) pairs, exactly as they appear in the contract.
CLAIMED: frozenset[tuple[str, str]] = frozenset(
    {
        ("POST", "/api/auth/register"),
        ("POST", "/api/auth/login"),
        ("POST", "/api/auth/refresh"),
        ("POST", "/api/auth/logout"),
        ("POST", "/api/auth/logout-all"),
        ("GET", "/api/auth/me"),
        ("POST", "/api/auth/password/change"),
        ("POST", "/api/auth/password/forgot"),
        ("POST", "/api/auth/password/reset"),
        ("POST", "/api/auth/verify-email"),
        ("POST", "/api/auth/resend-verification"),
        ("GET", "/api/users/me"),
        ("PATCH", "/api/users/me"),
        ("POST", "/api/users/me/phone/verification"),
        ("POST", "/api/users/me/phone/verify"),
    }
)


@dataclass(frozen=True)
class Operation:
    method: str
    path: str
    tag: str
    summary: str


@cache
def load_contract() -> dict[str, Any]:
    with CONTRACT_PATH.open(encoding="utf-8") as handle:
        return json.load(handle)  # type: ignore[no-any-return]


def operations() -> Iterator[Operation]:
    for path, methods in load_contract()["paths"].items():
        for method, spec in methods.items():
            if method in {"get", "post", "put", "patch", "delete"}:
                tags = spec.get("tags") or ["-"]
                yield Operation(method.upper(), path, tags[0], spec.get("summary", ""))


def response_schema(method: str, path: str, status: int) -> dict[str, Any] | None:
    """The JSON schema of a response, or ``None`` when the contract declares no body."""
    spec = load_contract()["paths"][path][method.lower()]
    response = spec.get("responses", {}).get(str(status))
    if response is None:
        return None
    content = response.get("content", {}).get("application/json")
    return content.get("schema") if content else None


def resolve_refs(schema: Any) -> Any:
    """Inline every ``$ref`` so ``jsonschema`` can validate without a resolver."""
    root = load_contract()

    def _resolve(node: Any, seen: tuple[str, ...]) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                ref = node["$ref"]
                if ref in seen:  # recursive schema: stop inlining
                    return {}
                target: Any = root
                for part in ref.removeprefix("#/").split("/"):
                    target = target[part]
                return _resolve(target, (*seen, ref))
            return {key: _resolve(value, seen) for key, value in node.items()}
        if isinstance(node, list):
            return [_resolve(item, seen) for item in node]
        return node

    return _resolve(schema, ())
