"""The committed CONTRACT.md matches the claimed operations."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from contract_table import render  # noqa: E402


def test_contract_table_is_current() -> None:
    assert (ROOT / "CONTRACT.md").read_text(encoding="utf-8") == render()
