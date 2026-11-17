"""How long each engine takes to decide, re-plans and plan steps (issue #30)."""

from __future__ import annotations

import sys
from pathlib import Path

from langgoap import ActionSpec, GoalSpec
from langgoap import plan as make_plan

from foood.app import create_app
from foood.bench import run, timing
from foood.goap_engine import Trace
from foood.recovery import Usage


def test_usage_carries_the_round_trip_time() -> None:
    assert Usage("m", 1, 2).latency_ms == 0.0
    assert Usage("m", 1, 2, latency_ms=12.5).latency_ms == 12.5


def test_timing_sums_each_kind_apart() -> None:
    decisions = [
        {"decision": "plan", "planningMs": 4.0, "steps": ["a", "b", "c"]},
        {"decision": "act: a"},
        {"decision": "replan", "planningMs": 2.5, "steps": ["b", "c"]},
        {"decision": "plan skips ahead", "planningMs": 1.0, "steps": ["c"]},
        {"decision": "llm: retry_payment", "latencyMs": 800.0, "llmCalls": 1},
        {"decision": "fallback", "latencyMs": 300.0, "llmCalls": 1},
        {"decision": "phrase", "latencyMs": 500.0, "llmCalls": 1},
        {"decision": "intake", "latencyMs": 700.0, "llmCalls": 1},
    ]
    assert timing(decisions) == {
        "planningMs": 7.5,
        "modelMs": 1100.0,
        "perceptionMs": 0.0,
        "languageMs": 1200.0,
        "replans": 1,
        "planSteps": 3,
    }


def test_timing_of_an_order_that_was_never_planned() -> None:
    assert timing([{"decision": "confirm"}]) == {
        "planningMs": 0.0,
        "modelMs": 0.0,
        "perceptionMs": 0.0,
        "languageMs": 0.0,
        "replans": 0,
        "planSteps": None,
    }


def test_the_tracer_times_every_planning_round() -> None:
    store = create_app(simulate=False).state.store
    trace = Trace(store)
    goal = GoalSpec(conditions={"a": True})
    found = make_plan({"a": False}, goal, [ActionSpec("flip", {"a": False}, {"a": True})])
    assert found is not None
    trace.on_plan_complete(found, 0.3, thread_id="order-1")
    trace.on_replan("state_deviation", found, 0.2, thread_id="order-1")
    rounds = [(d["decision"], d["planningMs"]) for d in store.decisions["order-1"]]
    assert [kind for kind, _ in rounds] == ["plan", "replan"]
    assert all(isinstance(ms, float) and ms >= 0 for _, ms in rounds)


def test_bench_rows_and_summary_carry_the_timing(tmp_path: Path) -> None:
    result = run("kitchen-trouble", "langgoap", 7, tmp_path)
    timing_block, summary = result["timing"], result["summary"]
    assert timing_block["planningMs"] > 0
    assert timing_block["modelMs"] == 0  # the planner decides; no model call decides for it
    assert timing_block["wallSeconds"] > 0
    assert summary["replans"] >= 1
    assert summary["meanPlanSteps"] > 0
    assert result["machine"]["python"] and result["machine"]["platform"]
    assert {"planningMs", "modelMs", "languageMs", "replans", "planSteps"} <= set(
        result["orders"][0]
    )


def test_table_shows_the_timing_columns(tmp_path: Path) -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    from bench_table import render

    run("happy-path", "langgoap", 7, tmp_path)
    header = next(line for line in render(tmp_path).splitlines() if line.startswith("| Scenario"))
    for name in ("Planner ms", "Model ms, decisions", "Re-plans", "Plan steps", "Wall s"):
        assert name in header


async def test_the_engine_records_the_model_round_trip() -> None:
    from foood.recovery import Recovery, ScriptedAdvisor
    from tests.support import login
    from tests.test_recovery import QA_OWNER, _app, _paid

    advisor = ScriptedAdvisor(
        Recovery(tool="replace_order", reasoning="QA Bistro is open and cheaper"), latency_ms=12.5
    )
    async with _app(advisor) as client:
        sara = await login(client, "demo.customer@example.com")
        owner = await login(client, QA_OWNER)
        order = await _paid(client, sara)
        await client.patch(
            f"/api/orders/{order['id']}/status",
            json={"status": "CANCELLED", "reason": "No pasta"},
            headers=owner,
        )
        body = (await client.get(f"/api/_engine?orderId={order['id']}")).json()
        chosen = next(d for d in body["decisions"][order["id"]] if d["decision"].startswith("llm:"))
        assert chosen["latencyMs"] == 12.5
