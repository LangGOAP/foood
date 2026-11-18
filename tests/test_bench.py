"""The bench (issue #20): headless runs, measured, and a table nobody types."""

from __future__ import annotations

import json
from pathlib import Path

from foood.bench import run
from foood.scenario import load


def test_happy_path_delivers_everything(tmp_path: Path) -> None:
    result = run("happy-path", "hand-wired", 7, tmp_path)
    summary = result["summary"]
    assert summary["orders"] == 2 and summary["delivered"] == 2 and summary["cancelled"] == 0
    assert summary["deliveredWithinPromise"] == 2 and summary["consistent"] == 2
    assert summary["failuresFired"] == [] and summary["unrecovered"] == []
    assert summary["recoveryDecisions"] == 0 and summary["humanPrompts"] == 0
    assert summary["llmCalls"] == 0 and summary["cost"] == 0
    assert 0 < summary["meanMinutes"] <= summary["p95Minutes"] < 30
    written = json.loads(Path(result["path"]).read_text())
    assert (
        written["scenario"] == "happy-path"
        and written["engine"] == "hand-wired"
        and written["seed"] == 7
    )
    assert [o["decisions"] for o in written["orders"]] == [3, 3]  # confirm, create delivery, assign


def test_kitchen_trouble_names_what_hand_wired_cannot_recover(tmp_path: Path) -> None:
    result = run("kitchen-trouble", "hand-wired", 7, tmp_path)
    summary = result["summary"]
    assert summary["orders"] == 3 and summary["consistent"] == 3
    assert summary["failuresFired"] == [
        "accepts_then_cancels",
        "prep_overrun",
        "restaurant_rejects",
    ]
    assert summary["unrecovered"] == ["accepts_then_cancels", "restaurant_rejects"]
    by_number = {o["number"]: o for o in result["orders"]}
    assert (
        by_number[1]["status"] == "CANCELLED" and by_number[1]["recoveryDecisions"] == 1
    )  # refund
    assert by_number[2]["status"] == "CANCELLED"
    assert by_number[3]["status"] == "DELIVERED" and by_number[3]["withinPromise"] is False
    assert any("unavailable" in note for note in by_number[3]["notes"])  # pasta ran out on order 2


def test_delivery_trouble_recovers_a_drop_before_pickup_only(tmp_path: Path) -> None:
    result = run("delivery-trouble", "hand-wired", 7, tmp_path)
    by_number = {o["number"]: o for o in result["orders"]}
    assert by_number[1]["status"] == "DELIVERED" and by_number[1]["failures"] == ["no_driver"]
    assert by_number[2]["status"] == "DELIVERED" and by_number[2]["recovered"] is True
    assert by_number[3]["status"] == "CANCELLED" and by_number[3]["failures"] == ["driver_drops"]
    assert by_number[4]["failures"] == ["courier_delay"]
    assert result["summary"]["unrecovered"] == ["driver_drops"]
    assert result["summary"]["consistent"] == 4


def test_a_storm_breaks_promises_but_hand_wired_delivers(tmp_path: Path) -> None:
    result = run("storm", "hand-wired", 7, tmp_path)
    summary = result["summary"]
    assert summary["orders"] == 5 and summary["delivered"] == 5 and summary["cancelled"] == 0
    assert summary["deliveredWithinPromise"] < 5  # the storm and the traffic, not a failure
    assert summary["failuresFired"] == ["driver_offline", "storm", "traffic"]
    assert summary["unrecovered"] == [] and summary["consistent"] == 5
    assert summary["recoveryDecisions"] == 0  # the baseline never notices the weather


def test_a_closed_road_makes_the_detour_late_and_hand_wired_delivers(tmp_path: Path) -> None:
    result = run("bridge-closed", "hand-wired", 7, tmp_path)
    summary = result["summary"]
    assert summary["orders"] == 3 and summary["delivered"] == 3
    assert summary["deliveredWithinPromise"] <= 1  # orders 1 and 2 take the detour
    assert summary["failuresFired"] == ["road_closed", "traffic"] and summary["unrecovered"] == []
    assert summary["consistent"] == 3


def test_same_inputs_same_numbers(tmp_path: Path) -> None:
    first = run("payment-trouble", "hand-wired", 7, tmp_path / "a")
    second = run("payment-trouble", "hand-wired", 7, tmp_path / "b")
    strip = lambda r: {k: v for k, v in r["summary"].items()}  # noqa: E731
    assert strip(first) == strip(second)
    assert [o["status"] for o in first["orders"]] == [o["status"] for o in second["orders"]]
    assert [o["minutes"] for o in first["orders"]] == [o["minutes"] for o in second["orders"]]


def test_bench_section_defaults_and_overrides() -> None:
    bench = load("happy-path").bench
    assert bench.orders is None and bench.restaurant == "QA Kitchen" and bench.drivers == 2
    from foood.scenario import parse

    custom = parse(
        'name = "x"\n[bench]\norders = 5\n'
        'items = ["QA Burger", "QA Fries"]\ndrop_off = [30.0, 31.0]\n'
    )
    assert custom.bench.orders == 5 and custom.bench.items == ("QA Burger", "QA Fries")
    assert custom.bench.drop_off == (30.0, 31.0)


def test_table_is_rendered_from_the_files(tmp_path: Path, monkeypatch) -> None:
    import sys

    monkeypatch.delenv(
        "FOOOD_LLM_PRICE_IN", raising=False
    )  # make exports bench.env; render unpriced
    monkeypatch.delenv("FOOOD_LLM_PRICE_OUT", raising=False)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    from bench_table import render

    assert "(no runs yet)" in render(tmp_path)
    run("happy-path", "hand-wired", 7, tmp_path)
    table = render(tmp_path)
    assert "| happy-path | hand-wired | 7 | 2 | 2 | 2 | 0 | 2/2 |" in table
    assert table.rstrip().endswith("1 run(s).")


def test_a_run_ends_with_the_replacement_not_the_original() -> None:
    """A placed order that was replaced is terminal; its replacement is the order that still has
    to reach the door, so the run follows the chain before calling the evening over."""
    from foood.bench import Placed, Run
    from foood.orders import Order, OrderItem
    from foood.scenario import load
    from foood.store import now

    bench = Run(load("happy-path"), "hand-wired", 7)
    store = bench.store
    kitchen = next(iter(store.restaurants.values()))
    customer = store.user_by_email("demo.customer@example.com")
    original = Order(customer.id, kitchen.id, [OrderItem("m", "QA Pasta", 10, 1)], "door")
    replacement = Order(customer.id, kitchen.id, [OrderItem("m", "QA Pasta", 10, 1)], "door")
    original.status, original.replaced_by_order_id = "CANCELLED", replacement.id
    replacement.status, replacement.replaces_order_id = "CONFIRMED", original.id
    store.orders[original.id] = original
    store.orders[replacement.id] = replacement
    bench.placed = [Placed(1, original.id, now())]
    bench.target = 1
    assert bench.final(original.id) is replacement
    assert bench.finished() is False
    replacement.status = "DELIVERED"
    assert bench.finished() is True
