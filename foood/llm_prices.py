"""Model prices with provenance (#37, #46).

A run is costed only with prices it was given, and every results file records them.
Two ways to give them: ``FOOOD_LLM_PRICES``, a JSON object of per-model prices in USD per
million tokens (``{"gpt-5.4-mini": {"in": 0.75, "out": 4.5}}``), for the branches where
different nodes run different models; and the older pair ``FOOOD_LLM_PRICE_IN`` /
``FOOOD_LLM_PRICE_OUT``, which prices any model not listed. A model with neither is
unpriced, and its cost is None rather than a guess. ``bench.env`` holds the values the
bench targets use, with their source and date.
"""

from __future__ import annotations

import json
import os
from typing import Any


def _pair(name_in: str, name_out: str) -> tuple[float | None, float | None]:
    def read(name: str) -> float | None:
        raw = os.environ.get(name, "").strip()
        return float(raw) if raw else None

    return read(name_in), read(name_out)


def prices() -> dict[str, Any]:
    """``{"default": {"in", "out"}, "models": {name: {"in", "out"}}}`` from the environment."""
    default_in, default_out = _pair("FOOOD_LLM_PRICE_IN", "FOOOD_LLM_PRICE_OUT")
    models: dict[str, dict[str, float]] = {}
    raw = os.environ.get("FOOOD_LLM_PRICES", "").strip()
    if raw:
        for name, entry in json.loads(raw).items():
            models[str(name)] = {"in": float(entry["in"]), "out": float(entry["out"])}
    return {"default": {"in": default_in, "out": default_out}, "models": models}


def price_for(model: str | None) -> tuple[float, float] | None:
    """The (input, output) price of a model, or None when it has none."""
    table = prices()
    if model is not None and model in table["models"]:
        entry = table["models"][model]
        return entry["in"], entry["out"]
    default = table["default"]
    if default["in"] is not None and default["out"] is not None:
        return default["in"], default["out"]
    return None


def cost_of(model: str | None, input_tokens: int, output_tokens: int) -> float | None:
    """USD for one call, or None when the model is unpriced."""
    pair = price_for(model)
    if pair is None:
        return None
    price_in, price_out = pair
    return round((input_tokens * price_in + output_tokens * price_out) / 1e6, 6)
