"""Write foood/langgraph_engine.mmd, the drawing of the LangGraph engine's lifecycle graph."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "foood" / "langgraph_engine.mmd"


def main() -> int:
    from foood.graph_engine import mermaid

    OUT.write_text(mermaid(), encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
