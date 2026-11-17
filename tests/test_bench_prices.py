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
    result = run("happy-path", "hand-wired", 7, tmp_path)
    assert result["llmPrices"] == {"inputPerMillionUsd": None, "outputPerMillionUsd": None}
    assert result["summary"]["llmPriced"] is False
    table = _render(tmp_path)
    assert "| unpriced |" in table and "per million tokens" not in table


def test_a_priced_run_carries_its_prices(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("FOOOD_LLM_PRICE_IN", "0.75")
    monkeypatch.setenv("FOOOD_LLM_PRICE_OUT", "4.50")
    result = run("happy-path", "hand-wired", 7, tmp_path)
    assert result["llmPrices"] == {"inputPerMillionUsd": 0.75, "outputPerMillionUsd": 4.5}
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
    assert "export FOOOD_LLM_PRICE_IN FOOOD_LLM_PRICE_OUT" in makefile


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
