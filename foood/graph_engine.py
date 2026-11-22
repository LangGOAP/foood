"""The LangGraph engine: the order lifecycle as a checkpointed StateGraph.

One thread per order. Action nodes change the platform through the same
domain functions the hand-wired engine calls; wait nodes ``interrupt`` until
the platform publishes the event they need, which the engine feeds back with
``Command(resume=...)``. A checkpointer keeps every thread, so a new engine
process resumes each order where it paused. ``recover`` is the fork every
failure routes to; in this step it applies the hand-wired reaction, the next
step puts a model there under controls, with this as the fallback.
"""

from __future__ import annotations

import json
import operator
import sqlite3
from collections import deque
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, Literal, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, StateSnapshot, interrupt

from foood.agent_engine import mcp_payload
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
from foood.engine import decide
from foood.errors import ApiError
from foood.llm import Model, tool_spec
from foood.money import dollars
from foood.orders import TERMINAL, Order, confirm_paid, fail_payment, get_order, transition
from foood.payments import (
    Payment,
    abandon,
    payment_for_order,
    refund_payment,
    retry_pending_payment,
    retry_with_backup,
)
from foood.recovery import (
    ALLOWED,
    CREDIT_DEFAULT,
    CREDIT_MAX,
    CREDIT_MIN,
    MAX_RECOVERIES,
    TERMINAL_FAILURES,
    Advisor,
    Context,
    Recovery,
    allowed_for,
)
from foood.replacements import (
    alternatives,
    answer_substitution,
    candidate_pool,
    offer_substitutes,
    replace,
    substitutes,
)
from foood.store import Store, iso, now
from foood.suggest import Classifier, Suggester
from foood.support import Question, ask
from foood.world import READ_TOOLS, WorldTools, zone_of
from foood.world_mcp import build_server, call_tool, tool_specs

CUSTOMER_DEADLINE = 180.0  # seconds a substitution offer stays open before it counts as declined
KITCHEN_FAILURES = {"kitchen_rejected", "kitchen_cancelled_mid_prep"}
LOOKUP_INSTRUCTIONS = (
    "You look things up for a food delivery platform's recovery step. You are given the failure "
    "and the facts gathered so far. Call the world tools only when another fact would change what "
    "to do; you decide nothing. Answer 'done' when there is nothing to add."
)

Failure = Literal[
    "card_declined",
    "payment_gateway_down",
    "kitchen_rejected",
    "kitchen_cancelled_mid_prep",
    "prep_overrun",
    "no_driver",
    "driver_dropped_before_pickup",
    "driver_dropped_after_pickup",
]


class OrderState(TypedDict, total=False):
    order_id: str
    entry: str  # the node a thread starts at: wait_payment, or wait_kitchen for a paid order
    event: dict[str, Any]  # the last platform event fed to a wait node
    failure: str  # the failure recover is handling
    # failures handled once: tick (prep_overrun, no_driver), gateway; nodes append, never replace
    handled: Annotated[list[str], operator.add]
    recoveries: int
    support_question: str  # the question recover is waiting on
    support_answer: str  # what support picked, for the next recover step
    world: dict[str, Any]  # what the lookup node gathered for recover


class Outcome(TypedDict, total=False):
    """What recover decided: where the thread goes next."""

    next: str  # a wait node, "wait_support", or "end"
    question: str  # the support question to wait on


Builder = StateGraph[OrderState, None, OrderState, OrderState]
Graph = CompiledStateGraph[OrderState, None, OrderState, OrderState]


# --- the graph --------------------------------------------------------------------------------


class LangGraphEngine:
    name = "langgraph"

    def __init__(
        self,
        checkpoint_path: Path | None = None,
        advisor: Advisor | None = None,
        max_recoveries: int = MAX_RECOVERIES,
        escalator: Advisor | None = None,
        looker: Model | None = None,
        classifier: Classifier | None = None,
        suggester: Suggester | None = None,
    ) -> None:
        self.checkpoint_path = checkpoint_path
        self.advisor = advisor
        self.escalator = escalator  # the frontier model, one call when decide's answer is unusable
        self.looker = looker  # a small tool-calling model that may look further at the world
        self.classifier = classifier  # a small model reading the kitchen's note
        self.suggester = suggester  # a mid model proposing substitutes from the pool
        self.max_recoveries = max_recoveries
        self.pending: deque[tuple[str, dict[str, Any]]] = deque()
        self.draining = False
        self._subscriptions: list[tuple[str, Callable[..., None]]] = []
        self._ticker: Callable[[], None] | None = None
        self.routes: dict[str, Callable[[OrderState], str]] = {}
        # the last snapshot of each thread: only this engine advances a thread, so a snapshot
        # holds until the engine invokes it again (a new process starts empty and reads)
        self._snapshots: dict[str, StateSnapshot] = {}

    # wiring ----------------------------------------------------------------------------

    def install(self, store: Store) -> None:
        self.store = store
        self.checkpointer: BaseCheckpointSaver[Any] = (
            SqliteSaver(sqlite3.connect(self.checkpoint_path, check_same_thread=False))
            if self.checkpoint_path
            else MemorySaver()
        )
        self.graph: Graph = build(self).compile(checkpointer=self.checkpointer)
        self.world_server = build_server(store)
        self.world_tools = [
            tool_spec(s["name"], s["description"], s["parameters"])
            for s in tool_specs(self.world_server, READ_TOOLS)
        ]
        self._subscribe("order.placed", lambda order: self.start(order.id, "wait_payment"))
        self._subscribe(
            "order.replaced",
            lambda original, replacement, reason: self.start(replacement.id, "wait_kitchen"),
        )
        self._subscribe("payment.processed", self._on_payment)
        self._subscribe(
            "order.substitution_answered",
            lambda order, offer, alternative: self.push(
                order.id,
                {"kind": "order.substitution_answered", "accepted": alternative is not None},
            ),
        )
        self._subscribe("payment.unavailable", self._on_unavailable)
        self._subscribe("payment.gateway_up", self._on_gateway_up)
        self._subscribe("order.status", self._on_order_status)
        self._subscribe("delivery.assigned", self._on_assigned)
        self._subscribe("delivery.status", self._on_delivery_status)
        self._subscribe("delivery.dropped", self._on_dropped)
        self._subscribe("support.answered", self._on_answered)
        # not an order's event: a driver coming online serves whichever delivery waits longest
        self._subscribe("driver.available", lambda driver: self.dispatch("a driver came online"))
        self._ticker = self.tick
        store.tickers.append(self._ticker)

    def uninstall(self) -> None:
        """Forget this engine's listeners (what a process exit does for free)."""
        for event, listener in self._subscriptions:
            self.store.listeners[event].remove(listener)
        self._subscriptions.clear()
        if self._ticker is not None:
            self.store.tickers.remove(self._ticker)
            self._ticker = None

    def _subscribe(self, event: str, listener: Callable[..., None]) -> None:
        self.store.subscribe(event, listener)
        self._subscriptions.append((event, listener))

    # events in ---------------------------------------------------------------------------

    def _on_payment(self, payment: Payment) -> None:
        self.push(payment.order_id, {"kind": "payment.processed", "status": payment.status})

    def _on_unavailable(self, payment: Payment) -> None:
        self.push(payment.order_id, {"kind": "payment.unavailable"})

    def _on_gateway_up(self) -> None:
        """The gateway is back: every order left waiting at its payment is charged now."""
        for order in list(self.store.orders.values()):
            if order.status in TERMINAL or self.waiting_at(order.id) != "wait_payment":
                continue
            retry_pending_payment(self.store, order.id)  # publishes payment.processed

    def _on_order_status(self, order: Order, previous: str) -> None:
        self.push(
            order.id,
            {
                "kind": "order.status",
                "status": order.status,
                "previous": previous,
                "cancelledBy": order.cancelled_by,
            },
        )

    def _on_assigned(self, delivery: Delivery, driver: Driver) -> None:
        self.push(delivery.order_id, {"kind": "delivery.assigned", "driverId": driver.id})

    def _on_delivery_status(self, delivery: Delivery) -> None:
        self.push(delivery.order_id, {"kind": "delivery.status", "status": delivery.status})

    def _on_dropped(
        self, delivery: Delivery, driver: Driver, reason: str | None, stage: str
    ) -> None:
        self.push(delivery.order_id, {"kind": "delivery.dropped", "stage": stage, "reason": reason})

    def _on_answered(self, question: Question) -> None:
        self.push(
            question.order_id,
            {"kind": "support.answered", "questionId": question.id, "answer": question.answer},
        )

    def tick(self) -> None:
        """Time passing matters only when a promise is breached; one tick event per order then.
        A substitution offer left open past its deadline counts as declined."""
        moment = now()
        for order_id, offer in list(self.store.offers.items()):
            if offer.status != "OPEN":
                continue
            if (moment - offer.offered_at).total_seconds() >= CUSTOMER_DEADLINE:
                order = self.store.orders.get(order_id)
                if order is not None:
                    answer_substitution(self.store, order, None, by="deadline")
        for order in list(self.store.orders.values()):
            if order.status in TERMINAL or not order.promise_breached:
                continue
            state = self.snapshot(order.id)
            if "tick" in state.values.get("handled", []):
                continue
            if state.next and state.next[0] in {"wait_kitchen", "wait_driver"}:
                # only the kitchen's and the search's waits act on time
                self.push(order.id, {"kind": "tick", "at": iso(now())})

    # threads -----------------------------------------------------------------------------

    def _config(self, order_id: str) -> RunnableConfig:
        return {"configurable": {"thread_id": order_id}}

    def snapshot(self, order_id: str) -> StateSnapshot:
        """The thread's checkpointed state, read once per change."""
        cached = self._snapshots.get(order_id)
        if cached is None:
            cached = self._snapshots[order_id] = self.graph.get_state(self._config(order_id))
        return cached

    def _invoke(self, order_id: str, graph_input: Any) -> None:
        self._snapshots.pop(order_id, None)
        self.graph.invoke(graph_input, self._config(order_id))

    def start(self, order_id: str, entry: str) -> None:
        self.pending.append((order_id, {"kind": "start", "entry": entry}))
        self.drain()

    def push(self, order_id: str, event: dict[str, Any]) -> None:
        self.pending.append((order_id, event))
        self.drain()

    def drain(self) -> None:
        """Feed events one at a time; events raised while a node runs wait their turn."""
        if self.draining:
            return
        self.draining = True
        try:
            while self.pending:
                order_id, event = self.pending.popleft()
                if event["kind"] == "start":
                    self._invoke(
                        order_id,
                        {"order_id": order_id, "entry": event["entry"], "recoveries": 0},
                    )
                    continue
                paused = self.snapshot(order_id)
                if not paused.next:
                    continue  # finished, or not an order this engine runs
                wait = paused.next[0]
                route = self.routes.get(wait)
                asked = OrderState(**paused.values)  # type: ignore[typeddict-item]
                asked["event"] = event
                if route is not None and route(asked) == wait:
                    continue  # the wait would only loop back: nothing to resume
                self._invoke(order_id, Command(resume=event))
        finally:
            self.draining = False

    def waiting_at(self, order_id: str) -> str | None:
        """The wait node an order's thread is paused at, or None when it is over."""
        state = self.snapshot(order_id)
        return state.next[0] if state.next else None

    # recovery ------------------------------------------------------------------------------

    def lookup(self, order: Order, failure: str) -> dict[str, Any]:
        """The lookup node: the world's facts for the fork, gathered in code through the same
        tools a model would call, plus whatever a small tool-calling model asks for on top.
        The decision node then sees facts and no tools."""
        store = self.store
        if self.advisor is None:  # no model at the fork: nothing reads the facts, so none are read
            return {}
        tools = WorldTools(store, order.id)
        kitchen = store.restaurants[order.restaurant_id]
        zone = zone_of(kitchen.latitude, kitchen.longitude)
        world: dict[str, Any] = {
            "zone": zone,
            "weather": tools.get_weather(zone)["kind"],
            "forecast": tools.forecast(zone, 30),
            "lookups": [],
        }
        if order.delivery_latitude is not None and order.delivery_longitude is not None:
            start = (kitchen.latitude, kitchen.longitude)
            door = (order.delivery_latitude, order.delivery_longitude)
            traffic = tools.get_traffic(start, door)
            world.update(
                {
                    "rideMinutes": traffic["minutes"],
                    "clearRideMinutes": traffic["baseMinutes"],
                    "delayFactor": traffic["delayFactor"],
                    "closures": traffic["closures"],
                    "promisedAt": iso(order.promised_at) if order.promised_at else None,
                    "now": iso(now()),
                }
            )
        else:
            world.update({"closures": []})
        if self.looker is not None:
            self.look_further(order, failure, world)
        return world

    def look_further(self, order: Order, failure: str, world: dict[str, Any]) -> None:
        """Up to two rounds of a small model choosing world tools; results join the facts."""
        prompt = (
            f"Failure: {failure}. Facts so far: {json.dumps(world, default=str)}. "
            "If another look at the world would change the recovery, call the tools for it; "
            "otherwise answer 'done' in one word."
        )
        input_items: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
        previous = None
        counted = WorldTools(self.store, order.id)
        for _ in range(2):
            try:
                turn = self.looker.complete(  # type: ignore[union-attr]
                    LOOKUP_INSTRUCTIONS, input_items, self.world_tools, previous=previous
                )
            except Exception as exc:  # noqa: BLE001  # a slow or broken lookup model is a note
                decide(
                    self.store,
                    order.id,
                    "lookup error",
                    because=str(exc)[:200],
                    llmRole="perception",
                    node="lookup",
                    llmCalls=1,
                )
                return
            names = [c.name for c in turn.tool_calls]
            decide(
                self.store,
                order.id,
                "lookup: " + (", ".join(names) if names else "done"),
                because=(turn.text or "")[:200],
                llmRole="perception",
                node="lookup",
                **turn.usage.record(),
            )
            if not turn.tool_calls:
                return
            outputs = []
            for call in turn.tool_calls:
                if call.name in READ_TOOLS:
                    counted._count(call.name)
                    try:
                        result = mcp_payload(
                            call_tool(self.world_server, call.name, call.arguments)
                        )
                    except Exception as exc:  # noqa: BLE001
                        result = {"error": str(exc)[:200]}
                else:
                    result = {"error": f"no tool named {call.name}"}
                world["lookups"].append(
                    {"tool": call.name, "arguments": call.arguments, "result": result}
                )
                outputs.append(
                    {
                        "type": "function_call_output",
                        "call_id": call.id,
                        "output": json.dumps(result, default=str)[:4000],
                    }
                )
            input_items = outputs
            previous = turn.response_id

    def classify(self, order: Order, failure: str, world: dict[str, Any]) -> dict[str, Any]:
        """The classify node: the kitchen's free-text note read into a kind, by a small model."""
        note = order.cancellation_note or ""
        if self.classifier is None or failure not in KITCHEN_FAILURES or not note.strip():
            return world
        items = [i.name for i in order.items]
        try:
            reason, usage = self.classifier.classify(note, items)
        except Exception as exc:  # noqa: BLE001  # the note stays unread; the fork still runs
            decide(
                self.store,
                order.id,
                "classify error",
                because=str(exc)[:200],
                llmRole="perception",
                node="classify",
                llmCalls=1,
            )
            return world
        decide(
            self.store,
            order.id,
            f"classify: {reason.kind}",
            because=note[:200],
            llmRole="perception",
            node="classify",
            **usage.record(),
        )
        return {**world, "reason": reason.model_dump()}

    def missing_items(self, order: Order) -> list[str]:
        """The order's lines the kitchen no longer has on its menu."""
        menu = {
            i.name.casefold()
            for i in self.store.menu_items.values()
            if i.restaurant_id == order.restaurant_id and i.available and not i.archived
        }
        return [line.name for line in order.items if line.name.casefold() not in menu]

    def wants_substitutes(self, order: Order, world: dict[str, Any]) -> bool:
        reason = world.get("reason") or {}
        if self.suggester is None or reason.get("kind") != "item_out":
            return False
        open_offer = self.store.offers.get(order.id)
        if open_offer is not None:  # asked once; the answer went back to the fork already
            return False
        return bool(self.missing_items(order))

    def suggest(self, order: Order, world: dict[str, Any]) -> bool:
        """The suggest node: the model proposes from the pool, the platform grounds and prices,
        the customer is asked. True when an offer went out."""
        assert self.suggester is not None
        store = self.store
        missing = self.missing_items(order)
        pool = candidate_pool(store, order)
        try:
            suggestions, usage = self.suggester.suggest(
                [i.name for i in order.items], missing, pool
            )
        except Exception as exc:  # noqa: BLE001
            decide(
                store,
                order.id,
                "suggest error",
                because=str(exc)[:200],
                llmRole="perception",
                node="suggest",
                llmCalls=1,
            )
            return False
        names = [str(c) for c in suggestions.candidates][:5]
        decide(
            store,
            order.id,
            "suggest: " + (", ".join(names) if names else "nothing"),
            because=suggestions.note[:200],
            llmRole="perception",
            node="suggest",
            **usage.record(),
        )
        options = substitutes(store, order, names)
        if not options:
            decide(store, order.id, "no substitute grounded", because="nothing on an open menu")
            return False
        reason = order.cancellation_note or f"{', '.join(missing)} ran out"
        offer = offer_substitutes(store, order, options[:4], reason)
        decide(
            store,
            order.id,
            "offer substitutes",
            because=", ".join(
                f"{o.restaurant.name}: {', '.join(i.name for _, i in o.items)}"
                for o in offer.options
            ),
        )
        return True

    def apply_substitution(self, order: Order) -> None:
        offer = self.store.offers.get(order.id)
        if offer is None or offer.chosen is None:
            return
        replace(self.store, order, offer.chosen, offer.reason)
        decide(
            self.store,
            order.id,
            "replace order",
            because=f"{offer.chosen.restaurant.name} makes the accepted substitute",
            replacementOrderId=order.replaced_by_order_id,
        )

    def recover(self, order: Order, failure: str, state: OrderState) -> Outcome:
        """The fork. A model picks a tool inside the allow-list and the budget; otherwise, or
        when it errs, the hand-wired reaction applies."""
        attempt = state.get("recoveries", 0) + 1
        answer = state.get("support_answer")
        if answer:  # a person already picked the tool
            decide(self.store, order.id, f"support: {answer}", because="support's choice")
            return self.apply(
                order, failure, Recovery(tool=answer, reasoning="support's choice"), attempt
            )
        if self.advisor is None:
            return self.hand_wired_reaction(order, failure)
        if attempt > self.max_recoveries:
            decide(self.store, order.id, "fallback", because="recovery budget exhausted")
            return self.hand_wired_reaction(order, failure)
        context = self.context(order, failure, attempt, state.get("world") or {})
        recovery = self.consult(
            order, failure, context, self.advisor, "decide", final=self.escalator is None
        )
        if recovery is None and self.escalator is not None:
            recovery = self.consult(order, failure, context, self.escalator, "escalate")
        if recovery is None:
            return self.hand_wired_reaction(order, failure)
        try:
            return self.apply(order, failure, recovery, attempt)
        except ApiError as refused:  # the platform said no: the hand-wired reaction still applies
            decide(
                self.store,
                order.id,
                "fallback",
                because=f"{recovery.tool} refused: {refused.detail}",
            )
            return self.hand_wired_reaction(order, failure)

    def consult(
        self,
        order: Order,
        failure: str,
        context: Context,
        advisor: Advisor,
        node: str,
        final: bool = True,
    ) -> Recovery | None:
        """One node's answer, recorded; None when the model erred or picked outside the list.
        An unusable answer is a fallback when nothing follows, an escalation when a stronger
        node is consulted next."""
        unusable = "fallback" if final else "escalated"
        try:
            recovery, usage = advisor.advise(context)
        except Exception as exc:  # noqa: BLE001  # whatever the model or the network did
            decide(
                self.store,
                order.id,
                unusable,
                because=f"model error: {exc}",
                llmRole="decision",
                node=node,
                llmCalls=1,
            )
            return None
        if recovery.tool not in context.allowed:
            decide(
                self.store,
                order.id,
                unusable,
                because=f"model picked {recovery.tool}, not allowed for {failure}",
                llmRole="decision",
                node=node,
                **usage.record(),
            )
            return None
        decide(
            self.store,
            order.id,
            f"{'llm' if node == 'decide' else node}: {recovery.tool}",
            because=recovery.reasoning,
            llmRole="decision",
            node=node,
            **usage.record(),
        )
        return recovery

    def context(
        self, order: Order, failure: str, attempt: int, world: dict[str, Any] | None = None
    ) -> Context:
        store = self.store
        kitchen = store.restaurants[order.restaurant_id]
        customer = store.users[order.customer_id]
        delivery = delivery_for_order(store, order.id)
        options = alternatives(store, order)
        facts = dict(world or {})
        latitude, longitude = order.delivery_latitude, order.delivery_longitude
        if facts.get("closures") is not None and latitude is not None and longitude is not None:
            tools = WorldTools(store, order.id)
            door = (latitude, longitude)
            options = [  # a kitchen behind a closed corridor is no alternative tonight
                a
                for a in options
                if not tools.get_traffic((a.restaurant.latitude, a.restaurant.longitude), door)[
                    "closures"
                ]
            ]
        facts["alternativesReachable"] = len(options)
        return Context(
            failure=failure,
            attempt=attempt,
            budget=self.max_recoveries,
            allowed=allowed_for(failure, facts),
            world=facts,
            order={
                "id": order.id[:8],
                "status": order.status,
                "items": [f"{i.quantity}x {i.name}" for i in order.items],
                "total": str(order.total),
                "promisedAt": iso(order.promised_at) if order.promised_at else None,
                "promiseBreached": order.promise_breached,
                "cancellationNote": order.cancellation_note,
            },
            kitchen={
                "name": kitchen.name,
                "status": kitchen.status,
                "prepMinutes": kitchen.prep_minutes,
            },
            customer={
                "cardsOnFile": len(customer.cards),
                "creditBalance": str(balance(store, customer.id)),
                "substitutionPolicy": customer.substitution_policy,
            },
            delivery=(
                {"status": delivery.status, "searchSeconds": delivery.to_json()["searchSeconds"]}
                if delivery
                else None
            ),
            alternatives=[a.to_json(order, customer) for a in options[:3]],
            drivers_online=len(available_drivers(store)),
            notes=(
                [
                    "The payment gateway is unavailable; nothing has been charged. "
                    "Waiting retries the payment as soon as the gateway is back."
                ]
                if failure == "payment_gateway_down"
                else []
            ),
        )

    def apply(self, order: Order, failure: str, recovery: Recovery, attempt: int) -> Outcome:
        """Run the chosen tool. Every branch ends in a consistent state or hands over to the
        hand-wired reaction, so a poor choice costs money or time, never correctness."""
        store = self.store
        tool = recovery.tool
        if tool == "replace_order":
            options = alternatives(store, order)
            if not options:
                decide(store, order.id, "fallback", because="no other kitchen can make it")
                return self.hand_wired_reaction(order, failure)
            choice = options[0]
            customer = store.users[order.customer_id]
            if choice.needs_approval(order, customer) and attempt == 1:
                difference = choice.difference(order)
                question = ask(
                    store,
                    order.id,
                    "substitution",
                    f"{choice.restaurant.name} can make this for "
                    f"{dollars(difference, signed=True)}. Go ahead?",
                    options=[
                        {"value": "replace_order", "label": f"Yes, {choice.restaurant.name}"},
                        {"value": "refund_and_cancel", "label": "No, refund"},
                    ],
                    default="refund_and_cancel",
                    deadline_seconds=120,
                    restaurant=choice.restaurant.name,
                    difference=str(difference),
                )
                decide(store, order.id, "ask support", because="the substitution needs approval")
                return {"next": "wait_support", "question": question.id}
            replace(store, order, choice, order.cancellation_note or failure.replace("_", " "))
            decide(
                store,
                order.id,
                "replace order",
                because=f"{choice.restaurant.name} makes it instead",
                replacementOrderId=order.replaced_by_order_id,
            )
            return {"next": "end"}
        if tool == "retry_backup_card":
            payment = payment_for_order(store, order.id)
            try:
                if payment is None:
                    raise ApiError(409, "nothing to retry")
                retry_with_backup(store, payment)
            except ApiError as refused:
                decide(store, order.id, "fallback", because=str(refused.detail))
                return self.hand_wired_reaction(order, failure)
            return {"next": "wait_payment"}
        if tool == "issue_credit":
            amount = Decimal(
                str(min(CREDIT_MAX, max(CREDIT_MIN, recovery.amount or CREDIT_DEFAULT)))
            )
            try:
                issue_credit(store, order.customer_id, amount, failure.replace("_", " "), order.id)
            except ApiError:
                pass  # already compensated: the credit is not doubled
            return self.hand_wired_reaction(order, failure)
        if tool == "widen_dispatch":
            use(store, stack_on_finishing)
            decide(
                store,
                order.id,
                "widen dispatch",
                because="busy drivers finishing nearby may take it",
            )
            self.dispatch("the search was widened")
            return {"next": "wait_driver"}
        if tool == "wait":
            if failure == "driver_dropped_before_pickup":
                return self.hand_wired_reaction(order, failure)
            decide(store, order.id, "keep waiting", because=recovery.reasoning)
            return {"next": self.origin(failure)}
        if tool == "ask_support":
            allowed = [t for t in ALLOWED[failure] if t != "ask_support"]
            choices = [o for o in (recovery.options or allowed) if o in allowed] or allowed
            default = "refund_and_cancel" if failure in TERMINAL_FAILURES else "wait"
            if default not in choices:
                default = choices[0]
            question = ask(
                store,
                order.id,
                failure,
                recovery.question
                or f"What should we do about this order ({failure.replace('_', ' ')})?",
                options=[{"value": o, "label": o.replace("_", " ")} for o in choices],
                default=default,
                deadline_seconds=120,
            )
            decide(store, order.id, "ask support", because=recovery.reasoning)
            return {"next": "wait_support", "question": question.id}
        if tool == "refund_and_cancel":
            return self.cancel_and_refund(order, failure)
        raise ValueError(f"unknown tool {tool!r}")

    @staticmethod
    def origin(failure: str) -> str:
        """The wait node a non-terminal failure returns to."""
        return {
            "card_declined": "wait_payment",
            "payment_gateway_down": "wait_payment",
            "kitchen_rejected": "wait_kitchen",
            "kitchen_cancelled_mid_prep": "wait_kitchen",
            "prep_overrun": "wait_kitchen",
            "no_driver": "wait_driver",
            "driver_dropped_before_pickup": "wait_driver",
            "driver_dropped_after_pickup": "wait_dropoff",
        }[failure]

    def cancel_and_refund(self, order: Order, failure: str) -> Outcome:
        store = self.store
        if failure == "payment_gateway_down":  # nothing was charged: there is nothing to refund
            return self.hand_wired_reaction(order, failure)
        if order.status not in TERMINAL:
            decide(store, order.id, "cancel", because=failure.replace("_", " "))
            delivery = delivery_for_order(store, order.id)
            if delivery is not None and delivery.status not in {"DELIVERED", "CANCELLED"}:
                cancel_delivery(store, delivery, failure.replace("_", " "), "SYSTEM")
            if order.status not in TERMINAL:
                reason = (
                    "SLA_BREACH"
                    if failure in {"prep_overrun", "no_driver"}
                    else "DELIVERY_CANCELLED"
                )
                transition(
                    store,
                    order,
                    "CANCELLED",
                    cancelled_by="SYSTEM",
                    cancellation_reason=reason,
                    cancellation_note=failure.replace("_", " "),
                )
        if failure == "card_declined":
            return self.hand_wired_reaction(order, failure)
        self.refund(order, failure.replace("_", " "))
        return {"next": "end"}

    def hand_wired_reaction(self, order: Order, failure: str) -> Outcome:
        store = self.store
        if failure == "card_declined":
            decide(store, order.id, "cancel", because="payment failed")
            fail_payment(store, order.id, "PAYMENT_FAILED")
            return {"next": "end"}
        if failure == "payment_gateway_down":
            decide(store, order.id, "cancel", because="payment gateway unavailable")
            pending = payment_for_order(store, order.id)
            if pending is not None and pending.status == "PENDING":
                abandon(store, pending, "Payment gateway unavailable")
            fail_payment(store, order.id, "PAYMENT_GATEWAY_UNAVAILABLE")
            return {"next": "end"}
        if failure in {"kitchen_rejected", "kitchen_cancelled_mid_prep"}:
            self.refund(order, f"cancelled by {order.cancelled_by}")
            return {"next": "end"}
        if failure == "prep_overrun":
            decide(store, order.id, "keep waiting", because="the kitchen is late")
            return {"next": "wait_kitchen"}
        if failure == "no_driver":
            decide(store, order.id, "keep waiting", because="no driver yet")
            return {"next": "wait_driver"}
        if failure == "driver_dropped_before_pickup":
            decide(store, order.id, "dispatch again", because="the driver dropped the job")
            self.dispatch("a driver dropped the job before pickup")
            return {"next": "wait_driver"}
        if failure == "driver_dropped_after_pickup":
            decide(store, order.id, "cancel", because="the driver dropped the job after pickup")
            if order.status not in TERMINAL:
                transition(
                    store,
                    order,
                    "CANCELLED",
                    cancelled_by="SYSTEM",
                    cancellation_reason="DELIVERY_CANCELLED",
                    cancellation_note="The driver dropped the job",
                )
            self.refund(order, "cancelled after pickup")
            return {"next": "end"}
        raise ValueError(f"unknown failure {failure!r}")

    def refund(self, order: Order, because: str) -> None:
        payment = payment_for_order(self.store, order.id)
        if payment is not None and payment.status == "COMPLETED":
            decide(self.store, order.id, "refund", because=because)
            refund_payment(self.store, payment, "ORDER_CANCELLED")

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


# --- nodes and edges ---------------------------------------------------------------------------


def build(engine: LangGraphEngine) -> Builder:
    store = engine.store

    def order_of(state: OrderState) -> Order:
        return get_order(store, state["order_id"])

    # action nodes: side effects, no interrupts
    def begin(state: OrderState) -> OrderState:
        return {}

    def confirm(state: OrderState) -> OrderState:
        decide(store, state["order_id"], "confirm", because="payment completed")
        confirm_paid(store, state["order_id"])
        return {}

    def dispatch(state: OrderState) -> OrderState:
        order = order_of(state)
        if delivery_for_order(store, order.id) is None:
            decide(store, order.id, "create delivery", because="the kitchen is ready")
            create_delivery(store, order)
        engine.dispatch("a delivery was created")
        return {}

    def lookup(state: OrderState) -> OrderState:
        return {"world": engine.lookup(order_of(state), state["failure"])}

    def classify(state: OrderState) -> OrderState:
        return {
            "world": engine.classify(order_of(state), state["failure"], state.get("world") or {})
        }

    def suggest(state: OrderState) -> OrderState:
        offered = engine.suggest(order_of(state), state.get("world") or {})
        return {"event": {"kind": "suggested", "offered": offered}}

    def declined(state: OrderState) -> OrderState:
        return {"world": {**(state.get("world") or {}), "substitutesDeclined": True}}

    def apply_substitution(state: OrderState) -> OrderState:
        engine.apply_substitution(order_of(state))
        return {}

    def after_classify(state: OrderState) -> str:
        return (
            "suggest"
            if engine.wants_substitutes(order_of(state), state.get("world") or {})
            else "recover"
        )

    def after_suggest(state: OrderState) -> str:
        return "wait_customer" if state["event"].get("offered") else "recover"

    def after_customer(state: OrderState) -> str:
        event = state["event"]
        if event["kind"] == "order.substitution_answered":
            return "apply_substitution" if event.get("accepted") else "declined"
        if event["kind"] == "order.status" and event["status"] in {"CANCELLED", "FAILED"}:
            return "cleanup"
        return "wait_customer"

    def recover(state: OrderState) -> OrderState:
        outcome = engine.recover(order_of(state), state["failure"], state)
        seen = state.get("handled", [])
        handled: list[str] = []
        if state["failure"] in {"prep_overrun", "no_driver"} and "tick" not in seen:
            handled.append("tick")
        if (
            state["failure"] == "payment_gateway_down"
            and outcome["next"] == "wait_payment"
            and "gateway" not in seen
        ):
            handled.append("gateway")  # one decision per outage: later refused taps are not news
        return {
            "event": {"kind": "recovered", "next": outcome["next"]},
            "handled": handled,
            "recoveries": state.get("recoveries", 0) + 1,
            "support_question": outcome.get("question", ""),
            "support_answer": "",
        }

    def cleanup(state: OrderState) -> OrderState:
        """The order was cancelled by someone else (customer, support): tidy up."""
        order = order_of(state)
        engine.refund(order, f"cancelled by {order.cancelled_by}")
        delivery = delivery_for_order(store, order.id)
        if delivery is not None and delivery.status not in {"DELIVERED", "CANCELLED"}:
            decide(store, order.id, "cancel delivery", because="the order was cancelled")
            cancel_delivery(
                store, delivery, order.cancellation_note, order.cancelled_by or "SYSTEM"
            )
        return {}

    # wait nodes: one interrupt each, the edge decides
    def waiter(name: str) -> Callable[[OrderState], OrderState]:
        def wait(state: OrderState) -> OrderState:
            event = interrupt({"waiting": name, "orderId": state["order_id"]})
            return {"event": event}

        wait.__name__ = name
        return wait

    def after_start(state: OrderState) -> str:
        return state.get("entry", "wait_payment")

    def after_payment(state: OrderState) -> str:
        event = state["event"]
        if event["kind"] == "payment.processed" and event["status"] == "COMPLETED":
            return "confirm"
        if event["kind"] == "payment.processed" and event["status"] == "FAILED":
            return "failure_card_declined"
        if event["kind"] == "payment.unavailable":
            if "gateway" in state.get("handled", []):
                return "wait_payment"  # already waiting for the gateway to come back
            return "failure_payment_gateway_down"
        if event["kind"] == "order.status" and event["status"] in {"CANCELLED", "FAILED"}:
            return "cleanup"
        return "wait_payment"

    def after_kitchen(state: OrderState) -> str:
        event = state["event"]
        if event["kind"] == "order.status":
            if event["status"] == "READY_FOR_PICKUP":
                return "dispatch"
            if event["status"] == "CANCELLED" and event.get("cancelledBy") == "RESTAURANT":
                return (
                    "failure_kitchen_cancelled_mid_prep"
                    if event.get("previous") == "PREPARING"
                    else "failure_kitchen_rejected"
                )
            if event["status"] in {"CANCELLED", "FAILED"}:
                return "cleanup"
        if event["kind"] == "tick":
            return "failure_prep_overrun"
        return "wait_kitchen"

    def after_driver(state: OrderState) -> str:
        event = state["event"]
        if event["kind"] == "delivery.assigned":
            return "wait_pickup"
        if event["kind"] == "order.status" and event["status"] in {"CANCELLED", "FAILED"}:
            return "cleanup"
        if event["kind"] == "tick":
            return "failure_no_driver"
        return "wait_driver"

    def after_pickup(state: OrderState) -> str:
        event = state["event"]
        if event["kind"] == "delivery.status" and event["status"] == "PICKED_UP":
            return "wait_dropoff"
        if event["kind"] == "delivery.dropped":
            return "failure_driver_dropped_before_pickup"
        if event["kind"] == "order.status" and event["status"] in {"CANCELLED", "FAILED"}:
            return "cleanup"
        return "wait_pickup"

    def after_dropoff(state: OrderState) -> str:
        event = state["event"]
        if event["kind"] == "delivery.status" and event["status"] == "DELIVERED":
            return END
        if event["kind"] == "delivery.dropped":
            return "failure_driver_dropped_after_pickup"
        if event["kind"] == "order.status" and event["status"] in {"CANCELLED", "FAILED"}:
            return "cleanup"
        return "wait_dropoff"

    def after_recover(state: OrderState) -> str:
        target = state["event"]["next"]
        return END if target == "end" else target

    def after_support(state: OrderState) -> str:
        event = state["event"]
        if event["kind"] == "support.answered" and event.get("questionId") == state.get(
            "support_question"
        ):
            return "answered"
        if event["kind"] == "order.status" and event["status"] in {"CANCELLED", "FAILED"}:
            return "cleanup"
        return "wait_support"

    def answered(state: OrderState) -> OrderState:
        return {"support_answer": str(state["event"].get("answer") or "")}

    def set_failure(failure: str) -> Callable[[OrderState], OrderState]:
        def mark(state: OrderState) -> OrderState:
            return {"failure": failure}

        mark.__name__ = f"failure_{failure}"
        return mark

    # the waits' routing, also asked by the engine before it resumes a thread: an event the
    # wait would only loop back on is not fed in at all (no super-step, no checkpoint)
    engine.routes = {
        "wait_payment": after_payment,
        "wait_kitchen": after_kitchen,
        "wait_driver": after_driver,
        "wait_pickup": after_pickup,
        "wait_dropoff": after_dropoff,
        "wait_support": after_support,
        "wait_customer": after_customer,
    }

    graph: Builder = StateGraph(OrderState)
    graph.add_node("begin", begin)
    graph.add_node("confirm", confirm)
    graph.add_node("dispatch", dispatch)
    graph.add_node("lookup", lookup)
    graph.add_node("classify", classify)
    graph.add_node("suggest", suggest)
    graph.add_node("declined", declined)
    graph.add_node("apply_substitution", apply_substitution)
    graph.add_node("recover", recover)
    graph.add_node("cleanup", cleanup)
    graph.add_node("answered", answered)
    waits = [
        "wait_payment",
        "wait_kitchen",
        "wait_driver",
        "wait_pickup",
        "wait_dropoff",
        "wait_support",
        "wait_customer",
    ]
    for name in waits:
        graph.add_node(name, waiter(name))  # type: ignore[arg-type]  # node protocol vs Callable
    failures = [
        "card_declined",
        "payment_gateway_down",
        "kitchen_rejected",
        "kitchen_cancelled_mid_prep",
        "prep_overrun",
        "no_driver",
        "driver_dropped_before_pickup",
        "driver_dropped_after_pickup",
    ]
    for failure in failures:  # a tiny node per failure keeps the drawing honest about the forks
        graph.add_node(f"failure_{failure}", set_failure(failure))  # type: ignore[arg-type]
        graph.add_edge(f"failure_{failure}", "lookup")  # look up, classify, then decide
    graph.add_edge("lookup", "classify")
    graph.add_conditional_edges("classify", after_classify, ["suggest", "recover"])
    graph.add_conditional_edges("suggest", after_suggest, ["wait_customer", "recover"])
    graph.add_conditional_edges(
        "wait_customer",
        after_customer,
        ["apply_substitution", "declined", "cleanup", "wait_customer"],
    )
    graph.add_edge("declined", "recover")
    graph.add_edge("apply_substitution", END)

    graph.add_edge(START, "begin")
    graph.add_conditional_edges("begin", after_start, ["wait_payment", "wait_kitchen"])
    graph.add_conditional_edges(
        "wait_payment",
        after_payment,
        [
            "confirm",
            "failure_card_declined",
            "failure_payment_gateway_down",
            "cleanup",
            "wait_payment",
        ],
    )
    graph.add_edge("confirm", "wait_kitchen")
    graph.add_conditional_edges(
        "wait_kitchen",
        after_kitchen,
        [
            "dispatch",
            "failure_kitchen_rejected",
            "failure_kitchen_cancelled_mid_prep",
            "failure_prep_overrun",
            "cleanup",
            "wait_kitchen",
        ],
    )
    graph.add_edge("dispatch", "wait_driver")
    graph.add_conditional_edges(
        "wait_driver", after_driver, ["wait_pickup", "failure_no_driver", "cleanup", "wait_driver"]
    )
    graph.add_conditional_edges(
        "wait_pickup",
        after_pickup,
        ["wait_dropoff", "failure_driver_dropped_before_pickup", "cleanup", "wait_pickup"],
    )
    graph.add_conditional_edges(
        "wait_dropoff",
        after_dropoff,
        [END, "failure_driver_dropped_after_pickup", "cleanup", "wait_dropoff"],
    )
    graph.add_conditional_edges(
        "recover",
        after_recover,
        [
            "wait_payment",
            "wait_kitchen",
            "wait_driver",
            "wait_pickup",
            "wait_dropoff",
            "wait_support",
            END,
        ],
    )
    graph.add_conditional_edges(
        "wait_support", after_support, ["answered", "cleanup", "wait_support"]
    )
    graph.add_edge("answered", "recover")
    graph.add_edge("cleanup", END)
    return graph


def mermaid() -> str:
    """The drawing of the lifecycle, for the README and the article."""
    engine = LangGraphEngine()
    engine.store = Store()
    return build(engine).compile().get_graph().draw_mermaid()
