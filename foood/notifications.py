"""Customer notifications: one per stage, phrased with the keys the customer app localises."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Query

from foood.auth import CurrentUser, StoreDep
from foood.catalog import page_of
from foood.deliveries import Delivery
from foood.drivers import Driver
from foood.errors import ApiError
from foood.models import money, new_id
from foood.orders import Order
from foood.payments import Payment, Refund
from foood.store import Store, iso, now

router = APIRouter(prefix="/api", tags=["notifications"])

#: messageKey -> (type, title, message template); {id} is the short order id
TEXTS: dict[str, tuple[str, str, str]] = {
    "payment.completed": (
        "PAYMENT_COMPLETED",
        "Payment received",
        "We received your payment for order {id}{card}.",
    ),
    "order.confirmed": (
        "ORDER_CONFIRMED",
        "Order confirmed",
        "Your order {id} has been confirmed.",
    ),
    "delivery.driver_assigned": (
        "DRIVER_ASSIGNED",
        "Driver assigned",
        "A driver is on the way to pick up your order {id}.",
    ),
    "delivery.picked_up": (
        "PICKED_UP",
        "Order picked up",
        "Your order {id} has been picked up and is on its way.",
    ),
    "delivery.delivered": (
        "DELIVERED",
        "Order delivered",
        "Your order {id} has been delivered. Enjoy!",
    ),
    "order.cancelled": ("ORDER_CANCELLED", "Order cancelled", "Your order {id} was cancelled."),
    "payment.failed": (
        "PAYMENT_FAILED",
        "Payment declined",
        "Your payment for order {id} was declined, so the order was not placed.",
    ),
    "payment.refunded": (
        "PAYMENT_REFUNDED",
        "Refund issued",
        "We refunded {amount} for your order {id}.",
    ),
    "order.replaced": (
        "ORDER_REPLACED",
        "Another kitchen has your order",
        "{restaurant} will make your order {id} instead: {reason}",
    ),
    "credit.issued": (
        "CREDIT_ISSUED",
        "Credit added",
        "We added {amount} EGP to your account: {reason}",
    ),
    "order.promise_moved": (
        "PROMISE_MOVED",
        "New delivery time",
        "Your order {id} is now expected by {time}: {reason}",
    ),
}


@dataclass
class Notification:
    user_id: str
    message_key: str
    params: dict[str, str]
    is_read: bool = False
    id: str = field(default_factory=new_id)
    created_at: datetime = field(default_factory=now)

    def to_json(self) -> dict[str, Any]:
        kind, title, template = TEXTS[self.message_key]
        short_id = self.params.get("orderId", "")[:8]
        message = template.format(
            id=short_id,
            amount=self.params.get("amount", ""),
            time=self.params.get("time", ""),
            reason=self.params.get("reason", ""),
            restaurant=self.params.get("restaurant", ""),
            card=f" with {self.params['card']}" if self.params.get("card") else "",
        )
        if self.message_key == "order.cancelled":
            who = (
                "Delivery Plus" if self.params.get("cancelledBy") == "SYSTEM" else "The restaurant"
            )
            note = self.params.get("note")
            message = f"{who} cancelled your order {short_id}"
            message += f": {note}" if note else "."
        return {
            "id": self.id,
            "userId": self.user_id,
            "type": kind,
            "title": title,
            "message": message,
            "messageKey": self.message_key,
            "params": dict(self.params),
            "isRead": self.is_read,
            "createdAt": iso(self.created_at),
        }


def notify(store: Store, user_id: str, message_key: str, **params: str | None) -> Notification:
    clean = {key: value for key, value in params.items() if value is not None}
    notification = Notification(user_id, message_key, clean)
    store.notifications.append(notification)
    return notification


# --- what each event means to the customer ---------------------------------------------------


def _on_order_status(store: Store, order: Order, previous: str) -> None:
    if order.status == "CONFIRMED":
        notify(store, order.customer_id, "order.confirmed", orderId=order.id)
    elif order.status == "CANCELLED" and order.cancelled_by != "PAYMENT":
        notify(
            store,
            order.customer_id,
            "order.cancelled",
            orderId=order.id,
            cancelledBy=order.cancelled_by,
            note=order.cancellation_note,
        )


def _on_payment(store: Store, payment: Payment) -> None:
    key = "payment.completed" if payment.status == "COMPLETED" else "payment.failed"
    card = payment.method["label"] if payment.method else None
    notify(store, payment.customer_id, key, orderId=payment.order_id, card=card)


def _on_refund(store: Store, payment: Payment, refund: Refund) -> None:
    notify(
        store,
        payment.customer_id,
        "payment.refunded",
        orderId=payment.order_id,
        amount=money(refund.amount),
    )


def _on_assigned(store: Store, delivery: Delivery, driver: Driver) -> None:
    notify(store, delivery.customer_id, "delivery.driver_assigned", orderId=delivery.order_id)


def _on_delivery_status(store: Store, delivery: Delivery) -> None:
    if delivery.status == "PICKED_UP":
        notify(store, delivery.customer_id, "delivery.picked_up", orderId=delivery.order_id)
    elif delivery.status == "DELIVERED":
        notify(store, delivery.customer_id, "delivery.delivered", orderId=delivery.order_id)


def _on_promise(store: Store, order: Order, reason: str) -> None:
    assert order.promised_at is not None
    notify(
        store,
        order.customer_id,
        "order.promise_moved",
        orderId=order.id,
        promisedAt=iso(order.promised_at),
        time=order.promised_at.strftime("%H:%M"),
        reason=reason,
    )


def _on_replaced(store: Store, original: Order, replacement: Order, reason: str) -> None:
    restaurant = store.restaurants.get(replacement.restaurant_id)
    notify(
        store,
        replacement.customer_id,
        "order.replaced",
        orderId=replacement.id,
        originalOrderId=original.id,
        restaurant=restaurant.name if restaurant else "Another kitchen",
        reason=reason,
    )


def _on_credit(store: Store, entry: Any) -> None:
    notify(
        store,
        entry.customer_id,
        "credit.issued",
        orderId=entry.order_id,
        amount=money(entry.amount),
        reason=entry.reason,
    )


def install(store: Store) -> None:
    store.subscribe("payment.processed", lambda payment: _on_payment(store, payment))
    store.subscribe("credit.issued", lambda entry: _on_credit(store, entry))
    store.subscribe(
        "order.replaced",
        lambda original, replacement, reason: _on_replaced(store, original, replacement, reason),
    )
    store.subscribe(
        "order.promise", lambda order, previous, reason: _on_promise(store, order, reason)
    )
    store.subscribe(
        "order.status", lambda order, previous: _on_order_status(store, order, previous)
    )
    store.subscribe("payment.refunded", lambda payment, refund: _on_refund(store, payment, refund))
    store.subscribe(
        "delivery.assigned", lambda delivery, driver: _on_assigned(store, delivery, driver)
    )
    store.subscribe("delivery.status", lambda delivery: _on_delivery_status(store, delivery))


# --- routes -----------------------------------------------------------------------------------


@router.get("/notifications")
async def list_notifications(
    user: CurrentUser,
    store: StoreDep,
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=50),
) -> dict[str, Any]:
    mine = [n for n in store.notifications if n.user_id == user.id]  # appended in event order
    mine.reverse()  # newest first, even when two stages share a timestamp
    return page_of([n.to_json() for n in mine], page, limit)


@router.patch("/notifications/read-all")
async def read_all(user: CurrentUser, store: StoreDep) -> dict[str, bool]:
    for notification in store.notifications:
        if notification.user_id == user.id:
            notification.is_read = True
    return {"success": True}


@router.patch("/notifications/{id}/read")
async def read_one(id: str, user: CurrentUser, store: StoreDep) -> dict[str, bool]:
    notification = next(
        (n for n in store.notifications if n.id == id and n.user_id == user.id), None
    )
    if notification is None:
        raise ApiError(404, "Notification not found")
    notification.is_read = True
    return {"success": True}
