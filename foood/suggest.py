"""Two small-model nodes for the kitchen's failures (#50).

``classify`` reads the kitchen's free-text reason into a kind, because "the last portion just
went out" and "the grill is down" call for different recoveries: a substitute dish, or another
kitchen. ``suggest`` proposes substitutes from what open kitchens can make now, using the
model's culinary knowledge to pick what stands in for what; the platform grounds and prices
the names (``foood.replacements``) and the customer decides. Both are perception calls:
text and menus turned into facts for the fork, never a decision.
"""

from __future__ import annotations

import time
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from foood.llm import Usage
from foood.nodes import chat_for, model_for, timeout_for

ReasonKind = Literal["item_out", "kitchen_down", "temporary", "other"]


class Reason(BaseModel):
    """What a kitchen's note means for the order."""

    kind: ReasonKind = Field(
        description=(
            "item_out: a dish ran out, the kitchen is fine; kitchen_down: the kitchen cannot "
            "cook (equipment, staff, closing); temporary: a short delay, worth waiting; "
            "other: anything else"
        )
    )
    item: str | None = Field(
        default=None, description="the dish that ran out, if the note names one"
    )


class Suggestions(BaseModel):
    """Dishes from the candidate pool that could stand in for what is missing, best first."""

    candidates: list[str] = Field(
        description="up to five dish names, exactly as the pool spells them"
    )
    note: str = Field(default="", description="one short reason, for the record")


CLASSIFY_PROMPT = (
    "You read a restaurant's note about an order it could not fulfil and say what kind of "
    "problem it is. Answer in the schema; name the dish only if the note names one."
)
SUGGEST_PROMPT = (
    "You suggest substitute dishes for a food delivery customer whose dish ran out. You are given "
    "what they ordered, what is missing, and a pool of dishes that open kitchens near them can "
    "make now, with prices. Pick up to five from the pool that best stand in for the missing dish, "
    "closest in kind first (a noodle dish for a noodle dish), spelled exactly as the pool spells "
    "them. Never invent a dish."
)


class Classifier(Protocol):
    def classify(self, note: str, items: list[str]) -> tuple[Reason, Usage]: ...


class Suggester(Protocol):
    def suggest(
        self, ordered: list[str], missing: list[str], pool: list[dict[str, Any]]
    ) -> tuple[Suggestions, Usage]: ...


def _invoke(chat: Any, system: str, user: str, model: str) -> tuple[Any, Usage]:
    started = time.perf_counter()
    result = chat.invoke([("system", system), ("user", user)])
    latency = (time.perf_counter() - started) * 1000
    parsed = result["parsed"]
    if parsed is None:
        raise ValueError(f"unparsable answer: {result.get('parsing_error')}")
    meta = getattr(result["raw"], "usage_metadata", None) or {}
    usage = Usage(
        model, int(meta.get("input_tokens", 0)), int(meta.get("output_tokens", 0)), latency
    )
    return parsed, usage


class OpenAIClassifier:
    """The classify node: a classification, so the roster gives it a small model."""

    def __init__(self, model: str | None = None, timeout: float | None = None) -> None:
        from langchain_openai import ChatOpenAI

        self.model = model or model_for("classify") or "gpt-5.4-nano"
        self.chat = ChatOpenAI(
            model=self.model, timeout=timeout or timeout_for("classify"), max_retries=0
        ).with_structured_output(Reason, include_raw=True)

    def classify(self, note: str, items: list[str]) -> tuple[Reason, Usage]:
        user = f"Order items: {items}. The kitchen's note: {note!r}."
        return _invoke(self.chat, CLASSIFY_PROMPT, user, self.model)


class OpenAISuggester:
    """The suggest node: culinary knowledge, so the roster gives it a mid model."""

    def __init__(self, model: str | None = None, timeout: float | None = None) -> None:
        from langchain_openai import ChatOpenAI

        self.model = model or model_for("suggest") or "gpt-5.4-mini"
        self.chat = ChatOpenAI(
            model=self.model, timeout=timeout or timeout_for("suggest"), max_retries=0
        ).with_structured_output(Suggestions, include_raw=True)

    def suggest(
        self, ordered: list[str], missing: list[str], pool: list[dict[str, Any]]
    ) -> tuple[Suggestions, Usage]:
        lines = [f"- {p['item']} at {p['restaurant']} ({p['price']})" for p in pool[:80]]
        user = (
            f"Ordered: {ordered}. Missing: {missing}.\nThe pool:\n" + "\n".join(lines)
            if lines
            else f"Ordered: {ordered}. Missing: {missing}. The pool is empty."
        )
        return _invoke(self.chat, SUGGEST_PROMPT, user, self.model)


class ScriptedClassifier:
    def __init__(self, *answers: Reason | Exception) -> None:
        self.answers = list(answers)
        self.seen: list[tuple[str, list[str]]] = []

    def classify(self, note: str, items: list[str]) -> tuple[Reason, Usage]:
        self.seen.append((note, items))
        answer = self.answers.pop(0) if self.answers else Reason(kind="other")
        if isinstance(answer, Exception):
            raise answer
        return answer, Usage("scripted", 40, 10, 20.0)


class ScriptedSuggester:
    def __init__(self, *answers: Suggestions | Exception) -> None:
        self.answers = list(answers)
        self.seen: list[dict[str, Any]] = []

    def suggest(
        self, ordered: list[str], missing: list[str], pool: list[dict[str, Any]]
    ) -> tuple[Suggestions, Usage]:
        self.seen.append({"ordered": ordered, "missing": missing, "pool": pool})
        answer = self.answers.pop(0) if self.answers else Suggestions(candidates=[])
        if isinstance(answer, Exception):
            raise answer
        return answer, Usage("scripted", 200, 30, 40.0)


def classifier_from_env() -> Classifier | None:
    return OpenAIClassifier() if chat_for("classify") is not None else None


def suggester_from_env() -> Suggester | None:
    return OpenAISuggester() if chat_for("suggest") is not None else None
