"""LLM intake: a sentence becomes a cart and an order, through the same doors the apps use.

A three-node graph: ``understand`` asks the model for a structured reading of the
request against the open kitchens' menus, ``resolve`` matches it to real menu
items deterministically and turns anything unclear into a question instead of
an order, ``place`` fills the cart and places the order with every platform
guard still in force (cart drift, radius, duplicates, phone verification).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Protocol, TypedDict

from fastapi import APIRouter, Request
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field

from foood.auth import CurrentUser, StoreDep
from foood.cart import CartItem, cart_of
from foood.engine import decide
from foood.errors import ApiError
from foood.llm import Usage
from foood.nodes import chat_for, model_for, timeout_for
from foood.orders import place_order
from foood.store import Store, User

router = APIRouter(prefix="/api", tags=["intake"])


class WantedItem(BaseModel):
    name: str = Field(description="the dish, as close to the menu's wording as possible")
    quantity: int = Field(default=1, ge=1, le=20)


class IntakeRequest(BaseModel):
    """What the model reads out of the sentence. Nothing here is trusted; resolve checks it."""

    restaurant: str | None = Field(
        default=None, description="the kitchen named or implied, or null"
    )
    items: list[WantedItem] = Field(default_factory=list)
    delivery_notes: str | None = Field(default=None, description="instructions for the driver")
    delivery_address: str | None = Field(default=None, description="only when the text names one")
    question: str | None = Field(
        default=None, description="when the request cannot be fulfilled as asked, what to ask back"
    )


SYSTEM_PROMPT = (
    "You take food orders for a delivery platform. Read the customer's sentence and the menus "
    "of the kitchens that are open, and answer with the kitchen, the dishes and quantities, any "
    "note for the driver, and an address only if one is named. Use the menus' own dish names. "
    "If the request names a dish nobody has, or could be several kitchens, fill `question` "
    "instead of guessing."
)


class IntakeAdvisor(Protocol):
    def understand(self, text: str, catalogue: str) -> tuple[IntakeRequest, Usage]: ...


class OpenAIIntake:
    """The intake node: extraction, so the roster gives it a small model."""

    def __init__(self, model: str | None = None, timeout: float | None = None) -> None:
        from langchain_openai import ChatOpenAI

        self.model = model or model_for("intake") or "gpt-5.4-mini"
        self.chat = ChatOpenAI(
            model=self.model, timeout=timeout or timeout_for("intake"), max_retries=0
        ).with_structured_output(IntakeRequest, include_raw=True)

    def understand(self, text: str, catalogue: str) -> tuple[IntakeRequest, Usage]:
        started = time.perf_counter()
        result = self.chat.invoke(
            [
                ("system", SYSTEM_PROMPT),
                ("user", f"Open kitchens and menus:\n{catalogue}\n\nRequest: {text}"),
            ]
        )
        latency = (time.perf_counter() - started) * 1000
        parsed = result["parsed"]
        if parsed is None:
            raise ValueError(f"unparsable answer: {result.get('parsing_error')}")
        meta = getattr(result["raw"], "usage_metadata", None) or {}
        return parsed, Usage(
            self.model,
            int(meta.get("input_tokens", 0)),
            int(meta.get("output_tokens", 0)),
            latency,
        )


class ScriptedIntake:
    def __init__(self, *readings: IntakeRequest | Exception) -> None:
        self.readings = list(readings)
        self.seen: list[tuple[str, str]] = []

    def understand(self, text: str, catalogue: str) -> tuple[IntakeRequest, Usage]:
        self.seen.append((text, catalogue))
        reading = self.readings.pop(0)
        if isinstance(reading, Exception):
            raise reading
        return reading, Usage("scripted", 60, 40, 30.0)


def intake_from_env() -> IntakeAdvisor | None:
    """The intake node's advisor when its model is on."""
    if chat_for("intake") is None:
        return None
    return OpenAIIntake()


# --- the graph ---------------------------------------------------------------------------------


class IntakeState(TypedDict, total=False):
    text: str
    reading: dict[str, Any]
    lines: list[dict[str, Any]]  # resolved cart lines: menuItemId, name, quantity, price
    restaurant_id: str
    question: str
    order: dict[str, Any]
    usage: dict[str, Any]


def catalogue_text(store: Store) -> str:
    lines = []
    for restaurant in sorted(store.restaurants.values(), key=lambda r: r.name):
        if restaurant.status != "OPEN" or restaurant.approval_status != "APPROVED":
            continue
        items = [
            f"{i.name} ({i.price})"
            for i in store.menu_items.values()
            if i.restaurant_id == restaurant.id and i.available and not i.archived
        ]
        lines.append(f"- {restaurant.name}: {', '.join(items)}")
    return "\n".join(lines)


@dataclass
class Intake:
    store: Store
    advisor: IntakeAdvisor | None
    graph: Any = field(init=False)

    def __post_init__(self) -> None:
        graph: StateGraph[IntakeState, None, IntakeState, IntakeState] = StateGraph(IntakeState)
        graph.add_node("understand", self.understand)
        graph.add_node("resolve", self.resolve)
        graph.add_edge(START, "understand")
        graph.add_edge("understand", "resolve")
        graph.add_edge("resolve", END)
        self.graph = graph.compile()

    def understand(self, state: IntakeState) -> IntakeState:
        if self.advisor is None:
            raise ApiError(503, "Intake needs a model; set OPENAI_API_KEY", error="NoModel")
        reading, usage = self.advisor.understand(state["text"], catalogue_text(self.store))
        return {
            "reading": reading.model_dump(),
            "usage": {
                "model": usage.model,
                "tokens": usage.total_tokens,
                "cost": usage.cost() or 0,
                "latencyMs": round(usage.latency_ms, 1),
            },
        }

    def resolve(self, state: IntakeState) -> IntakeState:
        """Match the reading to real menu items; anything unclear becomes a question."""
        reading = IntakeRequest.model_validate(state["reading"])
        if reading.question:
            return {"question": reading.question}
        if not reading.items:
            return {"question": "What would you like to order, and from which kitchen?"}
        open_kitchens = [
            r
            for r in self.store.restaurants.values()
            if r.status == "OPEN" and r.approval_status == "APPROVED"
        ]
        if reading.restaurant:
            wanted = reading.restaurant.casefold()
            candidates = [r for r in open_kitchens if wanted in r.name.casefold()]
        else:  # whichever open kitchen has every dish
            candidates = [r for r in open_kitchens if self._lines_for(r.id, reading) is not None]
        if not candidates:
            wanted_names = ", ".join(i.name for i in reading.items)
            question = f"No open kitchen has all of: {wanted_names}. Which kitchen did you mean?"
            return {"question": question}
        if len(candidates) > 1:
            names = ", ".join(sorted(r.name for r in candidates))
            return {"question": f"Several kitchens can make that: {names}. Which one?"}
        restaurant = candidates[0]
        lines = self._lines_for(restaurant.id, reading)
        if lines is None:
            missing = ", ".join(
                i.name for i in reading.items if self._match(restaurant.id, i.name) is None
            )
            return {"question": f"{restaurant.name} does not have: {missing}. Something else?"}
        return {"restaurant_id": restaurant.id, "lines": lines}

    def _match(self, restaurant_id: str, name: str) -> Any:
        wanted = name.casefold()
        menu = [
            i
            for i in self.store.menu_items.values()
            if i.restaurant_id == restaurant_id and i.available and not i.archived
        ]
        exact = next((i for i in menu if i.name.casefold() == wanted), None)
        if exact is not None:
            return exact
        loose = [i for i in menu if wanted in i.name.casefold() or i.name.casefold() in wanted]
        return loose[0] if len(loose) == 1 else None

    def _lines_for(self, restaurant_id: str, reading: IntakeRequest) -> list[dict[str, Any]] | None:
        lines = []
        for wanted in reading.items:
            item = self._match(restaurant_id, wanted.name)
            if item is None:
                return None
            lines.append(
                {
                    "menuItemId": item.id,
                    "name": item.name,
                    "quantity": wanted.quantity,
                    "price": str(item.price),
                }
            )
        return lines

    def run(self, user: User, text: str) -> dict[str, Any]:
        state: IntakeState = self.graph.invoke({"text": text})
        reading = IntakeRequest.model_validate(state["reading"])
        if state.get("question"):
            return {
                "status": "question",
                "question": state["question"],
                "reading": state["reading"],
                "usage": state.get("usage"),
            }
        cart = cart_of(self.store, user.id)
        cart.items.clear()
        cart.restaurant_id = state["restaurant_id"]
        for line in state["lines"]:
            item = self.store.menu_items[line["menuItemId"]]
            cart.items.append(
                CartItem(item.id, item.name, item.price, line["quantity"], item.image_url)
            )
        cart.touch()
        order = place_order(
            self.store,
            user,
            delivery_address=reading.delivery_address,
            delivery_notes=reading.delivery_notes,
            latitude=None,
            longitude=None,
        )
        usage = state.get("usage") or {}
        decide(
            self.store,
            order.id,
            "intake",
            because=text,
            llmRole="language",
            node="intake",
            llmCalls=1,
            llmTokens=int(usage.get("tokens", 0)),
            cost=usage.get("cost", 0),
            model=usage.get("model"),
            latencyMs=usage.get("latencyMs", 0),
        )
        return {
            "status": "placed",
            "order": order.to_json(),
            "reading": state["reading"],
            "usage": usage,
        }


# --- route -------------------------------------------------------------------------------------


class IntakeBody(BaseModel):
    text: str = Field(min_length=3, max_length=500)


@router.post("/orders/intake", status_code=200)
async def intake(
    body: IntakeBody, request: Request, user: CurrentUser, store: StoreDep
) -> dict[str, Any]:
    """Outside the Delivery Plus contract: a sentence in, an order (or a question) out."""
    advisor: IntakeAdvisor | None = getattr(request.app.state, "intake", None)
    return Intake(store, advisor).run(user, body.text)
