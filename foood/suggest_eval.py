"""The substitution fixture (#53): is a model's suggestion any good, measured once per model.

``tests/fixtures/substitutes.toml`` lists out-of-stock cases against the seeded menus: what
was ordered, what ran out, and which dishes a reasonable person would accept in its place.
For each case the suggest node proposes from the real candidate pool; the proposals are then
grounded one at a time, in the model's order, and the first that names a dish on an open menu
within reach is the suggestion scored: the case passes when that dish is one of the acceptable
ones. (The platform's own offer sorts options by price; here the model's ranking is what is
under test.) The same dish at another kitchen counts as itself. Nothing is scored by a model;
the acceptable sets are written by hand in the fixture. The result file names the model, so
the table can set models side by side.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Any

from foood.orders import Order, OrderItem
from foood.replacements import candidate_pool, matches, substitutes
from foood.store import Store, iso, now, seed_demo
from foood.suggest import Suggester

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "substitutes.toml"
OUT = ROOT / "results" / "suggest"


def cases(path: Path = FIXTURE) -> list[dict[str, Any]]:
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    return list(raw.get("case", []))


def _store_for(case: dict[str, Any]) -> tuple[Store, Order]:
    """A fresh seeded store where the case's dish ran out at its kitchen."""
    store = seed_demo(Store())
    kitchen = next(r for r in store.restaurants.values() if r.name == case["kitchen"])
    customer = store.user_by_email("demo.customer@example.com")
    assert customer is not None  # seeded
    items = []
    for name in case["ordered"]:
        item = next(
            i for i in store.menu_items.values() if i.restaurant_id == kitchen.id and i.name == name
        )
        items.append(OrderItem(item.id, item.name, item.price, 1))
    order = Order(
        customer.id,
        kitchen.id,
        items,
        "742 Evergreen Terrace",
        delivery_latitude=37.787,
        delivery_longitude=-122.3985,
    )
    store.orders[order.id] = order
    for item in store.menu_items.values():
        if item.restaurant_id == kitchen.id and item.name == case["missing"]:
            item.available = False
    return store, order


def grounded_in_order(store: Store, order: Order, names: list[str], missing: str) -> list[str]:
    """The dish each proposal grounds to for the missing line, in the model's order, proposals
    that ground to nothing left out: a swap the kitchen offers, or the missing dish itself when
    another kitchen within reach carries it."""
    offered: list[str] = []
    for name in names:
        for option in substitutes(store, order, [name]):
            swapped = dict(option.substitutions)
            if swapped:
                dish = swapped.get(missing)
            else:  # a kitchen that carries the dish itself counts only if the proposal names it
                dish = missing if matches(name, missing) else None
            if dish and dish not in offered:
                offered.append(dish)
                break
    return offered


def evaluate(suggester: Suggester, model: str, path: Path = FIXTURE) -> dict[str, Any]:
    """Every case through the suggester; the score is how many first suggestions are acceptable."""
    rows = []
    for case in cases(path):
        store, order = _store_for(case)
        pool = candidate_pool(store, order)
        suggestions, usage = suggester.suggest(case["ordered"], [case["missing"]], pool)
        names = [str(c) for c in suggestions.candidates][:5]
        offered = grounded_in_order(store, order, names, case["missing"])
        first = offered[0] if offered else None
        rows.append(
            {
                "kitchen": case["kitchen"],
                "ordered": case["ordered"],
                "missing": case["missing"],
                "acceptable": case["acceptable"],
                "proposed": names,
                "grounded": offered,
                "first": first,
                "pass": first in case["acceptable"],
                "tokens": usage.total_tokens,
                "cost": usage.cost() or 0,
                "latencyMs": round(usage.latency_ms, 1),
            }
        )
    return {
        "model": model,
        "fixture": str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path),
        "at": iso(now()),
        "cases": len(rows),
        "passed": sum(r["pass"] for r in rows),
        "grounded": sum(1 for r in rows if r["grounded"]),
        "cost": round(sum(r["cost"] for r in rows), 4),
        "rows": rows,
    }


def write(result: dict[str, Any], out_dir: Path = OUT) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{result['model']}.json"
    path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return path
