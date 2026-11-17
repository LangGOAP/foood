"""Repeat runs (issue #35): the same inputs several times, so the model is the only variation."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from typer.testing import CliRunner

from foood.bench import _spread, repeat, same_outcomes
from foood.cli import app


def test_repeats_of_a_model_free_run_are_identical(tmp_path: Path) -> None:
    result = repeat("happy-path", "hand-wired", 7, 2, tmp_path)
    path = Path(result["path"])
    assert path == tmp_path / "repeats" / "happy-path-hand-wired-seed7-x2.json"
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["times"] == 2 and len(saved["runs"]) == 2
    spread = saved["distribution"]
    assert spread["identical"] is True
    assert spread["delivered"] == {"2": 2}
    assert spread["llmCalls"] == {"0": 2} and spread["humanPrompts"] == {"0": 2}
    assert spread["unrecovered"] == {"-": 2}
    assert not spread["modelMs"] or spread["modelMs"]["max"] == 0
    assert spread["cost"]["max"] == 0 and spread["llmPriced"] is False


def test_the_recovery_decisions_taken_are_counted_across_runs(tmp_path: Path) -> None:
    result = repeat("payment-trouble", "hand-wired", 7, 2, tmp_path)
    taken = result["distribution"]["decisionsTaken"]
    assert sum(taken.values()) == sum(s["recoveryDecisions"] for s in result["runs"])
    assert sum(taken.values()) > 0 and "confirm" not in taken


def test_the_repeats_table_renders(tmp_path: Path) -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    from bench_table import render

    assert "## Repeats" not in render(tmp_path)
    repeat("happy-path", "hand-wired", 7, 2, tmp_path)
    table = render(tmp_path)
    assert "## Repeats" in table
    assert "| happy-path | hand-wired | 7 | 2 | 2 ×2 | 0 ×2 | 0 ×2 |" in table
    assert "| yes |" in table and table.rstrip().endswith("1 set(s).")


def test_identical_means_the_same_outcomes_not_the_same_tokens() -> None:
    first = {"delivered": 6, "llmCalls": 4, "cost": 0.0076, "llmTokens": 6625, "messageCalls": 42}
    second = {**first, "cost": 0.0079, "llmTokens": 6681}
    assert same_outcomes([first, second])
    assert not same_outcomes([first, {**second, "delivered": 5}])


def test_a_spread_keeps_small_medians() -> None:
    assert _spread([0.0076, 0.0078, 0.0079]) == {"min": 0.0076, "median": 0.0078, "max": 0.0079}
    assert _spread([]) is None


def test_the_cli_takes_a_repeat_count() -> None:
    import re

    shown = CliRunner().invoke(app, ["bench", "--help"]).output
    plain = re.sub(r"\x1b\[[0-9;]*m", "", shown)  # CI renders the help with colour codes
    assert "--repeat" in plain
