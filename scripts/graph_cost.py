"""Count what the LangGraph engine asks of LangGraph per bench scenario.

Counts, per scenario and seed, with models off (the engine's coded fallback):
  reads      StateSnapshot reads (``get_state``) the engine makes
  invokes    ``graph.invoke`` calls (one per start or resume)
  steps      super-steps LangGraph ran (node executions)
  noop       resumes whose wait routed straight back to the same wait
  writes     checkpoints the saver stored
  outcome    the order statuses at the end, to check a change keeps behaviour

Usage: uv run python scripts/graph_cost.py [seed ...]
"""

from __future__ import annotations

import hashlib
import json
import sys
from collections import Counter
from pathlib import Path
from tempfile import TemporaryDirectory

from langgraph.checkpoint.memory import InMemorySaver

from foood import graph_engine
from foood.bench import run

SCENARIOS = [p.stem for p in sorted(Path("scenarios").glob("*.toml"))]


def measure(scenario: str, seed: int) -> dict[str, object]:
    counts: Counter[str] = Counter()
    original_install = graph_engine.LangGraphEngine.install

    def install(self, store):  # type: ignore[no-untyped-def]
        original_install(self, store)
        graph = self.graph
        get_state, invoke = graph.get_state, graph.invoke
        saver = self.checkpointer
        put = saver.put

        def counted_get_state(*a, **k):  # type: ignore[no-untyped-def]
            counts["reads"] += 1
            return get_state(*a, **k)

        def counted_invoke(inp, config, *a, **k):  # type: ignore[no-untyped-def]
            counts["invokes"] += 1
            before = get_state(config).next
            seen: list[str] = []
            out = None
            for chunk in graph.stream(inp, config, stream_mode="updates", *a, **k):
                seen.extend(n for n in chunk if n != "__interrupt__")
            counts["steps"] += len(seen)
            after = get_state(config).next
            if before and after and seen == [before[0]] and after[0] == before[0]:
                counts["noop"] += 1
            return out

        def counted_put(*a, **k):  # type: ignore[no-untyped-def]
            counts["writes"] += 1
            return put(*a, **k)

        graph.get_state = counted_get_state  # type: ignore[method-assign]
        graph.invoke = counted_invoke  # type: ignore[method-assign]
        saver.put = counted_put  # type: ignore[method-assign]

    graph_engine.LangGraphEngine.install = install  # type: ignore[method-assign]
    try:
        with TemporaryDirectory() as tmp:
            result = run(scenario, "langgraph", seed, Path(tmp))
    finally:
        graph_engine.LangGraphEngine.install = original_install  # type: ignore[method-assign]
    rows = result.get("orders") or result.get("placed") or []
    outcome = [(r.get("status"), r.get("decisionNames")) for r in rows]
    digest = hashlib.sha256(json.dumps(outcome).encode()).hexdigest()[:12]
    return {"scenario": scenario, "seed": seed, **counts, "outcome": digest}


if __name__ == "__main__":
    assert InMemorySaver  # the engine's default saver when no checkpoint path is given
    seeds = [int(s) for s in sys.argv[1:]] or [7]
    for scenario in SCENARIOS:
        for seed in seeds:
            print(json.dumps(measure(scenario, seed)))
