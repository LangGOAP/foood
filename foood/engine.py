"""The order engine: who decides what happens to an order between "paid" and "delivered".

The platform's modules record facts (a payment completed, the kitchen is ready, a
driver dropped the job) and publish them on the store. An ``OrderEngine`` listens
and decides: confirm or cancel the order, create and dispatch its delivery, refund,
requeue. ``main`` ships the hand-wired engine; the ``langgraph`` and ``langgoap``
branches add theirs here, and nothing else about the platform changes.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from fastapi import APIRouter, Request

from foood.deliveries import (
    Delivery,
    cancel_delivery,
    create_delivery,
    delivery_for_order,
    dispatch_pending,
)
from foood.drivers import Driver
from foood.orders import Order, confirm_paid, fail_payment, get_order, transition
from foood.payments import Payment, abandon, payment_for_order, refund_payment
from foood.store import Store, iso, now

router = APIRouter(prefix="/api", tags=["engine"])


class OrderEngine(Protocol):
    name: str

    def install(self, store: Store) -> None: ...


def decide(store: Store, order_id: str, decision: str, **detail: Any) -> None:
    """Record a decision so people (and the bench) can read why an order went where it went."""
    store.decisions.setdefault(order_id, []).append(
        {"at": iso(now()), "decision": decision, **detail}
    )


class HandWiredEngine:
    """What most shops run: fixed reactions, one per anticipated failure, no alternatives."""

    name = "hand-wired"

    def install(self, store: Store) -> None:
        self.store = store
        store.subscribe("payment.processed", self.on_payment)
        store.subscribe("payment.unavailable", self.on_unavailable)
        store.subscribe("order.status", self.on_order_status)
        store.subscribe("driver.available", lambda driver: self.dispatch("a driver came online"))
        store.subscribe("delivery.dropped", self.on_dropped)

    def on_payment(self, payment: Payment) -> None:
        if payment.status == "COMPLETED":
            decide(self.store, payment.order_id, "confirm", because="payment completed")
            confirm_paid(self.store, payment.order_id)
        elif payment.status == "FAILED":
            decide(self.store, payment.order_id, "cancel", because="payment failed")
            fail_payment(self.store, payment.order_id, "PAYMENT_FAILED")

    def on_unavailable(self, payment: Payment) -> None:
        """The gateway refused to answer: a fixed reaction treats it like any payment failure."""
        decide(self.store, payment.order_id, "cancel", because="payment gateway unavailable")
        abandon(self.store, payment, "Payment gateway unavailable")
        fail_payment(self.store, payment.order_id, "PAYMENT_GATEWAY_UNAVAILABLE")

    def on_order_status(self, order: Order, previous: str) -> None:
        if order.status == "READY_FOR_PICKUP" and delivery_for_order(self.store, order.id) is None:
            decide(self.store, order.id, "create delivery", because="the kitchen is ready")
            create_delivery(self.store, order)
            self.dispatch("a delivery was created")
        elif order.status == "CANCELLED":
            payment = payment_for_order(self.store, order.id)
            if payment is not None and payment.status == "COMPLETED":
                decide(self.store, order.id, "refund", because=f"cancelled by {order.cancelled_by}")
                refund_payment(self.store, payment, "ORDER_CANCELLED")
            delivery = delivery_for_order(self.store, order.id)
            if delivery is not None and delivery.status not in {"DELIVERED", "CANCELLED"}:
                decide(self.store, order.id, "cancel delivery", because="the order was cancelled")
                cancel_delivery(
                    self.store, delivery, order.cancellation_note, order.cancelled_by or "SYSTEM"
                )

    def on_dropped(
        self, delivery: Delivery, driver: Driver, reason: str | None, stage: str
    ) -> None:
        if stage == "before_pickup":
            decide(
                self.store,
                delivery.order_id,
                "dispatch again",
                because="the driver dropped the job",
            )
            self.dispatch("a driver dropped the job before pickup")
            return
        decide(
            self.store,
            delivery.order_id,
            "cancel",
            because="the driver dropped the job after pickup",
        )
        order = get_order(self.store, delivery.order_id)
        if order.status not in {"CANCELLED", "DELIVERED", "FAILED"}:
            transition(
                self.store,
                order,
                "CANCELLED",
                cancelled_by="SYSTEM",
                cancellation_reason="DELIVERY_CANCELLED",
                cancellation_note=reason or "The driver dropped the job",
            )

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


class NullEngine:
    """Decides nothing. Proves the seam: without an engine, a paid order stays where it is."""

    name = "null"

    def install(self, store: Store) -> None:
        return None


ENGINES: dict[str, Callable[[], OrderEngine]] = {"hand-wired": HandWiredEngine, "null": NullEngine}


def make_engine(name: str) -> OrderEngine:
    if name == "llm":  # the tier where one frontier model runs the evening
        from foood.agent_engine import from_env

        return from_env()
    if name == "langgraph":
        import os
        from pathlib import Path

        from foood.graph_engine import LangGraphEngine
        from foood.recovery import advisor_from_env

        path = os.environ.get("FOOOD_CHECKPOINTS")
        return LangGraphEngine(
            Path(path) if path else None,
            advisor=advisor_from_env("decide"),
            escalator=advisor_from_env("escalate"),
        )
    try:
        return ENGINES[name]()
    except KeyError:
        raise ValueError(
            f"unknown engine {name!r}; one of {sorted(ENGINES) + ['llm', 'langgraph']}"
        ) from None


@router.get("/_engine")
async def engine_status(request: Request, orderId: str | None = None) -> dict[str, Any]:
    """Which engine runs the lifecycle and the decisions it took (not part of the contract)."""
    store = request.app.state.store
    decisions = store.decisions if orderId is None else {orderId: store.decisions.get(orderId, [])}
    return {"name": request.app.state.engine.name, "decisions": decisions}
