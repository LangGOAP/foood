"""The LangGOAP engine: the order's lifecycle as a goal, the platform's tools as actions.

One GOAP thread per customer order (it follows a replacement order to the
door). A sensor derives the world state from the platform before every plan;
actions change the platform through the same domain functions the other
engines call; the waits (for the payment, the kitchen, a driver, the pickup,
the drop-off) are actions that ``interrupt()`` until the platform publishes an
event, then return the truth they observe. When the truth is not what the plan
expected, the planner re-plans from it. Nothing here asks a model: the
alternatives are costed, and A* picks. Only the substitution approval asks a
person, through support, as a typed form.
"""

from __future__ import annotations

import sqlite3
import time
from collections import deque
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgoap import ActionSpec, FunctionalSensor, GoalSpec, GoapGraph
from langgoap.termination import MaxActionsPolicy
from langgoap.tracing.null import NullTracer
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command, interrupt
from pydantic import BaseModel, Field

from foood.credits import issue_credit
from foood.deliveries import (
    ACTIVE,
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
from foood.money import dollars
from foood.orders import TERMINAL, Order, confirm_paid, get_order, transition
from foood.payments import (
    Payment,
    gateway_is_down,
    payment_for_order,
    refund_payment,
    retry_pending_payment,
    retry_with_backup,
)
from foood.replacements import Alternative, alternatives, replace
from foood.store import Store, iso, now
from foood.support import Question, ask
from foood.world import WorldTools, zone_of

WAITS = {
    "await_payment",
    "await_gateway",
    "await_kitchen",
    "await_driver",
    "await_pickup",
    "await_dropoff",
}
TIMED_WAITS = {"await_kitchen", "await_driver"}
CREDIT_FOR_LATENESS = Decimal("5.00")


class SubstitutionApproval(BaseModel):
    """Support's answer to 'another kitchen can make it for a different price'."""

    approved: bool = Field(description="go ahead with the other kitchen")
    note: str = Field(default="", description="a word for the record")


class Trace(NullTracer):
    """Plans, re-plans and the actions that ran, written to the order's decisions."""

    def __init__(self, store: Store, current_order: Callable[[str], Order] | None = None) -> None:
        self.store = store
        self.order_id = ""
        self.last_steps: dict[str, list[str]] = {}
        self.current_order = current_order
        self.round_started: float | None = None

    def considered(self) -> list[dict[str, Any]]:
        """The other kitchens on the table when this plan was made, with what they would cost."""
        if self.current_order is None:
            return []
        try:
            order = self.current_order(self.order_id)
        except ApiError:
            return []
        customer = self.store.users[order.customer_id]
        return [
            {
                "kitchen": a.restaurant.name,
                "difference": str(a.difference(order)),
                "needsApproval": a.needs_approval(order, customer),
            }
            for a in alternatives(self.store, order)[:3]
        ]

    def on_plan_start(self, goal: Any, state: Any, strategy_name: str) -> None:
        self.round_started = time.perf_counter()

    def round_ms(self, reported: float | None = None) -> float:
        """Wall-clock of the planning round that just ended, from on_plan_start to this hook.

        The library reports a duration for first plans only; timing every round the same way
        keeps plans, re-plans and the rounds where the world ran ahead comparable.
        """
        if self.round_started is None:
            return round(reported or 0.0, 1)
        elapsed = (time.perf_counter() - self.round_started) * 1000
        self.round_started = None
        return round(elapsed, 1)

    def on_plan_complete(self, plan: Any, duration_ms: float) -> None:
        steps = [a.name for a in plan.actions]
        self.last_steps[self.order_id] = steps
        decide(
            self.store,
            self.order_id,
            "plan",
            because="A* over the platform's actions",
            steps=steps,
            planCost=round(float(plan.total_cost), 2),
            planningMs=self.round_ms(duration_ms),
            considered=self.considered(),
        )

    def on_replan(self, reason: str, new_plan: Any) -> None:
        steps = [a.name for a in new_plan.actions] if new_plan is not None else []
        previous = self.last_steps.get(self.order_id, [])
        self.last_steps[self.order_id] = steps
        # the world ran ahead of the plan (a driver was already there): same route, fewer steps
        skipped = bool(steps) and len(steps) < len(previous) and previous[-len(steps) :] == steps
        decide(
            self.store,
            self.order_id,
            "plan skips ahead" if skipped else "replan",
            because=reason,
            planningMs=self.round_ms(),
            steps=steps,
            planCost=round(float(new_plan.total_cost), 2) if new_plan is not None else None,
            considered=self.considered() if not skipped else [],
        )

    def on_plan_failed(self, reason: str, duration_ms: float) -> None:
        decide(
            self.store,
            self.order_id,
            "no plan",
            because=reason,
            planningMs=self.round_ms(duration_ms),
        )

    def on_action_complete(self, result: Any) -> None:
        """The executor's update: the last entry of its execution history is the action."""
        history = result.get("execution_history") if isinstance(result, dict) else None
        last = history[-1] if history else None
        if last is None:
            return
        name = last["action_name"] if isinstance(last, dict) else last.action_name
        ok = last["success"] if isinstance(last, dict) else last.success
        if name in WAITS:
            return
        decide(self.store, self.order_id, f"act: {name}", because="planned", succeeded=bool(ok))


class GoapEngine:
    name = "langgoap"

    def __init__(self, checkpoint_path: Path | None = None, max_actions: int = 60) -> None:
        self.checkpoint_path = checkpoint_path
        self.max_actions = max_actions
        self.pending: deque[tuple[str, dict[str, Any]]] = deque()
        self._world: dict[str, tuple[tuple[str, int], dict[str, Any]]] = {}
        self.draining = False
        self.threads: dict[str, str] = {}  # any order id -> its thread (the first order's id)
        self.questions: dict[str, str] = {}  # thread -> open support question id
        self._subscriptions: list[tuple[str, Callable[..., None]]] = []
        self._ticker: Callable[[], None] | None = None

    # wiring ----------------------------------------------------------------------------

    def install(self, store: Store) -> None:
        self.store = store
        self.tracer = Trace(store, self.current_order)
        self.checkpointer: BaseCheckpointSaver[Any] = (
            SqliteSaver(sqlite3.connect(self.checkpoint_path, check_same_thread=False))
            if self.checkpoint_path
            else MemorySaver()
        )
        self.goap = GoapGraph(
            self.actions(),
            sensors=[FunctionalSensor("platform", lambda ws: self.sense(ws))],
            tracer=self.tracer,
            termination_policies=[MaxActionsPolicy(self.max_actions)],
        )
        self.graph = self.goap.compile(checkpointer=self.checkpointer)
        self._subscribe("order.placed", lambda order: self.start(order.id))
        self._subscribe("order.replaced", self._on_replaced)
        self._subscribe(
            "payment.processed", lambda payment: self.push(payment.order_id, "payment.processed")
        )
        self._subscribe(
            "payment.unavailable",
            lambda payment: self.push(payment.order_id, "payment.unavailable"),
        )
        self._subscribe("payment.gateway_up", self._on_gateway_up)
        self._subscribe("order.status", lambda order, previous: self.push(order.id, "order.status"))
        self._subscribe(
            "delivery.assigned",
            lambda delivery, driver: self.push(delivery.order_id, "delivery.assigned"),
        )
        self._subscribe(
            "delivery.status", lambda delivery: self.push(delivery.order_id, "delivery.status")
        )
        self._subscribe(
            "delivery.dropped",
            lambda delivery, driver, reason, stage: self.push(
                delivery.order_id, "delivery.dropped"
            ),
        )
        self._subscribe("support.answered", self._on_answered)
        self._subscribe("world.changed", self._on_world_changed)
        self._subscribe(
            "driver.available", lambda driver: self.dispatch_all("a driver came online")
        )
        self._ticker = self.tick
        store.tickers.append(self._ticker)

    def uninstall(self) -> None:
        for event, listener in self._subscriptions:
            self.store.listeners[event].remove(listener)
        self._subscriptions.clear()
        if self._ticker is not None:
            self.store.tickers.remove(self._ticker)
            self._ticker = None

    def _subscribe(self, event: str, listener: Callable[..., None]) -> None:
        self.store.subscribe(event, listener)
        self._subscriptions.append((event, listener))

    # the world as the planner sees it ------------------------------------------------------

    def current_order(self, order_id: str) -> Order:
        order = get_order(self.store, order_id)
        while order.replaced_by_order_id:
            order = get_order(self.store, order.replaced_by_order_id)
        return order

    def sense(self, ws: dict[str, Any]) -> dict[str, Any]:
        store = self.store
        order = self.current_order(ws["order_id"])
        payment = payment_for_order(store, order.id)
        paid = order.payment_status == "COMPLETED" or (
            payment is not None and payment.status == "COMPLETED"
        )
        failed = (not paid) and payment is not None and payment.status == "FAILED"
        customer = store.users[order.customer_id]
        tried = {
            p.method["id"] for p in store.payments.values() if p.order_id == order.id and p.method
        }
        backup = any(c["id"] not in tried for c in customer.cards)
        late = order.promise_breached
        if order.status in {"CREATED", "PAYMENT_PENDING"}:
            kitchen = "unpaid"
        elif order.status in {"CONFIRMED", "PREPARING"}:
            kitchen = "late" if late and not ws.get("accepted_delay") else "cooking"
        elif order.status in {"READY_FOR_PICKUP", "DRIVER_ASSIGNED", "PICKED_UP", "DELIVERED"}:
            kitchen = "ready"
        elif order.status == "CANCELLED" and order.cancelled_by == "RESTAURANT":
            kitchen = "failed"
        else:
            kitchen = "cancelled"
        delivery = delivery_for_order(store, order.id)
        if delivery is None:
            ride = "none"
        elif delivery.status == "DELIVERED":
            ride = "delivered"
        elif delivery.status == "CANCELLED":
            ride = "lost" if order.status == "PICKED_UP" else "none"
        elif delivery.status == "CREATED":
            ride = "stalled" if late and not ws.get("accepted_wait") else "searching"
        elif delivery.status == "DRIVER_ASSIGNED":
            ride = "assigned"
        else:
            ride = "picked_up"
        options = alternatives(store, order)
        needs_approval = bool(options) and options[0].needs_approval(order, customer)
        answer = ws.get("substitution") or {}
        approved = answer.get("approved") if isinstance(answer, dict) else None
        credited = any(
            e.order_id in self.thread_orders(ws["order_id"]) and e.amount > 0 for e in store.credits
        )
        cancelled = order.status in {"CANCELLED", "FAILED"}
        world = self.world_facts(order, ws["order_id"])
        return {
            **world,
            "current_order_id": order.id,
            "gateway_up": not gateway_is_down(store),
            "paid": paid,
            "payment_failed": failed,
            "backup_card": backup,
            "kitchen": kitchen,
            "alternative": bool(options),
            "approval_ok": (not needs_approval) or approved is True,
            "needs_approval": needs_approval,
            "approved": approved,
            "delivery": ride,
            "widened": "dispatch" in store.hooks,
            "late": late,
            "credited": credited,
            "cancelled": cancelled,
            "delivered": order.status == "DELIVERED",
            "compensated_if_late": (not late) or credited,
        }

    #: facts every wait watches besides its own: lateness, and the world turning against the promise
    LATENESS = ("late", "credited", "compensated_if_late", "promise_at_risk")

    def world_facts(self, order: Order, thread: str) -> dict[str, Any]:
        """The world as facts, read through the same MCP tools a model would call and counted
        against the order; one read per tick, since the sensor runs after every event."""
        store = self.store
        stamp = (iso(now()), len(store.world.log))
        cached = self._world.get(thread)
        if cached is not None and cached[0] == stamp:
            return dict(cached[1])
        tools = WorldTools(store, thread)
        kitchen = store.restaurants[order.restaurant_id]
        zone = zone_of(kitchen.latitude, kitchen.longitude)
        weather = tools.get_weather(zone)["kind"]
        facts: dict[str, Any] = {
            "zone": zone,
            "door_zone": None,
            "rain": weather in {"rain", "storm"},
            "route_delay_high": False,
            "corridor_closed": False,
            "ride_minutes": 0,
            "promise_at_risk": False,
            "drivers_thin": len(available_drivers(store)) <= 1,
        }
        if order.delivery_latitude is not None and order.delivery_longitude is not None:
            door = (order.delivery_latitude, order.delivery_longitude)
            traffic = tools.get_traffic((kitchen.latitude, kitchen.longitude), door)
            door_weather = tools.get_weather(zone_of(*door))["kind"]
            left = (order.promised_at - now()).total_seconds() / 60 if order.promised_at else None
            facts.update(
                {
                    "door_zone": zone_of(*door),
                    "rain": facts["rain"] or door_weather in {"rain", "storm"},
                    "route_delay_high": traffic["delayFactor"] >= 1.5,
                    "corridor_closed": bool(traffic["closures"]),
                    "ride_minutes": traffic["minutes"],
                    # even leaving now, the courier could not make the promise
                    "promise_at_risk": (
                        left is not None
                        and order.status not in TERMINAL
                        and order.status != "DELIVERED"
                        and traffic["minutes"] > left
                    ),
                }
            )
        self._world[thread] = (stamp, dict(facts))
        return facts

    def observed(self, ws: dict[str, Any], *keys: str) -> dict[str, Any]:
        """The facts an action is answerable for, read fresh from the platform.

        Returning the whole world after every step would make any unrelated change
        look like a deviation; the sensor refreshes everything before a re-plan anyway.
        """
        sensed = self.sense(ws)
        return {k: sensed[k] for k in (*keys, *self.LATENESS)}

    def thread_orders(self, thread: str) -> set[str]:
        return {o for o, t in self.threads.items() if t == thread} | {thread}

    # the actions -----------------------------------------------------------------------------

    def actions(self) -> list[ActionSpec]:
        owned = {
            "await_payment": ("paid", "payment_failed", "kitchen", "gateway_up"),
            "await_gateway": ("gateway_up", "paid", "payment_failed", "kitchen"),
            "await_kitchen": ("kitchen",),
            "await_driver": ("delivery",),
            "await_pickup": ("delivery",),
            "await_dropoff": ("delivery", "delivered"),
        }

        def wait(name: str) -> Callable[[dict[str, Any]], dict[str, Any]]:
            def execute(ws: dict[str, Any]) -> dict[str, Any]:
                keys = (*owned[name], *self.LATENESS)
                while (
                    True
                ):  # every platform event resumes us; only a change in our facts ends the wait
                    interrupt({"waiting": name, "orderId": ws["order_id"]})
                    if name == "await_payment":
                        self.confirm_if_paid(ws)
                    elif name == "await_gateway":  # the gateway is back: charge the attempt now
                        retry_pending_payment(self.store, order_of(ws).id)
                        self.confirm_if_paid(ws)
                    fresh = self.observed(ws, *owned[name])
                    if any(fresh[k] != ws.get(k) for k in keys):
                        return fresh  # the truth, whatever the event said

            return execute

        def order_of(ws: dict[str, Any]) -> Order:
            return self.current_order(ws["order_id"])

        def retry_card(ws: dict[str, Any]) -> dict[str, Any]:
            payment = payment_for_order(self.store, order_of(ws).id)
            if payment is None:
                raise ApiError(409, "nothing to retry")
            retry_with_backup(self.store, payment)
            self.confirm_if_paid(ws)
            return self.observed(ws, "paid", "payment_failed", "backup_card", "kitchen")

        def do_replace(ws: dict[str, Any]) -> dict[str, Any]:
            order = order_of(ws)
            options = alternatives(self.store, order)
            if not options:
                raise ApiError(409, "no other kitchen can make it")
            choice = options[0]
            reason = order.cancellation_note or "the kitchen could not deliver"
            replacement = replace(self.store, order, choice, reason)
            self.threads[replacement.id] = self.threads.get(order.id, ws["order_id"])
            fresh = self.observed(
                ws,
                "current_order_id",
                "kitchen",
                "delivery",
                "alternative",
                "needs_approval",
                "approval_ok",
            )
            return {**fresh, "accepted_delay": False, "accepted_wait": False}

        def replace_cost(world_state: dict[str, Any]) -> float:
            try:
                order = order_of(world_state)
            except ApiError:
                return 50.0
            options = alternatives(self.store, order)
            if not options:
                return 50.0
            choice: Alternative = options[0]
            extra = max(0.0, float(choice.difference(order)))
            jam = 0.0
            if order.delivery_latitude is not None and order.delivery_longitude is not None:
                traffic = WorldTools(self.store, world_state.get("order_id")).get_traffic(
                    (choice.restaurant.latitude, choice.restaurant.longitude),
                    (order.delivery_latitude, order.delivery_longitude),
                )
                jam = (traffic["delayFactor"] - 1.0) * 5.0  # a slow route to the door costs
            return (
                3.0
                + extra
                + choice.restaurant.prep_minutes / 10
                + (choice.distance_km or 1.0)
                + jam
            )

        def ask_substitution(ws: dict[str, Any]) -> dict[str, Any]:
            answer = ws.get("substitution") or {}
            return {
                "approved": bool(answer.get("approved")),
                "approval_ok": bool(answer.get("approved")),
            }

        def do_dispatch(ws: dict[str, Any]) -> dict[str, Any]:
            order = order_of(ws)
            if delivery_for_order(self.store, order.id) is None:
                create_delivery(self.store, order)
            self.dispatch_all("the kitchen is ready")
            return self.observed(ws, "delivery")

        def widen(ws: dict[str, Any]) -> dict[str, Any]:
            use(self.store, stack_on_finishing)
            self.dispatch_all("the search was widened")
            return {
                **self.observed({**ws, "accepted_wait": True}, "delivery", "widened"),
                "accepted_wait": True,
            }

        def accept_wait(ws: dict[str, Any]) -> dict[str, Any]:
            return {
                **self.observed({**ws, "accepted_wait": True}, "delivery"),
                "accepted_wait": True,
            }

        def accept_delay(ws: dict[str, Any]) -> dict[str, Any]:
            return {
                **self.observed({**ws, "accepted_delay": True}, "kitchen"),
                "accepted_delay": True,
            }

        def widen_cost(world_state: dict[str, Any]) -> float:
            return 1.0 if world_state.get("rain") or world_state.get("drivers_thin") else 2.0

        def searching_cost(world_state: dict[str, Any]) -> float:
            return 7.0 if world_state.get("promise_at_risk") else 5.0

        def delay_cost(world_state: dict[str, Any]) -> float:
            return 8.0 if world_state.get("promise_at_risk") else 6.0

        def credit_cost(world_state: dict[str, Any]) -> float:
            # the weather's lateness is the house's to make up for, cheaply
            return 2.0 if world_state.get("rain") or world_state.get("route_delay_high") else 3.0

        def credit(ws: dict[str, Any]) -> dict[str, Any]:
            order = order_of(ws)
            try:
                issue_credit(
                    self.store, order.customer_id, CREDIT_FOR_LATENESS, "late delivery", order.id
                )
            except ApiError:
                pass
            return self.observed(ws, "credited", "compensated_if_late")

        return [
            ActionSpec(
                "await_payment",
                {"paid": False, "payment_failed": False, "cancelled": False, "gateway_up": True},
                {"paid": True, "kitchen": "cooking"},
                cost=1.0,
                execute=wait("await_payment"),
            ),
            ActionSpec(
                "await_gateway",
                {"gateway_up": False, "paid": False, "cancelled": False},
                {"gateway_up": True, "paid": True, "kitchen": "cooking"},
                cost=4.0,  # dearer than a card retry, far cheaper than giving the order up
                execute=wait("await_gateway"),
            ),
            ActionSpec(
                "retry_backup_card",
                {"payment_failed": True, "backup_card": True},
                {"paid": True, "payment_failed": False, "kitchen": "cooking"},
                cost=2.0,
                execute=retry_card,
            ),
            ActionSpec(
                "await_kitchen",
                {"paid": True, "kitchen": "cooking"},
                {"kitchen": "ready"},
                cost=1.0,
                execute=wait("await_kitchen"),
            ),
            ActionSpec(
                "accept_kitchen_delay",
                {"kitchen": "late"},
                {"kitchen": "cooking", "accepted_delay": True},
                cost=delay_cost,
                execute=accept_delay,
            ),
            ActionSpec(
                "replace_after_kitchen_failure",
                {"kitchen": "failed", "alternative": True, "approval_ok": True},
                {"kitchen": "cooking", "delivery": "none"},
                cost=replace_cost,
                execute=do_replace,
            ),
            ActionSpec(
                "replace_late_kitchen",
                {"kitchen": "late", "alternative": True, "approval_ok": True},
                {"kitchen": "cooking", "delivery": "none"},
                cost=replace_cost,
                execute=do_replace,
            ),
            ActionSpec(
                "replace_lost_order",
                {"delivery": "lost", "alternative": True, "approval_ok": True},
                {"kitchen": "cooking", "delivery": "none"},
                cost=replace_cost,
                execute=do_replace,
            ),
            ActionSpec(
                "ask_substitution",
                {"needs_approval": True, "approval_ok": False},
                {"approval_ok": True, "approved": True},
                cost=2.0,
                execute=ask_substitution,
                require_human_approval=SubstitutionApproval,
                human_input_key="substitution",
            ),
            ActionSpec(
                "dispatch",
                {"kitchen": "ready", "delivery": "none"},
                {"delivery": "searching"},
                cost=1.0,
                execute=do_dispatch,
            ),
            ActionSpec(
                "await_driver",
                {"delivery": "searching"},
                {"delivery": "assigned"},
                cost=2.0,
                execute=wait("await_driver"),
            ),
            ActionSpec(
                "widen_dispatch",
                {"delivery": "stalled", "widened": False},
                {"delivery": "searching", "widened": True, "accepted_wait": True},
                cost=widen_cost,
                execute=widen,
            ),
            ActionSpec(
                "keep_searching",
                {"delivery": "stalled"},
                {"delivery": "searching", "accepted_wait": True},
                cost=searching_cost,
                execute=accept_wait,
            ),
            ActionSpec(
                "await_pickup",
                {"delivery": "assigned"},
                {"delivery": "picked_up"},
                cost=1.0,
                execute=wait("await_pickup"),
            ),
            ActionSpec(
                "await_dropoff",
                {"delivery": "picked_up"},
                {"delivery": "delivered", "delivered": True},
                cost=1.0,
                execute=wait("await_dropoff"),
            ),
            ActionSpec(
                "issue_credit",
                {"late": True, "credited": False},
                {"credited": True, "compensated_if_late": True},
                cost=credit_cost,
                execute=credit,
            ),
        ]

    # threads ---------------------------------------------------------------------------------

    def _config(self, thread: str) -> RunnableConfig:
        return {"configurable": {"thread_id": thread}}

    def start(self, order_id: str) -> None:
        self.threads[order_id] = order_id
        self.pending.append((order_id, {"kind": "start"}))
        self.drain()

    def push(self, order_id: str, kind: str) -> None:
        thread = self.threads.get(order_id)
        if thread is None:
            return
        self.pending.append((thread, {"kind": kind, "orderId": order_id}))
        self.drain()

    def _on_gateway_up(self) -> None:
        """The gateway is back: every thread waiting for it may charge its attempt."""
        for thread in set(self.threads.values()):
            if self.waiting_at(thread) == "await_gateway":
                self.pending.append((thread, {"kind": "payment.gateway_up"}))
        self.drain()

    def _on_world_changed(self, change: dict[str, Any]) -> None:
        """The weather or the roads moved: every waiting thread looks again; a wait whose
        facts changed ends, and the planner re-plans from the real state."""
        self._world.clear()
        for thread in set(self.threads.values()):
            if self.waiting_at(thread) in WAITS:
                self.pending.append((thread, {"kind": "world.changed", "change": change}))
        self.drain()

    def _on_replaced(self, original: Order, replacement: Order, reason: str) -> None:
        thread = self.threads.get(original.id)
        if thread is not None:
            self.threads[replacement.id] = thread

    def _on_answered(self, question: Question) -> None:
        thread = next((t for t, q in self.questions.items() if q == question.id), None)
        if thread is None:
            return
        del self.questions[thread]
        answer = question.answer
        approved = answer == "approve" or (
            isinstance(answer, dict) and bool(answer.get("approved"))
        )
        resume: Any = {"approved": True, "note": "approved by support"} if approved else False
        self.pending.append((thread, {"kind": "support.answered", "resume": resume}))
        self.drain()

    def tick(self) -> None:
        for thread in set(self.threads.values()):
            waiting = self.waiting_at(thread)
            if waiting not in TIMED_WAITS:
                continue
            if self.current_order(thread).promise_breached:
                self.pending.append((thread, {"kind": "tick"}))
        self.drain()

    def drain(self) -> None:
        if self.draining:
            return
        self.draining = True
        try:
            while self.pending:
                thread, event = self.pending.popleft()
                config = self._config(thread)
                self.tracer.order_id = thread
                if event["kind"] == "start":
                    goal = GoalSpec(conditions={"delivered": True, "compensated_if_late": True})
                    world = {"order_id": thread, "accepted_delay": False, "accepted_wait": False}
                    world.update(self.sense(world))
                    state = self.graph.invoke(self.goap.initial_state(goal, world), config)
                else:
                    snapshot = self.graph.get_state(config)
                    if not snapshot.next:
                        continue
                    if thread in self.questions and event["kind"] != "support.answered":
                        continue  # a person is being asked; platform events wait their turn
                    resume = event.get("resume", event)
                    state = self.graph.invoke(Command(resume=resume), config)
                self.after(thread, state)
        finally:
            self.draining = False

    def after(self, thread: str, state: dict[str, Any]) -> None:
        """Look at where the thread stopped: a wait, a question for a person, or the end."""
        interrupts = state.get("__interrupt__") or []
        if interrupts:
            payload = interrupts[0].value
            if isinstance(payload, dict) and payload.get("type") in {
                "goap_action_form",
                "goap_action_approval",
            }:
                self.ask_person(thread, payload)
            return
        status = state.get("status")
        order = self.current_order(thread)
        if status == "goal_achieved":
            return
        explanation = state.get("no_plan_explanation") or state.get("replan_reason") or status
        decide(self.store, thread, "cancel", because=f"no route to the goal: {explanation}")
        self.cancel_and_refund(order, str(explanation))

    def ask_person(self, thread: str, payload: dict[str, Any]) -> None:
        order = self.current_order(thread)
        options = alternatives(self.store, order)
        if options:
            kitchen, extra = options[0].restaurant.name, options[0].difference(order)
            detail = f"{kitchen} can make this for {dollars(extra, signed=True)}. Go ahead?"
        else:
            detail = "Approve the substitution?"
        question = ask(
            self.store,
            order.id,
            "substitution",
            detail,
            options=[
                {"value": "approve", "label": "Yes, go ahead"},
                {"value": "decline", "label": "No, refund"},
            ],
            default="decline",
            deadline_seconds=120,
            form=None,
            action=payload.get("action"),
        )
        self.questions[thread] = question.id
        decide(self.store, thread, "ask support", because="the substitution needs approval")

    def waiting_at(self, order_id: str) -> str | None:
        thread = self.threads.get(order_id, order_id)
        snapshot = self.graph.get_state(self._config(thread))
        if not snapshot.next:
            return None
        for task in snapshot.tasks:
            for intr in getattr(task, "interrupts", ()):
                value = intr.value
                if isinstance(value, dict):
                    return str(value.get("waiting") or value.get("type") or "?")
        return snapshot.next[0]

    # platform moves ---------------------------------------------------------------------------

    def confirm_if_paid(self, ws: dict[str, Any]) -> None:
        """A completed payment confirms the order: the kitchen may start."""
        order = self.current_order(ws["order_id"])
        payment = payment_for_order(self.store, order.id)
        if (
            payment is not None
            and payment.status == "COMPLETED"
            and order.status in {"CREATED", "PAYMENT_PENDING"}
        ):
            decide(self.store, ws["order_id"], "confirm", because="payment completed")
            confirm_paid(self.store, order.id)

    def dispatch_all(self, because: str) -> None:
        for delivery, driver in dispatch_pending(self.store):
            decide(
                self.store,
                self.threads.get(delivery.order_id, delivery.order_id),
                "assign driver",
                because=because,
                driverId=driver.id,
                licensePlate=driver.license_plate,
            )

    def cancel_and_refund(self, order: Order, reason: str) -> None:
        store = self.store
        delivery = delivery_for_order(store, order.id)
        if delivery is not None and delivery.status in ACTIVE:
            cancel_delivery(store, delivery, reason[:200], "SYSTEM")
        if order.status not in TERMINAL:
            transition(
                store,
                order,
                "CANCELLED",
                cancelled_by="SYSTEM",
                cancellation_reason="NO_RECOVERY",
                cancellation_note=reason[:200],
            )
        payment = payment_for_order(store, order.id)
        if payment is not None and payment.status == "COMPLETED":
            decide(store, self.threads.get(order.id, order.id), "refund", because=reason[:120])
            refund_payment(store, payment, "ORDER_CANCELLED")


def mermaid() -> str:
    engine = GoapEngine()
    engine.store = Store()
    engine.tracer = Trace(engine.store, None)
    return GoapGraph(engine.actions()).compile().get_graph().draw_mermaid()


__all__ = [
    "Delivery",
    "Driver",
    "GoapEngine",
    "Payment",
    "SubstitutionApproval",
    "available_drivers",
    "iso",
    "now",
    "mermaid",
]
