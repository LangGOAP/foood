"""LLM-written customer messages, with the deterministic templates as the fallback.

The platform keeps producing every notification with its ``messageKey`` and
``params``, which the customer app localises on its own. When a phraser is
installed, the server's ``message`` is written for the situation instead of
templated: the kitchen's reason in plain words, the driver's name, the credit.
Any error, or no model, leaves the template in place.
"""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, Field

from foood.engine import decide
from foood.llm import Model, OpenAIModel, Usage
from foood.nodes import client_for, model_for, timeout_for
from foood.store import Store

MAX_CHARS = 240


class Message(BaseModel):
    text: str = Field(
        description=f"the message to the customer, plain and warm, under {MAX_CHARS} characters"
    )


SYSTEM_PROMPT = (
    "You write short notifications to food delivery customers. Use only the facts given, in a "
    "warm plain voice, one or two sentences, no promises beyond the facts, no exclamation marks. "
    "Mention the order's short id once. Never invent names, times or reasons."
)


class Phraser(Protocol):
    def phrase(self, facts: dict[str, Any]) -> tuple[str, Usage]: ...


class OpenAIPhraser:
    """The model writes the message from the facts; the template is the fallback on any doubt."""

    def __init__(self, model: Model | str | None = None, timeout: float | None = None) -> None:
        """The phrase node: two warm sentences, so the roster gives it the cheapest model."""
        self.client: Model = (
            model
            if model is not None and not isinstance(model, str)
            else OpenAIModel(
                model or model_for("phrase") or "gpt-5.4-nano", timeout or timeout_for("phrase")
            )
        )
        self.model = self.client.name

    def phrase(self, facts: dict[str, Any]) -> tuple[str, Usage]:
        turn = self.client.complete(
            SYSTEM_PROMPT, [{"role": "user", "content": f"Facts: {facts}"}], []
        )
        text = Message(text=turn.text.strip()).text
        return text, turn.usage


class ScriptedPhraser:
    def __init__(self, *texts: str | Exception) -> None:
        self.texts = list(texts)
        self.facts: list[dict[str, Any]] = []

    def phrase(self, facts: dict[str, Any]) -> tuple[str, Usage]:
        self.facts.append(facts)
        text = self.texts.pop(0) if self.texts else facts["template"]
        if isinstance(text, Exception):
            raise text
        return text, Usage("scripted", 80, 30, 40.0)


def phraser_from_env() -> Phraser | None:
    """The phrase node's phraser when its model is on."""
    client = client_for("phrase")
    return OpenAIPhraser(client) if client is not None else None


def facts_for(store: Store, notification: Any, template: str) -> dict[str, Any]:
    """What the model may say: the event, the template it may improve on, and the order's facts."""
    params = notification.params
    facts: dict[str, Any] = {"event": notification.message_key, "template": template, **params}
    order = store.orders.get(params.get("orderId", ""))
    if order is not None:
        restaurant = store.restaurants.get(order.restaurant_id)
        facts["shortId"] = order.id[:8]
        facts["restaurant"] = restaurant.name if restaurant else None
        facts["items"] = [f"{i.quantity}x {i.name}" for i in order.items]
        facts["status"] = order.status
        facts["promisedAt"] = order.promised_at.strftime("%H:%M") if order.promised_at else None
        facts["cancelledBy"] = order.cancelled_by
        facts["cancellationNote"] = order.cancellation_note
    return facts


def install(store: Store, phraser: Phraser) -> None:
    """Make every notification's message the model's words, template on any failure."""

    def phrase(notification: Any, template: str) -> str | None:
        facts = facts_for(store, notification, template)
        try:
            text, usage = phraser.phrase(facts)
        except Exception:  # noqa: BLE001  # the template is always good enough
            return None
        text = text.strip()
        if not text or len(text) > MAX_CHARS * 2:
            return None
        order_id = notification.params.get("orderId")
        if order_id:
            decide(
                store,
                order_id,
                "phrase",
                because=notification.message_key,
                llmRole="language",
                node="phrase",
                **usage.record(),
            )
        return text

    store.hooks["phrase"] = phrase
