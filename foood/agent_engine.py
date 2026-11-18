"""One frontier model runs the evening (#47): the ``llm`` engine.

The first refactoring, the one most teams ship. On every event that concerns an order the
model is told what happened and given tools: the platform's actions (confirm, dispatch,
replace, substitute, credit, refund, ask the customer or support), lookups (the order's
facts, the kitchens that can make it, the drivers online) and the world's MCP read tools
(weather, traffic, ETA, forecast). It calls what it likes, in the order it likes, until it
stops or the budget is spent; each round is one decision call recorded with the model's
name, tokens, latency and cost. Guards: a round budget and a tool budget per episode, a
timeout and no retries per call, and the hand-wired reaction whenever the model errs or
spends the budget without acting. ``FOOOD_LLM=off`` (or no key) makes it the hand-wired
engine, so the bench numbers match ``main`` exactly.

Timers stand in for the forks a graph would draw: a delivery nobody takes, a kitchen that
runs long, a promise that passes, each raise one episode. Intake (``POST /api/orders/intake``)
is the same loop with the cart as its tools, recorded as a language call.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from foood.auth import CurrentUser, StoreDep
from foood.cart import CartItem, cart_of
from foood.credits import balance, issue_credit
from foood.deliveries import (
    Delivery,
    cancel_delivery,
    create_delivery,
    delivery_for_order,
    dispatch_pending,
)
from foood.dispatch import stack_on_finishing, use
from foood.drivers import Driver, available_drivers
from foood.engine import HandWiredEngine, decide
from foood.errors import ApiError
from foood.llm import Episode, Model, Turn, model_from_env, tool_spec
from foood.models import money
from foood.money import dollars
from foood.orders import (
    TERMINAL,
    Order,
    fail_payment,
    get_order,
    place_order,
    revise_promise,
    transition,
)
from foood.payments import (
    Payment,
    abandon,
    payment_for_order,
    refund_payment,
    retry_pending_payment,
    retry_with_backup,
)
from foood.replacements import (
    Alternative,
    alternatives,
    candidate_pool,
    offer_substitutes,
    replace,
    substitutes,
)
from foood.store import Store, User, iso, now
from foood.support import Question, ask
from foood.world import READ_TOOLS, WorldTools, eta_minutes, zone_of
from foood.world_mcp import build_server, call_tool, tool_specs

MAX_ROUNDS = 6  # model rounds per episode
MAX_TOOL_CALLS = 10  # tool calls per episode
NO_DRIVER_AFTER = 90.0  # seconds a delivery may wait before the model hears about it
PREP_OVERRUN_FACTOR = 3.0  # the kitchen is "running long" past this many times its prep time
CREDIT_MIN, CREDIT_MAX, CREDIT_DEFAULT = 1.0, 20.0, 5.0

INSTRUCTIONS = (
    "You run one order's lifecycle for foood, a food delivery platform in San Francisco. "
    "You are told what just happened. Look things up with the tools when that helps, then act "
    "with the action tools; one situation usually needs one action. Get the food to the door "
    "within the promise at a reasonable cost: another kitchen, the backup card, a wider driver "
    "search, a substitute the customer can accept, a credit when the customer waits; refund "
    "only when nothing else can work. The weather and traffic tools describe the city; use them "
    "when a promise is at risk. When nothing needs doing, call wait. End with one sentence."
)
INTAKE_INSTRUCTIONS = (
    "You take food orders in a sentence for foood. Find the dishes on the open kitchens' menus "
    "with search_menu, put them in the cart with add_to_cart (one kitchen per order), and place "
    "the order with place_order. If a dish is unknown, sold out, or the sentence is ambiguous, "
    "call ask_customer with one short question instead of guessing. End with one sentence."
)


class AgentEngine:
    """The model is the program: it hears every event and calls the tools it wants."""

    name = "llm"

    def __init__(self, model: Model | None = None) -> None:
        self.model: Model | None = model if model is not None else model_from_env()
        self.model_name: str | None = self.model.name if self.model else None
        self.episodes: list[Episode] = []

    # --- wiring ------------------------------------------------------------------------------

    def install(self, store: Store) -> None:
        self.store = store
        self.fallback = HandWiredEngine()
        self.fallback.store = store
        if self.model is None:  # no model: the baseline, exactly
            self.fallback.install(store)
            return
        self.world = build_server(store)
        self.world_tools = [
            tool_spec(s["name"], s["description"], s["parameters"])
            for s in tool_specs(self.world, READ_TOOLS)
        ]
        self.raised: dict[str, set[str]] = {}  # order id -> timers already raised
        store.subscribe("payment.processed", self.on_payment)
        store.subscribe("payment.unavailable", self.on_unavailable)
        store.subscribe("payment.gateway_up", self.on_gateway_up)
        store.subscribe("order.status", self.on_order_status)
        store.subscribe("driver.available", lambda driver: self.dispatch("a driver came online"))
        store.subscribe("delivery.dropped", self.on_dropped)
        store.subscribe("support.answered", self.on_support_answered)
        store.subscribe("order.substitution_answered", self.on_substitution_answered)
        store.subscribe("world.changed", self.on_world_changed)
        store.tickers.append(self.tick)

    # --- events: each one is a situation the model hears -------------------------------------

    def on_payment(self, payment: Payment) -> None:
        order = get_order(self.store, payment.order_id)
        if payment.status == "COMPLETED":
            text = f"The payment of {dollars(payment.amount)} for the order completed."
        elif payment.status == "FAILED":
            text = (
                f"The payment for the order failed: {payment.failure_reason or 'card declined'}. "
                f"Cards on file: {len(self.store.users[order.customer_id].cards)}."
            )
        else:
            return
        self.episode(order, text, lambda: self.fallback.on_payment(payment))

    def on_unavailable(self, payment: Payment) -> None:
        order = get_order(self.store, payment.order_id)
        self.episode(
            order,
            "The payment gateway is unavailable: the payment is pending and nothing was charged. "
            "You will hear when the gateway is back; retry_payment charges the attempt then.",
            lambda: self.fallback.on_unavailable(payment),
        )

    def on_gateway_up(self, **_: Any) -> None:
        for payment in list(self.store.payments.values()):
            if payment.status != "PENDING":
                continue
            order = self.store.orders.get(payment.order_id)
            if order is None or order.status in TERMINAL:
                continue
            self.episode(order, "The payment gateway is back; this order's payment is pending.")

    def on_order_status(self, order: Order, previous: str) -> None:
        if order.status == "READY_FOR_PICKUP" and delivery_for_order(self.store, order.id) is None:
            self.episode(
                order,
                "The kitchen is ready: the order can be picked up.",
                lambda: self.fallback.on_order_status(order, previous),
            )
        elif order.status == "CANCELLED":
            by = order.cancelled_by or "SYSTEM"
            note = order.cancellation_note or order.cancellation_reason or "no reason given"
            sold_out = self.sold_out(order)
            text = f"The order was cancelled by {by}: {note}."
            if sold_out:
                text += f" Sold out at this kitchen now: {', '.join(sold_out)}."
            self.episode(order, text, lambda: self.fallback.on_order_status(order, previous))

    def on_dropped(
        self, delivery: Delivery, driver: Driver, reason: str | None, stage: str
    ) -> None:
        order = get_order(self.store, delivery.order_id)
        when = "before pickup" if stage == "before_pickup" else "after pickup, with the food"
        self.episode(
            order,
            f"The driver dropped the job {when}: {reason or 'no reason given'}.",
            lambda: self.fallback.on_dropped(delivery, driver, reason, stage),
        )

    def on_support_answered(self, question: Question) -> None:
        order = self.store.orders.get(question.order_id)
        if order is None or order.status in TERMINAL and order.replaced_by_order_id:
            return
        self.episode(
            order, f"Support answered your question ({question.prompt!r}): {question.answer!r}."
        )

    def on_substitution_answered(
        self, order: Order, offer: Any, alternative: Alternative | None
    ) -> None:
        if alternative is None:
            text = "The customer declined every substitute you offered."
        else:
            text = (
                f"The customer accepted a substitute: {alternative.restaurant.name} makes "
                f"{', '.join(i.name for _, i in alternative.items)} for a difference of "
                f"{money(alternative.difference(order))}. apply_substitution carries it out."
            )
        self.episode(order, text)

    def on_world_changed(self, change: dict[str, Any]) -> None:
        for order in list(self.store.orders.values()):
            if order.status in TERMINAL or order.replaced_by_order_id:
                continue
            delivery = delivery_for_order(self.store, order.id)
            if delivery is not None and delivery.status in {"DELIVERED", "CANCELLED"}:
                continue
            self.episode(order, f"The world changed: {json.dumps(change)}.")

    def tick(self) -> None:
        """The forks a graph would draw as timed waits: raised once per order each."""
        moment = now()
        for order in list(self.store.orders.values()):
            if order.status in TERMINAL or order.replaced_by_order_id:
                continue
            raised = self.raised.setdefault(order.id, set())
            delivery = delivery_for_order(self.store, order.id)
            if delivery is not None and delivery.status == "CREATED" and "no_driver" not in raised:
                waited = (moment - delivery.created_at).total_seconds()
                if waited >= NO_DRIVER_AFTER:
                    raised.add("no_driver")
                    self.episode(
                        order, f"No driver has taken the delivery for {int(waited)} seconds."
                    )
            if order.status == "PREPARING" and "prep_overrun" not in raised:
                kitchen = self.store.restaurants[order.restaurant_id]
                cooking = (moment - order.updated_at).total_seconds()
                if cooking >= kitchen.prep_minutes * 60 * PREP_OVERRUN_FACTOR:
                    raised.add("prep_overrun")
                    self.episode(
                        order,
                        f"The kitchen has been preparing the order for {int(cooking // 60)} "
                        f"minutes, against its usual {kitchen.prep_minutes}.",
                    )
            if order.promise_breached and "late" not in raised and order.status != "DELIVERED":
                raised.add("late")
                self.episode(order, "The promised time has passed and the order is not delivered.")

    def dispatch(self, because: str) -> None:
        for delivery, driver in dispatch_pending(self.store):
            decide(
                self.store,
                delivery.order_id,
                "assign driver",
                because=because,
                driverId=driver.id,
                licensePlate=driver.license_plate,
            )

    # --- the loop ----------------------------------------------------------------------------

    def episode(
        self, order: Order, situation: str, fallback: Callable[[], None] | None = None
    ) -> Episode:
        """Tell the model what happened and let it act; the hand-wired reaction if it cannot."""
        assert self.model is not None
        store = self.store
        episode = Episode()
        self.episodes.append(episode)
        acted = False
        tools = self.tools_for(order)
        specs = [t["spec"] for t in tools.values()] + self.world_tools
        world = WorldTools(store, order.id)  # counts the model's world calls against the order
        input_items: list[dict[str, Any]] = [
            {"role": "user", "content": f"{self.facts(order)}\n\nWhat happened: {situation}"}
        ]
        previous: str | None = None
        try:
            while episode.rounds < MAX_ROUNDS:
                turn: Turn = self.model.complete(
                    INSTRUCTIONS, input_items, specs, previous=previous
                )
                episode.rounds += 1
                names = [c.name for c in turn.tool_calls]
                decide(
                    store,
                    order.id,
                    "llm: " + (", ".join(names) if names else "done"),
                    because=(turn.text or situation)[:240],
                    llmRole="decision",
                    **turn.usage.record(),
                )
                if not turn.tool_calls:
                    episode.text = turn.text
                    break
                outputs: list[dict[str, Any]] = []
                for call in turn.tool_calls:
                    if len(episode.tool_calls) >= MAX_TOOL_CALLS:
                        result: Any = {"error": "tool budget spent; act now or wait"}
                    elif call.name in READ_TOOLS:
                        result = self.world_lookup(world, call.name, call.arguments)
                    elif call.name in tools:
                        result = self.run_tool(order, tools[call.name], call.arguments)
                        acted = acted or tools[call.name]["action"]
                    else:
                        result = {"error": f"no tool named {call.name}"}
                    episode.tool_calls.append(call.name)
                    outputs.append(
                        {
                            "type": "function_call_output",
                            "call_id": call.id,
                            "output": json.dumps(result, default=str)[:4000],
                        }
                    )
                    if call.name == "wait":
                        acted = True
                input_items = outputs
                previous = turn.response_id
        except Exception as exc:  # the model, the network, a tool that blew up
            episode.failed = f"{type(exc).__name__}: {exc}"[:200]
            decide(store, order.id, "fallback", because=f"model error: {episode.failed}")
            if fallback is not None and not acted:
                fallback()
            return episode
        if not acted and fallback is not None:
            decide(store, order.id, "fallback", because="the model spent its budget without acting")
            fallback()
        return episode

    # --- what the model is told ---------------------------------------------------------------

    def facts(self, order: Order) -> str:
        store = self.store
        kitchen = store.restaurants[order.restaurant_id]
        customer = store.users[order.customer_id]
        payment = payment_for_order(store, order.id)
        delivery = delivery_for_order(store, order.id)
        door = (
            (order.delivery_latitude, order.delivery_longitude)
            if order.delivery_latitude is not None and order.delivery_longitude is not None
            else None
        )
        facts = {
            "order": {
                "id": order.id[:8],
                "status": order.status,
                "items": [f"{i.quantity}x {i.name}" for i in order.items],
                "total": money(order.total),
                "promisedAt": iso(order.promised_at) if order.promised_at else None,
                "promiseBreached": order.promise_breached,
                "now": iso(now()),
            },
            "kitchen": {
                "name": kitchen.name,
                "zone": zone_of(kitchen.latitude, kitchen.longitude),
                "latitude": kitchen.latitude,
                "longitude": kitchen.longitude,
                "prepMinutes": kitchen.prep_minutes,
            },
            "door": (
                {"zone": zone_of(*door), "latitude": door[0], "longitude": door[1]}
                if door
                else None
            ),
            "payment": payment.status if payment else None,
            "delivery": (
                {"status": delivery.status, "searchSeconds": delivery.to_json()["searchSeconds"]}
                if delivery
                else None
            ),
            "customer": {
                "cardsOnFile": len(customer.cards),
                "creditBalance": str(balance(store, customer.id)),
                "substitutionPolicy": customer.substitution_policy,
            },
            "driversOnlineIdle": len(available_drivers(store)),
        }
        return f"Facts: {json.dumps(facts, default=str)}"

    def sold_out(self, order: Order) -> list[str]:
        gone = []
        for line in order.items:
            item = next(
                (
                    i
                    for i in self.store.menu_items.values()
                    if i.restaurant_id == order.restaurant_id
                    and i.name.casefold() == line.name.casefold()
                ),
                None,
            )
            if item is None or not item.available:
                gone.append(line.name)
        return gone

    # --- the tools ---------------------------------------------------------------------------

    def tools_for(self, order: Order) -> dict[str, dict[str, Any]]:
        """Name -> {spec, run, action}; ``run`` takes the arguments and returns JSON-able data."""
        store = self.store

        def lookup(
            name: str, description: str, run: Callable[..., Any], **params: Any
        ) -> dict[str, Any]:
            return {
                "spec": tool_spec(
                    name, description, {"properties": params, "required": list(params)}
                ),
                "run": run,
                "action": False,
            }

        def action(
            name: str, description: str, run: Callable[..., Any], **params: Any
        ) -> dict[str, Any]:
            tool = lookup(name, description, run, **params)
            tool["action"] = True
            return tool

        def fresh() -> Order:
            return get_order(store, order.id)

        def customer() -> User:
            return store.users[order.customer_id]

        def do_confirm() -> Any:
            decide(store, order.id, "confirm", because="the model confirmed the payment")
            return confirm_or_error(lambda: confirm_paid_json(store, order.id))

        def do_retry_payment() -> Any:
            payment = retry_pending_payment(store, order.id)
            return {"ok": payment is not None, "status": payment.status if payment else None}

        def do_retry_backup() -> Any:
            payment = payment_for_order(store, order.id)
            if payment is None:
                return {"error": "no payment to retry"}
            attempt = retry_with_backup(store, payment)
            decide(store, order.id, "act: retry_backup_card", because="charging the backup card")
            return {"ok": True, "status": attempt.status, "attempt": attempt.attempt}

        def do_cancel(reason: str) -> Any:
            current = fresh()
            if current.status not in TERMINAL:
                payment = payment_for_order(store, current.id)
                if payment is not None and payment.status == "PENDING":
                    abandon(store, payment, reason)
                    fail_payment(store, current.id, "PAYMENT_ABANDONED")
                else:
                    transition(
                        store,
                        current,
                        "CANCELLED",
                        cancelled_by="SYSTEM",
                        cancellation_reason="SYSTEM",
                        cancellation_note=reason,
                    )
            decide(store, order.id, "cancel", because=reason)
            refund_if_paid(store, current, reason)
            drop_delivery(store, current, reason)
            return {"ok": True, "status": fresh().status}

        def do_refund(reason: str) -> Any:
            refund = refund_if_paid(store, fresh(), reason)
            decide(store, order.id, "refund", because=reason)
            return {"ok": refund is not None}

        def do_create_delivery() -> Any:
            if delivery_for_order(store, order.id) is not None:
                return {"error": "a delivery exists already"}
            decide(store, order.id, "create delivery", because="the model sent the order out")
            create_delivery(store, fresh())
            self.dispatch("a delivery was created")
            made = delivery_for_order(store, order.id)
            return {"ok": True, "deliveryStatus": made.status if made else None}

        def do_dispatch(reason: str) -> Any:
            pairs = []
            for delivery, driver in dispatch_pending(store):
                decide(
                    store,
                    delivery.order_id,
                    "assign driver",
                    because=reason,
                    driverId=driver.id,
                    licensePlate=driver.license_plate,
                )
                pairs.append(driver.license_plate)
            return {"assigned": pairs, "driversOnlineIdle": len(available_drivers(store))}

        def do_widen() -> Any:
            use(store, stack_on_finishing)
            decide(
                store,
                order.id,
                "widen dispatch",
                because="busy drivers finishing nearby may take it",
            )
            self.dispatch("the search was widened")
            d = delivery_for_order(store, order.id)
            return {"ok": True, "deliveryStatus": d.status if d else None}

        def do_replace(
            restaurant_id: str | None = None, reason: str = "the kitchen could not make it"
        ) -> Any:
            options = alternatives(store, fresh())
            if restaurant_id:
                options = [o for o in options if o.restaurant.id == restaurant_id]
            if not options:
                return {"error": "no other kitchen can make this order"}
            choice = options[0]
            replacement = replace(store, fresh(), choice, reason)
            decide(
                store,
                order.id,
                "replace order",
                because=f"{choice.restaurant.name} makes it instead",
                replacementOrderId=replacement.id,
            )
            return {
                "ok": True,
                "restaurant": choice.restaurant.name,
                "replacementOrderId": replacement.id,
            }

        def do_offer(candidates: list[str], reason: str) -> Any:
            options = substitutes(store, fresh(), [str(c) for c in candidates][:8])
            if not options:
                return {"error": "no open kitchen can make any of those; try other dishes"}
            offer = offer_substitutes(store, fresh(), options[:4], reason)
            decide(
                store,
                order.id,
                "offer substitutes",
                because=", ".join(
                    f"{o.restaurant.name}: {', '.join(i.name for _, i in o.items)}"
                    for o in offer.options
                ),
            )
            return {"ok": True, "offered": [o.to_json(order, customer()) for o in offer.options]}

        def do_apply_substitution() -> Any:
            offer = store.offers.get(order.id)
            if offer is None or offer.chosen is None:
                return {"error": "the customer has not accepted a substitute"}
            replacement = replace(store, fresh(), offer.chosen, offer.reason)
            decide(
                store,
                order.id,
                "replace order",
                because=f"{offer.chosen.restaurant.name} makes the accepted substitute",
                replacementOrderId=replacement.id,
            )
            return {"ok": True, "replacementOrderId": replacement.id}

        def do_credit(amount: float, reason: str) -> Any:
            value = Decimal(str(min(CREDIT_MAX, max(CREDIT_MIN, float(amount or CREDIT_DEFAULT)))))
            try:
                entry = issue_credit(store, order.customer_id, value, reason, order.id)
            except ApiError as refused:
                return {"error": str(refused.detail)}
            decide(store, order.id, "issue credit", because=f"{money(value)}: {reason}")
            return {"ok": True, "amount": money(entry.amount)}

        def do_promise(minutes_from_now: float, reason: str) -> Any:
            from datetime import timedelta

            moved = revise_promise(
                store, fresh(), now() + timedelta(minutes=float(minutes_from_now)), reason
            )
            decide(store, order.id, "revise promise", because=reason)
            return {"ok": True, "promisedAt": iso(moved.promised_at) if moved.promised_at else None}

        def do_ask(question: str, options: list[str], default: str | None = None) -> Any:
            choices = [str(o) for o in options][:5] or ["wait", "refund"]
            asked = ask(
                store,
                order.id,
                "llm",
                question,
                options=[{"value": o, "label": o} for o in choices],
                default=default if default in choices else choices[0],
                deadline_seconds=120,
            )
            decide(store, order.id, "ask support", because=question)
            return {"ok": True, "questionId": asked.id, "options": choices}

        def do_wait(reason: str) -> Any:
            decide(store, order.id, "keep waiting", because=reason)
            return {"ok": True}

        def see_facts() -> Any:
            return json.loads(self.facts(fresh())[len("Facts: ") :])

        def see_alternatives() -> Any:
            return [a.to_json(fresh(), customer()) for a in alternatives(store, fresh())[:5]]

        def see_pool() -> Any:
            return candidate_pool(store, fresh())[:60]

        def see_drivers() -> Any:
            return {"online": len(available_drivers(store))}

        def see_eta() -> Any:
            current = fresh()
            kitchen = store.restaurants[current.restaurant_id]
            if current.delivery_latitude is None or current.delivery_longitude is None:
                return {"error": "the order has no drop-off coordinates"}
            minutes = eta_minutes(
                store,
                (kitchen.latitude, kitchen.longitude),
                (current.delivery_latitude, current.delivery_longitude),
            )
            WorldTools(store, order.id)._count("eta")
            return {
                "rideMinutesNow": minutes,
                "promisedAt": iso(current.promised_at) if current.promised_at else None,
                "now": iso(now()),
            }

        string = {"type": "string"}
        number = {"type": "number"}
        return {
            "order_facts": lookup(
                "order_facts",
                "The order, its payment, delivery, kitchen and customer now.",
                see_facts,
            ),
            "alternatives": lookup(
                "alternatives",
                "Open kitchens that can make the same items, priced.",
                see_alternatives,
            ),
            "candidate_pool": lookup(
                "candidate_pool",
                "What open kitchens within reach can make now, for choosing substitutes.",
                see_pool,
            ),
            "drivers_online": lookup(
                "drivers_online", "How many drivers are online and idle.", see_drivers
            ),
            "ride_eta": lookup(
                "ride_eta",
                "Minutes from the kitchen to the door now, weather and traffic included.",
                see_eta,
            ),
            "confirm_payment": action(
                "confirm_payment",
                "Confirm the order after a completed payment; the kitchen starts.",
                do_confirm,
            ),
            "retry_payment": action(
                "retry_payment",
                "Charge a pending payment attempt again (after a gateway outage).",
                do_retry_payment,
            ),
            "retry_backup_card": action(
                "retry_backup_card",
                "Charge the customer's next card on file after a decline.",
                do_retry_backup,
            ),
            "cancel_order": action(
                "cancel_order",
                "Cancel the order, refund what was paid and cancel its delivery.",
                do_cancel,
                reason=string,
            ),
            "refund": action(
                "refund", "Refund the payment without changing the order.", do_refund, reason=string
            ),
            "create_delivery": action(
                "create_delivery",
                "Send the ready order out: create its delivery and dispatch a driver.",
                do_create_delivery,
            ),
            "dispatch": action(
                "dispatch",
                "Hand every waiting delivery to an idle driver.",
                do_dispatch,
                reason=string,
            ),
            "widen_dispatch": action(
                "widen_dispatch",
                "Also consider busy drivers finishing nearby, then dispatch.",
                do_widen,
            ),
            "replace_order": action(
                "replace_order",
                "Re-order the same items from another open kitchen; the payment follows. "
                "restaurant_id is optional (the cheapest nearest kitchen otherwise).",
                do_replace,
                restaurant_id={"type": ["string", "null"]},
                reason=string,
            ),
            "offer_substitutes": action(
                "offer_substitutes",
                "Offer the customer substitute dishes by name (from candidate_pool); the platform "
                "prices what open kitchens can make and asks the customer.",
                do_offer,
                candidates={"type": "array", "items": string},
                reason=string,
            ),
            "apply_substitution": action(
                "apply_substitution",
                "Carry out the substitute the customer accepted.",
                do_apply_substitution,
            ),
            "issue_credit": action(
                "issue_credit",
                f"Add a credit ({money(CREDIT_MIN)} to {money(CREDIT_MAX)}) to the customer.",
                do_credit,
                amount=number,
                reason=string,
            ),
            "revise_promise": action(
                "revise_promise",
                "Move the promised time and tell the customer why.",
                do_promise,
                minutes_from_now=number,
                reason=string,
            ),
            "ask_support": action(
                "ask_support",
                "Put a choice to a person at support, with the options.",
                do_ask,
                question=string,
                options={"type": "array", "items": string},
                default={"type": ["string", "null"]},
            ),
            "wait": action(
                "wait",
                "Do nothing now; the platform will tell you what happens next.",
                do_wait,
                reason=string,
            ),
        }

    def run_tool(self, order: Order, tool: dict[str, Any], arguments: dict[str, Any]) -> Any:
        try:
            return tool["run"](**arguments)
        except ApiError as refused:
            return {"error": str(refused.detail)}
        except TypeError as bad:
            return {"error": f"bad arguments: {bad}"}

    def world_lookup(self, world: WorldTools, name: str, arguments: dict[str, Any]) -> Any:
        world._count(name)  # against the order; the server counts it by tool as well
        try:
            result = call_tool(self.world, name, arguments)
        except Exception as exc:  # a bad argument is the model's to see, not ours to raise
            return {"error": str(exc)[:200]}
        return mcp_payload(result)

    # --- intake --------------------------------------------------------------------------------

    def intake(self, user: User, text: str) -> dict[str, Any]:
        """A sentence becomes an order (or one question) through the same loop with cart tools."""
        if self.model is None:
            raise ApiError(503, "Intake needs a model; set OPENAI_API_KEY", error="NoModel")
        store = self.store
        outcome: dict[str, Any] = {}
        usage_total = {"calls": 0, "tokens": 0, "cost": 0.0, "latencyMs": 0.0}

        def search_menu(query: str) -> Any:
            needle = query.casefold().strip()
            found = []
            for item in store.menu_items.values():
                kitchen = store.restaurants.get(item.restaurant_id)
                if kitchen is None or kitchen.status != "OPEN" or item.archived:
                    continue
                if needle in item.name.casefold() or needle in kitchen.name.casefold():
                    found.append(
                        {
                            "menuItemId": item.id,
                            "name": item.name,
                            "restaurant": kitchen.name,
                            "restaurantId": kitchen.id,
                            "price": money(item.price),
                            "available": item.available,
                        }
                    )
            return found[:20]

        def add_to_cart(menu_item_id: str, quantity: int = 1) -> Any:
            item = store.menu_items.get(menu_item_id)
            if item is None or not item.available or item.archived:
                return {"error": "no such item, or it is unavailable"}
            cart = cart_of(store, user.id)
            if cart.items and cart.restaurant_id != item.restaurant_id:
                return {"error": "the cart holds another kitchen's items; one kitchen per order"}
            line = cart.find(item.id)
            if line is None:
                cart.items.append(
                    CartItem(item.id, item.name, item.price, int(quantity), item.image_url)
                )
            else:
                line.quantity += int(quantity)
            cart.restaurant_id = item.restaurant_id
            cart.touch()
            return {"ok": True, "cart": cart.to_json()}

        def place(delivery_address: str | None = None, delivery_notes: str | None = None) -> Any:
            order = place_order(
                store,
                user,
                delivery_address=delivery_address,
                delivery_notes=delivery_notes,
                latitude=None,
                longitude=None,
            )
            outcome["order"] = order
            return {"ok": True, "orderId": order.id, "total": money(order.total)}

        def ask_customer(question: str) -> Any:
            outcome["question"] = question
            return {"ok": True}

        tools = {
            "search_menu": tool_spec(
                "search_menu",
                "Find dishes (or kitchens) by a word of their name on the open menus.",
                {"properties": {"query": {"type": "string"}}, "required": ["query"]},
            ),
            "add_to_cart": tool_spec(
                "add_to_cart",
                "Put a found dish in the customer's cart.",
                {
                    "properties": {
                        "menu_item_id": {"type": "string"},
                        "quantity": {"type": "integer"},
                    },
                    "required": ["menu_item_id", "quantity"],
                },
            ),
            "place_order": tool_spec(
                "place_order",
                "Place the order from the cart; an address only when the sentence names one.",
                {
                    "properties": {
                        "delivery_address": {"type": ["string", "null"]},
                        "delivery_notes": {"type": ["string", "null"]},
                    },
                    "required": [],
                },
            ),
            "ask_customer": tool_spec(
                "ask_customer",
                "Ask the customer one short question instead of ordering.",
                {"properties": {"question": {"type": "string"}}, "required": ["question"]},
            ),
        }
        runners: dict[str, Callable[..., Any]] = {
            "search_menu": search_menu,
            "add_to_cart": add_to_cart,
            "place_order": place,
            "ask_customer": ask_customer,
        }
        input_items: list[dict[str, Any]] = [{"role": "user", "content": text}]
        previous = None
        rounds = 0
        final = ""
        while rounds < MAX_ROUNDS and "order" not in outcome and "question" not in outcome:
            turn = self.model.complete(
                INTAKE_INSTRUCTIONS, input_items, list(tools.values()), previous=previous
            )
            rounds += 1
            usage_total["calls"] += 1
            usage_total["tokens"] += turn.usage.total_tokens
            usage_total["cost"] = round(usage_total["cost"] + (turn.usage.cost() or 0), 6)
            usage_total["latencyMs"] += turn.usage.latency_ms
            final = turn.text or final
            if not turn.tool_calls:
                break
            outputs = []
            for call in turn.tool_calls:
                runner = runners.get(call.name)
                try:
                    result = (
                        runner(**call.arguments) if runner else {"error": f"no tool {call.name}"}
                    )
                except ApiError as refused:
                    result = {"error": str(refused.detail)}
                except TypeError as bad:
                    result = {"error": f"bad arguments: {bad}"}
                outputs.append(
                    {
                        "type": "function_call_output",
                        "call_id": call.id,
                        "output": json.dumps(result, default=str)[:4000],
                    }
                )
            input_items = outputs
            previous = turn.response_id
        if "order" in outcome:
            order = outcome["order"]
            decide(
                store,
                order.id,
                "intake",
                because=text[:240],
                llmRole="language",
                llmCalls=usage_total["calls"],
                llmTokens=usage_total["tokens"],
                cost=usage_total["cost"],
                model=self.model_name,
                latencyMs=round(usage_total["latencyMs"], 1),
            )
            return {"status": "placed", "order": order.to_json(), "usage": usage_total}
        return {
            "status": "question",
            "question": outcome.get("question") or final or "Could you say that again?",
            "usage": usage_total,
        }


# --- helpers ----------------------------------------------------------------------------------


def confirm_or_error(run: Callable[[], Any]) -> Any:
    try:
        return run()
    except ApiError as refused:
        return {"error": str(refused.detail)}


def confirm_paid_json(store: Store, order_id: str) -> dict[str, Any]:
    from foood.orders import confirm_paid

    order = confirm_paid(store, order_id)
    return {"ok": True, "status": order.status}


def refund_if_paid(store: Store, order: Order, reason: str) -> Any:
    payment = payment_for_order(store, order.id)
    if payment is None or payment.status != "COMPLETED":
        return None
    if payment.id in store.refunds:
        return store.refunds[payment.id]
    return refund_payment(store, payment, reason[:60] or "ORDER_CANCELLED")


def drop_delivery(store: Store, order: Order, reason: str) -> None:
    delivery = delivery_for_order(store, order.id)
    if delivery is not None and delivery.status not in {"DELIVERED", "CANCELLED"}:
        cancel_delivery(store, delivery, reason, "SYSTEM")


def mcp_payload(result: Any) -> Any:
    """What an MCP tool returned, as data: structured content when the server sends it, the
    text otherwise; a call_tool that returns (content, structured) or a plain value also works."""
    if isinstance(result, tuple) and len(result) == 2:
        content, structured = result
        if structured:
            return structured
        result = content
    structured = getattr(result, "structuredContent", None) or getattr(
        result, "structured_content", None
    )
    if structured:
        return structured
    content = getattr(result, "content", result)
    if isinstance(content, list):
        texts = [getattr(c, "text", None) for c in content]
        texts = [t for t in texts if t is not None]
        if len(texts) == 1:
            try:
                return json.loads(str(texts[0]))
            except (json.JSONDecodeError, TypeError):
                return texts[0]
        return texts
    return content


# --- routes ------------------------------------------------------------------------------------

router = APIRouter(prefix="/api", tags=["intake"])


class IntakeBody(BaseModel):
    text: str = Field(min_length=3, max_length=500)


@router.post("/orders/intake")
async def intake_route(
    body: IntakeBody, request: Request, user: CurrentUser, store: StoreDep
) -> dict[str, Any]:
    """A sentence becomes an order through the model's intake (outside the contract)."""
    engine = request.app.state.engine
    if not isinstance(engine, AgentEngine):
        raise ApiError(503, "Intake needs the llm engine (--engine llm)", error="NoModel")
    if user.role != "CUSTOMER":
        raise ApiError(403, "Only customers order")
    return engine.intake(user, body.text)


def from_env() -> AgentEngine:
    """The engine the launcher and the bench build: a real model when there is a key."""
    return AgentEngine(None if os.environ.get("FOOOD_LLM", "").lower() != "off" else _no_model())


def _no_model() -> Any:
    return None
