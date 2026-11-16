"""Simulated payments: the checkout's create -> process pair, and refunds.

Nothing is charged anywhere. The outcome of ``process`` comes from the
``charge`` hook on the store (always approved by default), which the scenario
engine replaces to decline cards on cue. ``simulateFailure`` is the Delivery
Plus test hook, honoured only when ``PAYMENT_TEST_HOOKS=true``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Header
from pydantic import BaseModel

from foood.auth import CurrentUser, StoreDep
from foood.errors import ApiError
from foood.models import money, new_id
from foood.orders import Order, confirm_paid, fail_payment, get_order, transition
from foood.store import Store, iso, now

router = APIRouter(prefix="/api", tags=["payments"])

PAYABLE = {"CREATED", "PAYMENT_PENDING"}


@dataclass
class Payment:
    order_id: str
    customer_id: str
    amount: Decimal
    status: str = "PENDING"  # PENDING | PROCESSING | COMPLETED | FAILED | REFUNDED
    failure_reason: str | None = None
    id: str = field(default_factory=new_id)
    created_at: datetime = field(default_factory=now)
    updated_at: datetime = field(default_factory=now)

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "orderId": self.order_id,
            "customerId": self.customer_id,
            "amount": money(self.amount),
            "status": self.status,
            "failureReason": self.failure_reason,
            "createdAt": iso(self.created_at),
            "updatedAt": iso(self.updated_at),
        }


@dataclass
class Refund:
    payment_id: str
    order_id: str
    amount: Decimal
    reason: str
    id: str = field(default_factory=new_id)
    status: str = "COMPLETED"
    created_at: datetime = field(default_factory=now)

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "paymentId": self.payment_id,
            "orderId": self.order_id,
            "amount": money(self.amount),
            "status": self.status,
            "reason": self.reason,
            "createdAt": iso(self.created_at),
        }


def charge(store: Store, payment: Payment) -> tuple[str, str | None]:
    """Ask the (simulated) card network; returns ``(status, failure_reason)``."""
    hook = store.hooks.get("charge")
    if hook is None:
        return "COMPLETED", None
    status, reason = hook(payment)
    return status, reason


def payment_for_order(store: Store, order_id: str) -> Payment | None:
    return next((p for p in store.payments.values() if p.order_id == order_id), None)


def refund_payment(store: Store, payment: Payment, reason: str) -> Refund:
    if payment.status != "COMPLETED":
        raise ApiError(409, "Only a completed payment can be refunded", error="NotRefundable")
    payment.status = "REFUNDED"
    payment.updated_at = now()
    refund = Refund(payment.id, payment.order_id, payment.amount, reason)
    store.refunds[payment.id] = refund
    order = store.orders.get(payment.order_id)
    if order is not None:
        order.payment_status = "REFUNDED"
        order.updated_at = now()
    store.publish("payment.refunded", payment=payment, refund=refund)
    return refund


def _refund_on_cancel(order: Order, previous: str, store: Store) -> None:
    if order.status != "CANCELLED":
        return
    payment = payment_for_order(store, order.id)
    if payment is not None and payment.status == "COMPLETED":
        refund_payment(store, payment, "ORDER_CANCELLED")


def install(store: Store) -> None:
    """Refund automatically when a paid order is cancelled."""
    store.subscribe(
        "order.status", lambda order, previous: _refund_on_cancel(order, previous, store)
    )


# --- routes -------------------------------------------------------------------------


class CreatePaymentRequest(BaseModel):
    orderId: str


class ProcessRequest(BaseModel):
    simulateFailure: bool | None = None


class RefundRequest(BaseModel):
    reason: str | None = None


def _test_hooks_enabled() -> bool:
    return os.environ.get("PAYMENT_TEST_HOOKS", "").lower() in {"1", "true", "yes"}


def _get_payment(store: Store, payment_id: str, user_id: str, role: str) -> Payment:
    payment = store.payments.get(payment_id)
    if payment is None:
        raise ApiError(404, "Payment not found")
    if role != "ADMIN" and payment.customer_id != user_id:
        raise ApiError(403, "You do not have access to this payment")
    return payment


@router.post("/payments", status_code=201)
async def create_payment(
    body: CreatePaymentRequest,
    user: CurrentUser,
    store: StoreDep,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    key = (user.id, f"payment:{idempotency_key}") if idempotency_key else None
    if key and key in store.idempotency:
        return store.payments[store.idempotency[key]].to_json()
    order = get_order(store, body.orderId)
    if order.customer_id != user.id:
        raise ApiError(403, "You do not have access to this order")
    if order.status not in PAYABLE:
        raise ApiError(409, f"An order in {order.status} cannot be paid", error="OrderNotPayable")
    existing = payment_for_order(store, order.id)
    if existing is not None and existing.status in {"PENDING", "PROCESSING"}:
        payment = existing
    else:
        payment = Payment(order.id, user.id, order.total)
        store.payments[payment.id] = payment
        if order.status == "CREATED":
            transition(store, order, "PAYMENT_PENDING")
    if key:
        store.idempotency[key] = payment.id
    return payment.to_json()


@router.post("/payments/{id}/process", status_code=201)
async def process_payment(
    id: str, body: ProcessRequest, user: CurrentUser, store: StoreDep
) -> dict[str, Any]:
    if body.simulateFailure is not None and not _test_hooks_enabled():
        raise ApiError(400, "simulateFailure is a test hook and is disabled")
    payment = _get_payment(store, id, user.id, user.role)
    if payment.status == "COMPLETED":
        return payment.to_json()
    if payment.status != "PENDING":
        raise ApiError(
            409, f"A {payment.status} payment cannot be processed", error="InvalidTransition"
        )
    if body.simulateFailure is True:
        status, reason = "FAILED", "Card declined (simulated)"
    elif body.simulateFailure is False:
        status, reason = "COMPLETED", None
    else:
        status, reason = charge(store, payment)
    payment.status = status
    payment.failure_reason = reason
    payment.updated_at = now()
    store.publish("payment.processed", payment=payment)  # the outcome, then its consequence
    if status == "COMPLETED":
        confirm_paid(store, payment.order_id)
    else:
        fail_payment(store, payment.order_id, "PAYMENT_FAILED")
    return payment.to_json()


@router.get("/payments/{id}")
async def get_payment(id: str, user: CurrentUser, store: StoreDep) -> dict[str, Any]:
    return _get_payment(store, id, user.id, user.role).to_json()


@router.post("/payments/{id}/refund", status_code=201)
async def refund(
    id: str, body: RefundRequest, user: CurrentUser, store: StoreDep
) -> dict[str, Any]:
    if user.role != "ADMIN":
        raise ApiError(403, "Only support staff can refund a payment")
    payment = _get_payment(store, id, user.id, user.role)
    return refund_payment(store, payment, body.reason or "SUPPORT").to_json()


@router.get("/payments/{id}/refund")
async def get_refund(id: str, user: CurrentUser, store: StoreDep) -> dict[str, Any]:
    _get_payment(store, id, user.id, user.role)
    record = store.refunds.get(id)
    if record is None:
        raise ApiError(404, "No refund for this payment")
    return record.to_json()
