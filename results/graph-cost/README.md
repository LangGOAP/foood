# What the LangGraph engine asks of LangGraph

`scripts/graph_cost.py` runs every bundled scenario with models off (`FOOOD_LLM=off`, the
engine's coded fallback) on seeds 7, 11 and 13 and counts snapshot reads, `invoke` calls,
super-steps, checkpoint writes, and no-op resumes (a wait routed straight back to itself).
`outcome` is a digest of every order's final status and decision names; equal digests mean
the change kept behaviour.

- `before-a7646ea.jsonl`: the engine at commit a7646ea.
- `after.jsonl`: the engine after the change (the wait's routing asked before a resume, one
  snapshot read per thread change, an append reducer for `handled`), on LangGraph 1.2.24.

Machine: Intel(R) Core(TM) i7-9750H CPU @ 2.60GHz, Darwin x86_64, Python 3.13.14, in-memory checkpointer. The counts depend
on the scenarios and the engine only, not the machine.

Regenerate: `FOOOD_LLM=off uv run python scripts/graph_cost.py 7 11 13 > results/graph-cost/after.jsonl`
