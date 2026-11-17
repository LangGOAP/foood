"""The recover fork's advisor: a model picks one of the platform's recovery tools, corralled.

This is how teams add LLM judgement to a workflow today, so it gets the
controls they use: an allow-list of tools per failure, a step budget per
order, a timeout and no retries per call, structured output, and the
hand-wired reaction as the fallback when the model errs, picks a tool it may
not, or the budget is spent. Every call is recorded with its tokens so the
bench can report what the judgement cost.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import BaseModel, Field

DEFAULT_MODEL = "gpt-5.4-mini"
MAX_RECOVERIES = 3
CREDIT_MIN, CREDIT_MAX, CREDIT_DEFAULT = 1.0, 20.0, 5.0

TOOLS = {
    "replace_order": "re-order the same items from another open kitchen; the payment follows",
    "retry_backup_card": "charge the customer's next card on file",
    "issue_credit": "add a credit ($1 to $20) to the customer's account, then carry on as usual",
    "widen_dispatch": "also consider busy drivers finishing nearby, then keep waiting",
    "wait": "keep waiting; for a dropped job, dispatch again",
    "ask_support": "put the choice of tool to a person; they pick from the options",
    "refund_and_cancel": "refund what was paid and cancel the order",
}
ALLOWED: dict[str, list[str]] = {
    "card_declined": ["retry_backup_card", "refund_and_cancel"],
    "payment_gateway_down": ["wait", "refund_and_cancel"],
    "kitchen_rejected": ["replace_order", "ask_support", "refund_and_cancel"],
    "kitchen_cancelled_mid_prep": [
        "replace_order",
        "issue_credit",
        "ask_support",
        "refund_and_cancel",
    ],
    "prep_overrun": ["wait", "issue_credit", "replace_order", "ask_support", "refund_and_cancel"],
    "no_driver": ["wait", "widen_dispatch", "issue_credit", "ask_support", "refund_and_cancel"],
    "driver_dropped_before_pickup": ["wait", "widen_dispatch", "issue_credit"],
    "driver_dropped_after_pickup": [
        "replace_order",
        "issue_credit",
        "ask_support",
        "refund_and_cancel",
    ],
}
TERMINAL_FAILURES = {
    "card_declined",
    "kitchen_rejected",
    "kitchen_cancelled_mid_prep",
    "driver_dropped_after_pickup",
}


class Recovery(BaseModel):
    """What the model answers: one tool from the allow-list, its arguments, and why."""

    tool: str = Field(description="exactly one of the allowed tools")
    amount: float | None = Field(
        default=None, description="issue_credit: dollars, between 1 and 20"
    )
    question: str | None = Field(default=None, description="ask_support: what to ask, one sentence")
    options: list[str] | None = Field(
        default=None,
        description="ask_support: the tools the person may pick, from the allowed list",
    )
    reasoning: str = Field(description="one or two sentences, for the decision log")


@dataclass
class Usage:
    model: str
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def cost(self) -> float | None:
        """USD, only when prices are configured (FOOOD_LLM_PRICE_IN/OUT per million tokens)."""
        price_in, price_out = os.environ.get("FOOOD_LLM_PRICE_IN"), os.environ.get(
            "FOOOD_LLM_PRICE_OUT"
        )
        if not price_in or not price_out:
            return None
        return round(
            (self.input_tokens * float(price_in) + self.output_tokens * float(price_out)) / 1e6, 6
        )


@dataclass
class Context:
    """Everything the model is told. Facts only; the tools are the only verbs."""

    failure: str
    attempt: int
    budget: int
    allowed: list[str]
    order: dict[str, Any]
    kitchen: dict[str, Any]
    customer: dict[str, Any]
    delivery: dict[str, Any] | None
    alternatives: list[dict[str, Any]]
    drivers_online: int
    support_answer: str | None = None
    notes: list[str] = field(default_factory=list)

    def prompt(self) -> str:
        lines = [
            f"Failure: {self.failure} (recovery attempt {self.attempt} of {self.budget}).",
            f"Order: {self.order}",
            f"Kitchen: {self.kitchen}",
            f"Customer: {self.customer}",
            f"Delivery: {self.delivery}",
            f"Other kitchens that can make it: {self.alternatives or 'none'}",
            f"Drivers online and idle: {self.drivers_online}",
        ]
        if self.support_answer:
            lines.append(f"Support answered: {self.support_answer}")
        if self.notes:
            lines.append("Notes: " + "; ".join(self.notes))
        lines.append("Allowed tools: " + "; ".join(f"{t}: {TOOLS[t]}" for t in self.allowed))
        lines.append(
            "Pick exactly one allowed tool. Prefer getting the food to the door within the "
            "promise at a reasonable cost; refund only when nothing else can work."
        )
        return "\n".join(lines)


SYSTEM_PROMPT = (
    "You run order recovery for a food delivery platform. You are given one failed order, "
    "the facts around it and a short list of allowed tools. "
    "Answer with one tool and a short reason."
)


class Advisor(Protocol):
    def advise(self, context: Context) -> tuple[Recovery, Usage]: ...


class OpenAIAdvisor:
    """One structured call, no retries, a timeout; errors are the caller's to catch."""

    def __init__(self, model: str | None = None, timeout: float = 20.0) -> None:
        from langchain_openai import ChatOpenAI

        self.model = model or os.environ.get("FOOOD_LLM_MODEL", DEFAULT_MODEL)
        self.chat = ChatOpenAI(
            model=self.model, timeout=timeout, max_retries=0
        ).with_structured_output(Recovery, include_raw=True)

    def advise(self, context: Context) -> tuple[Recovery, Usage]:
        result = self.chat.invoke([("system", SYSTEM_PROMPT), ("user", context.prompt())])
        parsed = result["parsed"]
        if parsed is None:
            raise ValueError(f"unparsable answer: {result.get('parsing_error')}")
        raw = result["raw"]
        meta = getattr(raw, "usage_metadata", None) or {}
        usage = Usage(
            self.model, int(meta.get("input_tokens", 0)), int(meta.get("output_tokens", 0))
        )
        return parsed, usage


class ScriptedAdvisor:
    """For tests and demos: answers from a script, or raises what the script says to raise."""

    def __init__(self, *answers: Recovery | Exception, tokens: int = 100) -> None:
        self.answers = list(answers)
        self.tokens = tokens
        self.contexts: list[Context] = []

    def advise(self, context: Context) -> tuple[Recovery, Usage]:
        self.contexts.append(context)
        if not self.answers:
            raise RuntimeError("the script ran out of answers")
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer, Usage("scripted", self.tokens // 2, self.tokens // 2)


def advisor_from_env() -> Advisor | None:
    """The real advisor when a key is present and the model is not switched off."""
    if os.environ.get("FOOOD_LLM", "").lower() in {"off", "0", "false", "no"}:
        return None
    if not os.environ.get("OPENAI_API_KEY"):
        return None
    return OpenAIAdvisor()
