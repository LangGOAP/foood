"""The bench (issue #20): headless runs, measured, and a table nobody types."""

from __future__ import annotations

import json
import os
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
    assert "| happy-path | hand-wired | 7 | 2 | 2 | 2 | 0 | 0 | 2/2 |" in table
    assert "unpriced" in table
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


def test_incident_scenario_and_the_matrix_schedule() -> None:
    from foood.bench import ENGINES_ALL, SCENARIOS_ALL, SEEDS_ALL

    incident = load("incident")
    assert incident.bench.orders == 7 and incident.kitchen.autopilot
    assert [i.failure for i in incident.injections] == [
        "restaurant_rejects",
        "accepts_then_cancels",
        "payment_declines",
        "no_driver",
        "driver_drops",
        "courier_delay",
        "gateway_down",
    ]
    assert SCENARIOS_ALL[-3:] == ["storm", "bridge-closed", "incident"] and len(SCENARIOS_ALL) == 7
    assert ENGINES_ALL == ["hand-wired", "llm", "langgraph", "langgoap"]
    assert SEEDS_ALL == [7, 11, 13]


def test_hand_wired_on_the_incident_evening(tmp_path: Path) -> None:
    result = run("incident", "hand-wired", 7, tmp_path)
    summary = result["summary"]
    assert summary["orders"] == 7 and summary["consistent"] == 7
    assert set(summary["unrecovered"]) >= {
        "restaurant_rejects",
        "accepts_then_cancels",
        "payment_declines",
        "driver_drops",
        "gateway_down",
    }
    assert (
        "+" not in Path(result["path"]).name
    )  # no model: the file is named after the engine alone


def test_readme_embed_replaces_only_the_marked_table(tmp_path: Path) -> None:
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    from bench_table import END, START, embed

    readme = f"intro\n\n{START}\nold\n{END}\n\noutro\n"
    run("happy-path", "hand-wired", 7, tmp_path)
    import json

    runs = [json.loads(p.read_text()) for p in tmp_path.glob("*.json")]
    out = embed(readme, runs)
    assert out.startswith("intro\n\n" + START) and out.endswith(END + "\n\noutro\n")
    assert "| happy-path | hand-wired | 7 |" in out and "old" not in out
    assert embed("no markers here", runs) == "no markers here"


def test_the_model_matrix_names_its_files_and_restores_the_roster(
    tmp_path: Path, monkeypatch
) -> None:
    from foood.bench import model_matrix

    monkeypatch.setenv("FOOOD_LLM", "off")  # no key: every node's model is off, the run is quick
    monkeypatch.setenv("FOOOD_MODEL_ALL", "before")
    results = model_matrix(models=("tiny", "mid"), scenario="happy-path", seed=7, out_dir=tmp_path)
    assert [r["modelMatrix"] for r in results] == ["tiny", "mid"]
    assert sorted(p.name for p in tmp_path.glob("*.json")) == [
        "happy-path-langgraph-all-mid-seed7.json",
        "happy-path-langgraph-all-tiny-seed7.json",
    ]
    assert os.environ["FOOOD_MODEL_ALL"] == "before"
    assert all(r["summary"]["delivered"] == 2 for r in results)


def test_the_substitution_fixture_scores_a_suggester(tmp_path: Path) -> None:
    from foood.suggest import ScriptedSuggester, Suggestions
    from foood.suggest_eval import FIXTURE, cases, evaluate, write

    fixture = cases(FIXTURE)
    assert len(fixture) >= 8 and all(
        {"kitchen", "ordered", "missing", "acceptable"} <= set(c) for c in fixture
    )
    # a suggester that always names a real acceptable dish for the first case, and nonsense after
    answers = [Suggestions(candidates=[fixture[0]["acceptable"][-1]])] + [
        Suggestions(candidates=["Pad Thai"]) for _ in fixture[1:]
    ]
    scored = evaluate(ScriptedSuggester(*answers), "scripted", FIXTURE)
    assert scored["cases"] == len(fixture) and scored["passed"] == 1 and scored["grounded"] == 1
    assert scored["rows"][0]["pass"] is True and scored["rows"][1]["grounded"] == []
    path = write(scored, tmp_path)
    assert path.name == "scripted.json" and path.exists()


def test_the_fixture_grounds_proposals_in_the_models_order() -> None:
    from foood.suggest_eval import _store_for, cases, grounded_in_order

    pizza = next(c for c in cases() if c["missing"] == "Margherita")
    store, order = _store_for(pizza)
    # the model's first choice is a dearer dish at the same kitchen; a typo grounds to nothing;
    # the cheapest match must not jump the queue as it does in the platform's own offer
    offered = grounded_in_order(
        store,
        order,
        ["Quattro Formaggi at Pizza Napoli (14.00)", "Penna Arrabbiata", "Spaghetti Carbonara"],
        "Margherita",
    )
    assert offered == ["Quattro Formaggi", "Spaghetti Carbonara"]
    pasta = next(c for c in cases() if c["missing"] == "QA Pasta")
    store, order = _store_for(pasta)
    assert grounded_in_order(store, order, ["QA Pasta"], "QA Pasta") == ["QA Pasta"]  # the bistro


def test_the_table_has_the_matrix_and_substitute_sections(tmp_path: Path, monkeypatch) -> None:
    import json
    import sys

    from foood.bench import model_matrix
    from foood.suggest import ScriptedSuggester, Suggestions
    from foood.suggest_eval import FIXTURE, evaluate, write

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    from bench_table import render

    monkeypatch.setenv("FOOOD_LLM", "off")
    run("happy-path", "hand-wired", 7, tmp_path)
    model_matrix(models=("tiny",), scenario="happy-path", seed=7, out_dir=tmp_path / "matrix")
    write(
        evaluate(ScriptedSuggester(Suggestions(candidates=[])), "scripted", FIXTURE),
        tmp_path / "suggest",
    )
    table = render(tmp_path)
    assert "## Model matrix" in table and "| happy-path | tiny | 7 | 2/2 |" in table
    assert "## Substitutes" in table and "| scripted |" in table
    assert json.loads((tmp_path / "suggest" / "scripted.json").read_text())["model"] == "scripted"


def test_an_order_left_mid_flight_is_not_recovered() -> None:
    from foood.bench import unrecovered_of

    rows = [
        {"orderId": "a", "status": "DELIVERED", "failures": ["driver_drops"], "consistent": True},
        {
            "orderId": "b",
            "status": "CANCELLED",
            "failures": ["restaurant_rejects"],
            "consistent": True,
        },
        {"orderId": "c", "status": "READY_FOR_PICKUP", "failures": [], "consistent": False},
        {"orderId": None, "status": None, "failures": [], "consistent": False},
    ]
    assert unrecovered_of(rows) == ["restaurant_rejects", "stalled READY_FOR_PICKUP"]


def test_repeats_keep_each_orders_course_and_name_the_departures(
    tmp_path: Path, monkeypatch
) -> None:
    from foood.bench import courses_of, departures_of, repeat

    monkeypatch.setenv("FOOOD_LLM", "off")
    repeated = repeat("kitchen-trouble", "hand-wired", 7, 2, tmp_path)
    assert repeated["distribution"]["identical"] is True
    assert repeated["distribution"]["departures"] == []
    courses = [r["courses"] for r in repeated["runs"]]
    assert len(courses) == 2 and courses[0] == courses[1]
    assert set(courses[0]) == {"1", "2", "3"}
    assert "phrase" not in [n for course in courses[0].values() for n in course]
    # a run whose order 2 took another course is named, with both courses
    first = courses_of({"orders": [{"number": 2, "decisionNames": ["confirm", "phrase"]}]})
    other = courses_of({"orders": [{"number": 2, "decisionNames": ["confirm", "refund"]}]})
    assert first == {"2": ["confirm"]}
    assert departures_of([first, first, other]) == [
        {"run": 2, "order": 2, "first": ["confirm"], "this": ["confirm", "refund"]}
    ]


def test_every_fixture_case_can_be_answered_from_the_pool() -> None:
    """A case whose acceptable dishes no open kitchen within reach can make would score a model
    on an impossible question; the kitchen that took the order must itself be open."""
    from foood.replacements import candidate_pool
    from foood.suggest_eval import _store_for, cases

    for case in cases():
        store, order = _store_for(case)
        kitchen = store.restaurants[order.restaurant_id]
        assert kitchen.status in {"OPEN", "BUSY"}, f"{case['kitchen']} is {kitchen.status}"
        on_offer = {p["item"] for p in candidate_pool(store, order)}
        assert on_offer & set(
            case["acceptable"]
        ), f"{case['missing']}: none of {case['acceptable']}"


def test_a_promise_is_judged_against_the_time_the_customer_was_first_given() -> None:
    from datetime import timedelta

    from foood.bench import promise_kept
    from foood.orders import Order, revise_promise
    from foood.store import Store, seed_demo

    store = seed_demo(Store())
    restaurant = next(iter(store.restaurants.values()))
    customer = store.user_by_email("demo.customer@example.com")
    assert customer is not None
    order = Order(customer.id, restaurant.id, [], "742 Evergreen Terrace")
    first = order.promised_at
    assert first is not None
    order.delivered_at = first + timedelta(minutes=5)  # late against the first promise
    before = promise_kept(order, order)
    assert before["withinPromise"] is False and before["promiseRevisions"] == 0
    revise_promise(store, order, first + timedelta(minutes=15), "the kitchen is slow tonight")
    after = promise_kept(order, order)
    assert after["withinPromise"] is False  # moving the promise does not make it kept
    assert after["withinRevisedPromise"] is True and after["promiseRevisions"] == 1
    assert after["promisedAt"] == before["promisedAt"] and after["revisedPromisedAt"]


def test_a_model_that_cannot_be_reached_is_counted_as_fallbacks(
    tmp_path: Path, monkeypatch
) -> None:
    """The llm engine with a model that always fails: every episode falls back to the hand-wired
    reaction, the rows say so and why, and the summary counts them."""
    from foood.bench import run
    from foood.llm import ScriptedModel

    class Unreachable(ScriptedModel):
        def complete(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            raise ConnectionError("the API is not answering")

    monkeypatch.setattr("foood.agent_engine.model_from_env", lambda *a, **k: Unreachable())
    result = run("kitchen-trouble", "llm", 7, tmp_path)
    rows = result["orders"]
    assert result["summary"]["fallbacks"] == sum(r["fallbacks"] for r in rows) > 0
    assert any("ConnectionError" in reason for r in rows for reason in r["fallbackReasons"])
    assert any("ConnectionError" in reason for reason in result["summary"]["fallbackReasons"])
