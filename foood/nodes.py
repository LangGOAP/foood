"""Which model runs which node (#48).

The graph is what lets a business process use different models for different needs: a
sentence into a structured request is extraction, a notification is two warm sentences,
choosing which world tools to call is tool calling, a kitchen's free-text reason is a
classification, a substitute dish takes culinary knowledge, and picking one allowed tool
from the facts is the judgement. ``models.toml`` names a model per node and a timeout per
call; no node retries. The environment overrides it: ``FOOOD_MODELS`` points at another
file, ``FOOOD_MODEL_<NODE>`` sets one node, ``FOOOD_MODEL_ALL`` sets every node (the bench's
model matrix), and ``off`` switches a node's model off so its deterministic fallback applies.
Every call a node makes is recorded on the order with the node's name, so the results files
show what each node cost.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from foood.llm import OpenAIModel

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PATH = ROOT / "models.toml"
NODES = ("intake", "phrase", "lookup", "classify", "suggest", "decide", "escalate")
DEFAULT_TIMEOUT = 20.0
OFF = {"off", "none", "", "0", "false", "no"}


@dataclass
class Roster:
    models: dict[str, str | None] = field(default_factory=dict)
    timeouts: dict[str, float] = field(default_factory=dict)
    path: Path | None = None

    def model_for(self, node: str) -> str | None:
        if node not in NODES:
            raise ValueError(f"no node {node!r}; one of {NODES}")
        return self.models.get(node)

    def timeout_for(self, node: str) -> float:
        return self.timeouts.get(node, DEFAULT_TIMEOUT)

    def to_json(self) -> dict[str, Any]:
        return {
            "path": str(self.path) if self.path else None,
            "models": {node: self.models.get(node) for node in NODES},
            "timeouts": {node: self.timeout_for(node) for node in NODES},
        }


def _clean(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return None if text.lower() in OFF else text


def load(path: Path | str | None = None) -> Roster:
    """The roster from the file, then the environment's overrides on top."""
    chosen = Path(path) if path else Path(os.environ.get("FOOOD_MODELS") or DEFAULT_PATH)
    models: dict[str, str | None] = {}
    timeouts: dict[str, float] = {}
    if chosen.exists():
        raw = tomllib.loads(chosen.read_text(encoding="utf-8"))
        for node, value in raw.get("nodes", {}).items():
            if node not in NODES:
                raise ValueError(f"{chosen}: no node {node!r}; one of {NODES}")
            models[node] = _clean(value)
        for node, value in raw.get("timeouts", {}).items():
            if node not in NODES:
                raise ValueError(f"{chosen}: no node {node!r}; one of {NODES}")
            timeouts[node] = float(value)
    everyone = os.environ.get("FOOOD_MODEL_ALL")
    if everyone is not None:
        models = {node: _clean(everyone) for node in NODES}
    for node in NODES:
        one = os.environ.get(f"FOOOD_MODEL_{node.upper()}")
        if one is not None:
            models[node] = _clean(one)
    return Roster(models, timeouts, chosen if chosen.exists() else None)


def switched_off() -> bool:
    """``FOOOD_LLM=off`` or no key: no node has a model, whatever the roster says."""
    if os.environ.get("FOOOD_LLM", "").lower() in {"off", "0", "false", "no"}:
        return True
    return not os.environ.get("OPENAI_API_KEY")


def model_for(node: str) -> str | None:
    """The node's model name, or None when it is off (by the roster or the environment)."""
    if switched_off():
        return None
    return load().model_for(node)


def timeout_for(node: str) -> float:
    return load().timeout_for(node)


def client_for(node: str) -> OpenAIModel | None:
    """The node's Responses API client (for the nodes that call tools or write prose)."""
    name = model_for(node)
    return OpenAIModel(name, timeout_for(node)) if name else None


def chat_for(node: str) -> Any | None:
    """The node's LangChain chat model (for the nodes that answer in a schema)."""
    name = model_for(node)
    if not name:
        return None
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(model=name, timeout=timeout_for(node), max_retries=0)
