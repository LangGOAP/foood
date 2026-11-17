"""Priced rows with provenance (issue #37): a run records the prices it was given."""

from __future__ import annotations

import sys
from pathlib import Path

from foood.bench import run

ROOT = Path(__file__).resolve().parents[1]


def _render(folder: Path) -> str:
    sys.path.insert(0, str(ROOT / "scripts"))
    from bench_table import render

    return render(folder)


def test_an_unpriced_run_says_so(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("FOOOD_LLM_PRICE_IN", raising=False)
    monkeypatch.delenv("FOOOD_LLM_PRICE_OUT", raising=False)
    monkeypatch.delenv("FOOOD_LLM_PRICES", raising=False)
    result = run("happy-path", "hand-wired", 7, tmp_path)
    assert result["llmPrices"] == {
        "inputPerMillionUsd": None,
        "outputPerMillionUsd": None,
        "perModel": {},
    }
    assert result["summary"]["llmPriced"] is False
    table = _render(tmp_path)
    assert "| unpriced |" in table
    assert "Costs use the model prices each run file records" not in table


def test_a_priced_run_carries_its_prices(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("FOOOD_LLM_PRICE_IN", "0.75")
    monkeypatch.setenv("FOOOD_LLM_PRICE_OUT", "4.50")
    monkeypatch.delenv("FOOOD_LLM_PRICES", raising=False)
    result = run("happy-path", "hand-wired", 7, tmp_path)
    assert result["llmPrices"] == {
        "inputPerMillionUsd": 0.75,
        "outputPerMillionUsd": 4.5,
        "perModel": {},
    }
    assert result["summary"]["llmPriced"] is True
    table = _render(tmp_path)
    assert "| 0.0 |" in table  # priced, and nothing was spent: no model ran
    assert "0.75 in and 4.5 out, USD per million tokens" in table


def test_the_bench_targets_price_every_run_the_same_way() -> None:
    env = dict(
        line.split("=", 1)
        for line in (ROOT / "bench.env").read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    )
    assert float(env["FOOOD_LLM_PRICE_IN"]) > 0 and float(env["FOOOD_LLM_PRICE_OUT"]) > 0
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert "include bench.env" in makefile
    assert "export FOOOD_LLM_PRICE_IN FOOOD_LLM_PRICE_OUT FOOOD_LLM_PRICES" in makefile
    import json

    listed = json.loads(env["FOOOD_LLM_PRICES"])
    assert {"gpt-5.4", "gpt-5.4-mini", "gpt-5.4-nano"} <= set(listed)
    assert all(entry["in"] > 0 and entry["out"] > 0 for entry in listed.values())


def test_only_model_calls_cost_money(tmp_path: Path, monkeypatch) -> None:
    """A decision that carries a cost but no model call (a plan's A* cost) is not dollars."""
    from foood.bench import Run
    from foood.engine import decide
    from foood.scenario import load

    monkeypatch.setenv("FOOOD_LLM_PRICE_IN", "0.75")
    monkeypatch.setenv("FOOOD_LLM_PRICE_OUT", "4.50")
    bench = Run(load("happy-path"), "hand-wired", 7)
    store = bench.store
    decide(store, "order-x", "plan", because="A*", cost=141.77)
    decide(store, "order-x", "phrase", because="hello", llmCalls=1, llmTokens=10, cost=0.0003)
    decisions = store.decisions["order-x"]
    spent = round(sum(float(d.get("cost", 0)) for d in decisions if d.get("llmCalls")), 4)
    assert spent == 0.0003


def test_prices_can_be_per_model(monkeypatch) -> None:
    """Different nodes run different models (#46): each call is costed at its own model's price."""
    from foood.llm_prices import cost_of, price_for, prices

    monkeypatch.delenv("FOOOD_LLM_PRICE_IN", raising=False)
    monkeypatch.delenv("FOOOD_LLM_PRICE_OUT", raising=False)
    monkeypatch.setenv(
        "FOOOD_LLM_PRICES", '{"small": {"in": 0.05, "out": 0.4}, "mid": {"in": 0.75, "out": 4.5}}'
    )
    assert prices()["models"]["small"] == {"in": 0.05, "out": 0.4}
    assert price_for("mid") == (0.75, 4.5)
    assert price_for("frontier") is None  # a model without a listed price is unpriced
    assert cost_of("small", 1_000_000, 1_000_000) == 0.45
    assert cost_of("frontier", 10, 10) is None
    monkeypatch.setenv("FOOOD_LLM_PRICE_IN", "1.0")
    monkeypatch.setenv("FOOOD_LLM_PRICE_OUT", "2.0")
    assert price_for("frontier") == (1.0, 2.0)  # the pair is the price for any unlisted model
    assert price_for(None) == (1.0, 2.0)


def test_calls_are_counted_by_role_and_by_model(monkeypatch) -> None:
    from foood.bench import by_model, by_role, role_of

    decisions = [
        {
            "decision": "llm: replace_order",
            "llmCalls": 1,
            "model": "mid",
            "cost": 0.01,
            "latencyMs": 1200,
        },
        {
            "decision": "classify reason",
            "llmRole": "perception",
            "llmCalls": 1,
            "model": "small",
            "cost": 0.001,
            "latencyMs": 300,
        },
        {"decision": "phrase", "llmCalls": 1, "model": "small", "cost": 0.002, "llmTokens": 40},
        {"decision": "intake", "llmCalls": 1, "model": "small", "cost": 0.002, "llmTokens": 60},
        {"decision": "plan", "cost": 141.0},  # a planner's cost is not dollars and not a call
        {"decision": "confirm"},
    ]
    assert [role_of(d) for d in decisions[:4]] == ["decision", "perception", "language", "language"]
    assert by_role(decisions) == {
        "decision": {"calls": 1, "latencyMs": 1200},
        "perception": {"calls": 1, "latencyMs": 300},
        "language": {"calls": 2, "latencyMs": 0},
    }
    assert by_model(decisions) == {
        "mid": {"calls": 1, "tokens": 0, "cost": 0.01},
        "small": {"calls": 3, "tokens": 100, "cost": 0.005},
    }


def test_a_models_rounds_are_calls_not_recovery_actions() -> None:
    from foood.bench import NOT_RECOVERY

    names = ["llm: confirm_payment", "confirm", "llm: alternatives", "replace order", "phrase"]
    actions = [n for n in names if n not in NOT_RECOVERY and not n.startswith("llm:")]
    assert actions == ["replace order"]


def test_world_calls_are_counted_per_order() -> None:
    from foood.bench import Run
    from foood.scenario import load
    from foood.world import WorldTools

    bench = Run(load("happy-path"), "hand-wired", 7)
    tools = WorldTools(bench.store, order_id="order-x")
    tools.eta((37.7731, -122.4167), (37.787, -122.3985))
    tools.get_weather("civic")
    WorldTools(bench.store).zones()  # not about an order
    assert bench.store.world.calls_by_order["order-x"] == 2
    assert bench.store.world.calls == {"eta": 1, "get_weather": 1, "zones": 1}


def test_a_run_reports_roles_models_and_world_calls(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("FOOOD_LLM_PRICES", '{"gpt-5.4-mini": {"in": 0.75, "out": 4.5}}')
    monkeypatch.delenv("FOOOD_LLM_PRICE_IN", raising=False)
    monkeypatch.delenv("FOOOD_LLM_PRICE_OUT", raising=False)
    result = run("happy-path", "hand-wired", 7, tmp_path)
    summary = result["summary"]
    assert summary["llmCallsByRole"] == {"decision": 0, "perception": 0, "language": 0}
    assert summary["llmCallsDeciding"] == 0 and summary["llmByModel"] == {}
    assert summary["worldCalls"] == 0 and result["orders"][0]["worldCalls"] == 0
    assert result["llmPrices"]["perModel"] == {"gpt-5.4-mini": {"in": 0.75, "out": 4.5}}
    table = _render(tmp_path)
    assert "| LLM calls, decisions |" in table and "| LLM calls, perception |" in table
    assert "| LLM calls, language |" in table and "| World calls |" in table
    assert "gpt-5.4-mini: 0.75 in and 4.5 out" in table
