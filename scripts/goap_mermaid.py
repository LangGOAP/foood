"""Write foood/langgoap_engine.mmd, the drawing of the LangGOAP engine's runtime graph."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "foood" / "langgoap_engine.mmd"


def main() -> int:
    from foood.goap_engine import mermaid

    OUT.write_text(mermaid(), encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
