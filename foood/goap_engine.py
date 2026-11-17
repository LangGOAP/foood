"""The LangGOAP engine: the order's lifecycle as a goal, the platform's tools as actions.

One GOAP thread per customer order (it follows a replacement order to the
door). A sensor derives the world state from the platform before every plan
and after every action, so an action's outcome is what the platform says it
is; actions change the platform through the same domain functions the other
engines call; the waits (for the payment, the kitchen, a driver, the pickup,
the drop-off) are ``ActionSpec.wait`` actions that pause the thread until a
platform event changes one of their facts. When the truth is not what the plan
expected, the planner re-plans from it. Nothing here asks a model: the
alternatives are costed, and A* picks. Only the substitution approval asks a
person, through support, as a typed form.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterable
from decimal import Decimal
from pathlib import Path
from typing import Any

from langgoap import ActionSpec, GoapGraph, GoapResult
from langgoap.tracing import NullTracer
from langgraph.checkpoint.sqlite import SqliteSaver
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

TIMED_WAITS = {"await_kitchen", "await_driver"}
# a new kitchen or a new search: whatever delay was accepted for the old one no longer holds
NOTHING_ACCEPTED = {"accepted_delay": False, "accepted_wait": False}
CREDIT_FOR_LATENESS = Decimal("5.00")


class SubstitutionApproval(BaseModel):
    """Support's answer to 'another kitchen can make it for a different price'."""

    approved: bool = Field(description="go ahead with the other kitchen")
    note: str = Field(default="", description="a word for the record")


class Trace(NullTracer):
    """Plans, re-plans and the actions that ran, written to the order's decisions."""

    def __init__(self, store: Store, waits: Iterable[str] = ()) -> None:
        self.store = store
        self.last_steps: dict[str, list[str]] = {}
        self.waits = frozenset(waits)  # the waits whose end is not an act of the engine's

    def on_plan_complete(self, plan: Any, duration_ms: float, thread_id: str | None = None) -> None:
        thread, steps = str(thread_id), [a.name for a in plan.actions]
        self.last_steps[thread] = steps
        decide(
            self.store,
            str(thread_id),
            "plan",
            because="A* over the platform's actions",
            steps=steps,
            planCost=round(float(plan.total_cost), 2),
            planningMs=round(duration_ms, 1),
        )

    def on_replan(
        self,
        reason: str,
        new_plan: Any,
        duration_ms: float | None = None,
        thread_id: str | None = None,
    ) -> None:
        steps = [a.name for a in new_plan.actions] if new_plan is not None else []
        thread = str(thread_id)
        previous = self.last_steps.get(thread, [])
        self.last_steps[thread] = steps
        # the world ran ahead of the plan (a driver was already there): same route, fewer steps
        skipped = bool(steps) and len(steps) < len(previous) and previous[-len(steps) :] == steps
        decide(
            self.store,
            str(thread_id),
            "plan skips ahead" if skipped else "replan",
            because=reason,
            steps=steps,
            planCost=round(float(new_plan.total_cost), 2) if new_plan is not None else None,
        )

    def on_plan_failed(self, reason: str, duration_ms: float, thread_id: str | None = None) -> None:
        decide(self.store, str(thread_id), "no plan", because=reason)

    def on_action_complete(self, result: Any, thread_id: str | None = None) -> None:
        """The executor's update: the last entry of its execution history is the action."""
        history = result.get("execution_history") if isinstance(result, dict) else None
        if not history or history[-1].action_name in self.waits:
            return
        last = history[-1]
        decide(
            self.store,
            str(thread_id),
            f"act: {last.action_name}",
            because="planned",
            succeeded=bool(last.success),
        )


class GoapEngine:
    name = "langgoap"

    def __init__(self, checkpoint_path: Path | None = None, max_actions: int = 60) -> None:
        self.checkpoint_path = checkpoint_path
        self.max_actions = max_actions
        self.threads: dict[str, str] = {}  # any order id -> its thread (the first order's id)
        self.questions: dict[str, str] = {}  # thread -> open support question id
        self._subscriptions: list[tuple[str, Callable[..., None]]] = []
        self._ticker: Callable[[], None] | None = None

    # wiring ----------------------------------------------------------------------------

    def install(self, store: Store) -> None:
        self.store = store
        actions = self.actions()
        # a wait that only waits is the world moving, not an act of the engine's
        quiet_waits = [a.name for a in actions if a.is_wait and a.execute is None]
        self.tracer = Trace(store, waits=quiet_waits)
        self.goap = GoapGraph(
            actions,
            effects="observed",
            goal={"delivered": True, "compensated_if_late": True},
            sensors=[self.sense],
            always_observe=self.LATENESS,
            tracer=self.tracer,
            max_actions=self.max_actions,
            checkpointer=(
                SqliteSaver(sqlite3.connect(self.checkpoint_path, check_same_thread=False))
                if self.checkpoint_path
                else None
            ),
            on_stop=self.after,
        )
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
        return {
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

    #: facts every wait watches besides its own: lateness and the compensation it calls for
    LATENESS = ("late", "credited", "compensated_if_late")

    def thread_orders(self, thread: str) -> set[str]:
        return {o for o, t in self.threads.items() if t == thread} | {thread}

    # the actions -----------------------------------------------------------------------------

    def actions(self) -> list[ActionSpec]:
        replaced = ("current_order_id", "alternative")  # what a new order changes besides effects

        def order_of(ws: dict[str, Any]) -> Order:
            return self.current_order(ws["order_id"])

        def gateway_back(ws: dict[str, Any]) -> None:  # the gateway is back: charge the attempt now
            retry_pending_payment(self.store, order_of(ws).id)
            self.confirm_if_paid(ws)

        def retry_card(ws: dict[str, Any]) -> None:
            payment = payment_for_order(self.store, order_of(ws).id)
            if payment is None:
                raise ApiError(409, "nothing to retry")
            retry_with_backup(self.store, payment)
            self.confirm_if_paid(ws)

        def do_replace(ws: dict[str, Any]) -> dict[str, Any]:
            order = order_of(ws)
            options = alternatives(self.store, order)
            if not options:
                raise ApiError(409, "no other kitchen can make it")
            choice = options[0]
            reason = order.cancellation_note or "the kitchen could not deliver"
            replacement = replace(self.store, order, choice, reason)
            self.threads[replacement.id] = self.threads.get(order.id, ws["order_id"])
            return dict(NOTHING_ACCEPTED)

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
            return 3.0 + extra + choice.restaurant.prep_minutes / 10 + (choice.distance_km or 1.0)

        def do_dispatch(ws: dict[str, Any]) -> None:
            order = order_of(ws)
            if delivery_for_order(self.store, order.id) is None:
                create_delivery(self.store, order)
            self.dispatch_all("the kitchen is ready")

        def widen(ws: dict[str, Any]) -> None:
            use(self.store, stack_on_finishing)
            self.dispatch_all("the search was widened")

        def credit(ws: dict[str, Any]) -> None:
            order = order_of(ws)
            try:
                issue_credit(
                    self.store, order.customer_id, CREDIT_FOR_LATENESS, "late delivery", order.id
                )
            except ApiError:
                pass

        return [
            ActionSpec.wait(
                "await_payment",
                {"paid": False, "payment_failed": False, "cancelled": False, "gateway_up": True},
                {"paid": True, "kitchen": "cooking"},
                cost=1.0,
                observe=("payment_failed", "gateway_up"),
                on_resume=self.confirm_if_paid,
            ),
            ActionSpec.wait(
                "await_gateway",
                {"gateway_up": False, "paid": False, "cancelled": False},
                {"gateway_up": True, "paid": True, "kitchen": "cooking"},
                cost=4.0,  # dearer than a card retry, far cheaper than giving the order up
                observe=("payment_failed",),
                on_resume=gateway_back,
            ),
            ActionSpec(
                "retry_backup_card",
                {"payment_failed": True, "backup_card": True},
                {"paid": True, "payment_failed": False, "kitchen": "cooking"},
                cost=2.0,
                execute=retry_card,
                observe=("backup_card",),
            ),
            ActionSpec.wait(
                "await_kitchen",
                {"paid": True, "kitchen": "cooking"},
                {"kitchen": "ready"},
                cost=1.0,
            ),
            ActionSpec(
                "accept_kitchen_delay",
                {"kitchen": "late"},
                {"kitchen": "cooking", "accepted_delay": True},
                cost=6.0,
            ),
            ActionSpec(
                "replace_after_kitchen_failure",
                {"kitchen": "failed", "alternative": True, "approval_ok": True},
                {"kitchen": "cooking", "delivery": "none"},
                cost=replace_cost,
                execute=do_replace,
                observe=(*replaced, "needs_approval", "approval_ok"),
            ),
            ActionSpec(
                "replace_late_kitchen",
                {"kitchen": "late", "alternative": True, "approval_ok": True},
                {"kitchen": "cooking", "delivery": "none"},
                cost=replace_cost,
                execute=do_replace,
                observe=(*replaced, "needs_approval", "approval_ok"),
            ),
            ActionSpec(
                "replace_lost_order",
                {"delivery": "lost", "alternative": True, "approval_ok": True},
                {"kitchen": "cooking", "delivery": "none"},
                cost=replace_cost,
                execute=do_replace,
                observe=(*replaced, "needs_approval", "approval_ok"),
            ),
            ActionSpec(
                "ask_substitution",
                {"needs_approval": True, "approval_ok": False},
                {"approval_ok": True, "approved": True},
                cost=2.0,
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
            ActionSpec.wait(
                "await_driver",
                {"delivery": "searching"},
                {"delivery": "assigned"},
                cost=2.0,
            ),
            ActionSpec(
                "widen_dispatch",
                {"delivery": "stalled", "widened": False},
                {"delivery": "searching", "widened": True, "accepted_wait": True},
                cost=2.0,
                execute=widen,
            ),
            ActionSpec(
                "keep_searching",
                {"delivery": "stalled"},
                {"delivery": "searching", "accepted_wait": True},
                cost=5.0,
            ),
            ActionSpec.wait(
                "await_pickup",
                {"delivery": "assigned"},
                {"delivery": "picked_up"},
                cost=1.0,
            ),
            ActionSpec.wait(
                "await_dropoff",
                {"delivery": "picked_up"},
                {"delivery": "delivered", "delivered": True},
                cost=1.0,
            ),
            ActionSpec(
                "issue_credit",
                {"late": True, "credited": False},
                {"credited": True, "compensated_if_late": True},
                cost=3.0,
                execute=credit,
            ),
        ]

    # threads ---------------------------------------------------------------------------------

    def start(self, order_id: str) -> None:
        self.threads[order_id] = order_id
        self.goap.start({"order_id": order_id, **NOTHING_ACCEPTED}, thread_id=order_id)

    def push(self, order_id: str, kind: str) -> None:
        thread = self.threads.get(order_id)
        if thread is not None:
            self.goap.notify(thread, {"kind": kind, "orderId": order_id})

    def _on_gateway_up(self) -> None:
        """The gateway is back: every thread waiting for it may charge its attempt."""
        for thread in set(self.threads.values()):
            if self.goap.waiting_action(thread) == "await_gateway":
                self.goap.notify(thread, {"kind": "payment.gateway_up"})

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
        self.goap.resume(resume, thread_id=thread)

    def tick(self) -> None:
        for thread in set(self.threads.values()):
            if (
                self.goap.waiting_action(thread) in TIMED_WAITS
                and self.current_order(thread).promise_breached
            ):
                self.goap.notify(thread, {"kind": "tick"})

    def after(self, thread: str, result: GoapResult) -> None:
        """Look at where the thread stopped: a wait, a question for a person, or the end."""
        if (paused := result.interrupt) is not None:
            if paused["type"] in {"goap_action_form", "goap_action_approval"}:
                self.ask_person(thread, paused)
            return
        if result.succeeded:
            return
        why = result.get("no_plan_explanation") or result.get("replan_reason") or result.status
        decide(self.store, thread, "cancel", because=f"no route to the goal: {why}")
        self.cancel_and_refund(self.current_order(thread), str(why))

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

    def waiting_at(self, thread: str) -> str | None:
        """Where a thread is paused, for the support page and the tests: the wait's name, or
        the kind of question asked."""
        paused = self.goap.waiting(thread)
        if paused is None:
            return None
        if paused["type"] == "goap_action_wait":
            return str(paused.get("waiting") or paused["action"])
        return str(paused["type"])

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
    engine.tracer = Trace(engine.store)
    return GoapGraph(engine.actions(), sensors=[engine.sense]).compile().get_graph().draw_mermaid()


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
