"""The model, for the tier where one frontier model does everything (#47).

One client over OpenAI's Responses API with function tools, a scripted stand-in for tests,
and a ``Usage`` record per call with the model's name, its tokens, its latency and its cost
at that model's own price (``foood.llm_prices``). Every tier records calls through
``Usage`` so the bench can count them by role and by model.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from foood.llm_prices import cost_of

#: the frontier model of the llm tier; FOOOD_LLM_MODEL overrides, "off" disables the model
DEFAULT_MODEL = "gpt-5.4"
DEFAULT_TIMEOUT = 45.0
MAX_OUTPUT_TOKENS = 600


@dataclass
class Usage:
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def cost(self) -> float | None:
        """USD at this model's price, or None when the model is unpriced."""
        return cost_of(self.model, self.input_tokens, self.output_tokens)

    def record(self) -> dict[str, Any]:
        """The fields a decision carries for the bench: calls, tokens, cost, model, latency."""
        return {
            "llmCalls": 1,
            "llmTokens": self.total_tokens,
            "cost": self.cost() or 0,
            "model": self.model,
            "latencyMs": round(self.latency_ms, 1),
        }


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class Turn:
    """One round with the model: its text, the tools it asked for, what it cost."""

    text: str
    tool_calls: list[ToolCall]
    usage: Usage
    response_id: str | None = None


def tool_spec(name: str, description: str, parameters: dict[str, Any]) -> dict[str, Any]:
    """A function tool in the Responses API's shape."""
    schema = dict(parameters)
    schema.setdefault("type", "object")
    schema.setdefault("properties", {})
    return {"type": "function", "name": name, "description": description, "parameters": schema}


class Model(Protocol):
    name: str

    def complete(
        self,
        instructions: str,
        input: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        previous: str | None = None,
    ) -> Turn: ...


class OpenAIModel:
    """The Responses API; no retries and a timeout, so a slow model is a measured fact."""

    def __init__(self, model: str | None = None, timeout: float = DEFAULT_TIMEOUT) -> None:
        from openai import OpenAI

        self.name = model or os.environ.get("FOOOD_LLM_MODEL") or DEFAULT_MODEL
        self.client = OpenAI(timeout=timeout, max_retries=0)

    def complete(
        self,
        instructions: str,
        input: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        previous: str | None = None,
    ) -> Turn:
        started = time.perf_counter()
        request: dict[str, Any] = {
            "model": self.name,
            "instructions": instructions,
            "input": input,
            "max_output_tokens": MAX_OUTPUT_TOKENS,
            "store": True,
        }
        if tools:
            request["tools"] = tools
            request["tool_choice"] = "auto"
        if previous:
            request["previous_response_id"] = previous
        response = self.client.responses.create(**request)
        latency = (time.perf_counter() - started) * 1000
        usage = Usage(
            self.name,
            int(getattr(response.usage, "input_tokens", 0) or 0),
            int(getattr(response.usage, "output_tokens", 0) or 0),
            latency,
        )
        calls = [
            ToolCall(item.call_id, item.name, _arguments(item.arguments))
            for item in response.output
            if getattr(item, "type", None) == "function_call"
        ]
        return Turn(response.output_text or "", calls, usage, response.id)


def _arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


Script = Turn | Exception | Callable[[list[dict[str, Any]]], Turn | Exception]


class ScriptedModel:
    """Answers in order from a script; what it was asked is kept for assertions."""

    name = "scripted"

    def __init__(self, *script: Script, tokens: int = 100, latency_ms: float = 50.0) -> None:
        self.script = list(script)
        self.tokens = tokens
        self.latency_ms = latency_ms
        self.seen: list[dict[str, Any]] = []
        self.calls = 0

    def says(self, text: str, *tool_calls: tuple[str, dict[str, Any]]) -> Turn:
        return Turn(
            text,
            [ToolCall(f"call_{i}", name, args) for i, (name, args) in enumerate(tool_calls)],
            Usage(self.name, self.tokens // 2, self.tokens - self.tokens // 2, self.latency_ms),
            f"resp_{self.calls}",
        )

    def complete(
        self,
        instructions: str,
        input: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        previous: str | None = None,
    ) -> Turn:
        self.seen.append(
            {"instructions": instructions, "input": input, "tools": [t["name"] for t in tools]}
        )
        self.calls += 1
        if not self.script:
            return self.says("")
        answer = self.script.pop(0)
        if callable(answer) and not isinstance(answer, Exception):
            answer = answer(input)
        if isinstance(answer, Exception):
            raise answer
        return answer


def model_from_env() -> Model | None:
    """The real model when a key is present and the model is not switched off."""
    if os.environ.get("FOOOD_LLM_MODEL", "").lower() == "off":
        return None
    if not os.environ.get("OPENAI_API_KEY"):
        return None
    return OpenAIModel()


@dataclass
class Episode:
    """What one run of the agent loop produced, for the record and the tests."""

    rounds: int = 0
    tool_calls: list[str] = field(default_factory=list)
    text: str = ""
    failed: str | None = None
