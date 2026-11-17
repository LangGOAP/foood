"""Credits: compensation the customer sees, applied at the next checkout.

A ledger per customer. Engines (or support) issue a credit for a breach, a
cancellation after pickup or a long wait; the next quote and order apply the
balance and the payment charges the rest; a cancelled order gives its credit
back. One compensation credit per order, so refunds and credits never pay the
same failure twice.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from foood.auth import CurrentUser, StoreDep
from foood.errors import ApiError
from foood.models import money, new_id
from foood.money import CURRENCY
from foood.store import Store, User, iso, now

router = APIRouter(prefix="/api", tags=["credits"])


@dataclass
class CreditEntry:
    customer_id: str
    amount: Decimal  # positive when issued, negative when applied to an order
    reason: str
    order_id: str | None = None  # the order compensated, or the order it was applied to
    id: str = field(default_factory=new_id)
    created_at: datetime = field(default_factory=now)

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "amount": money(self.amount),
            "reason": self.reason,
            "orderId": self.order_id,
            "createdAt": iso(self.created_at),
        }


def balance(store: Store, customer_id: str) -> Decimal:
    return sum((e.amount for e in store.credits if e.customer_id == customer_id), Decimal("0"))


def issue_credit(
    store: Store, customer_id: str, amount: Decimal, reason: str, order_id: str | None = None
) -> CreditEntry:
    """Compensate a customer. At most one credit per order."""
    if amount <= 0:
        raise ApiError(400, "A credit must be a positive amount")
    if order_id is not None and any(
        e.order_id == order_id and e.amount > 0 and e.reason != "RETURNED" for e in store.credits
    ):
        raise ApiError(409, "This order was already compensated", error="AlreadyCompensated")
    entry = CreditEntry(customer_id, amount, reason, order_id)
    store.credits.append(entry)
    store.publish("credit.issued", entry=entry)
    return entry


def apply_credit(store: Store, customer_id: str, order_id: str, gross: Decimal) -> Decimal:
    """Use what the customer has, up to the order's total; returns the amount applied."""
    available = balance(store, customer_id)
    applied = min(available, gross)
    if applied > 0:
        store.credits.append(CreditEntry(customer_id, -applied, "APPLIED", order_id))
    return applied


def return_credit(store: Store, customer_id: str, order_id: str, amount: Decimal) -> None:
    """A cancelled order gives its credit back."""
    if amount > 0:
        store.credits.append(CreditEntry(customer_id, amount, "RETURNED", order_id))


def ledger(store: Store, customer_id: str) -> dict[str, Any]:
    entries = sorted(
        (e for e in store.credits if e.customer_id == customer_id),
        key=lambda e: e.created_at,
        reverse=True,
    )
    return {
        "currency": CURRENCY,
        "balance": money(balance(store, customer_id)),
        "entries": [e.to_json() for e in entries],
    }


# --- routes (outside the Delivery Plus contract) -------------------------------------------------


class IssueRequest(BaseModel):
    customerId: str | None = None
    orderId: str | None = None
    amount: Decimal = Field(gt=0)
    reason: str = Field(min_length=1, max_length=200)


@router.get("/credits/me")
async def my_credits(user: CurrentUser, store: StoreDep) -> dict[str, Any]:
    return ledger(store, user.id)


@router.get("/credits/{customerId}")
async def customer_credits(customerId: str, user: CurrentUser, store: StoreDep) -> dict[str, Any]:
    if user.role != "ADMIN" and user.id != customerId:
        raise ApiError(403, "Support only")
    if customerId not in store.users:
        raise ApiError(404, "Customer not found")
    return ledger(store, customerId)


@router.post("/credits", status_code=201)
async def issue(body: IssueRequest, user: CurrentUser, store: StoreDep) -> dict[str, Any]:
    if user.role != "ADMIN":
        raise ApiError(403, "Support only")
    customer: User | None = None
    if body.orderId is not None:
        order = store.orders.get(body.orderId)
        if order is None:
            raise ApiError(404, "Order not found")
        customer = store.users.get(order.customer_id)
    elif body.customerId is not None:
        customer = store.users.get(body.customerId)
    if customer is None:
        raise ApiError(404, "Customer not found")
    return issue_credit(store, customer.id, body.amount, body.reason, body.orderId).to_json()
