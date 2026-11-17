"""Replacement orders: the same items from another kitchen when the first one fails.

A recovery action for any engine. ``alternatives`` ranks the open restaurants
that carry the order's items by name, inside the customer's delivery radius,
with the price difference and whether the customer's substitution policy lets
it go ahead without asking support. ``replace`` creates the new order with the
original payment carried over (a cheaper kitchen refunds the difference, a
dearer one is waived), cancels what is left of the original and tells the
customer. Nothing here decides; engines do.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from foood.auth import CurrentUser, StoreDep
from foood.catalog import distance_km
from foood.deliveries import cancel_delivery, delivery_for_order
from foood.errors import ApiError
from foood.models import MenuItem, Restaurant, money
from foood.orders import TERMINAL, Order, OrderItem, get_order, promise_minutes, transition
from foood.payments import Refund, payment_for_order
from foood.store import Store, User, now

router = APIRouter(prefix="/api", tags=["replacements"])


@dataclass
class Alternative:
    restaurant: Restaurant
    items: list[tuple[OrderItem, MenuItem]]  # original line -> the other kitchen's item
    distance_km: float | None

    @property
    def subtotal(self) -> Decimal:
        return sum((item.price * line.quantity for line, item in self.items), Decimal("0"))

    def difference(self, original: Order) -> Decimal:
        return self.subtotal - original.subtotal

    def needs_approval(self, original: Order, customer: User) -> bool:
        policy = customer.substitution_policy or {}
        if not policy.get("allow"):
            return True
        increase = self.difference(original)
        if increase <= 0:
            return False
        limit = original.subtotal * Decimal(str(policy.get("max_increase_pct", 0))) / 100
        return increase > limit

    def to_json(self, original: Order, customer: User) -> dict[str, Any]:
        return {
            "restaurantId": self.restaurant.id,
            "restaurantName": self.restaurant.name,
            "distanceKm": round(self.distance_km, 2) if self.distance_km is not None else None,
            "prepMinutes": self.restaurant.prep_minutes,
            "subtotalAmount": money(self.subtotal),
            "difference": money(self.difference(original)),
            "needsApproval": self.needs_approval(original, customer),
            "items": [
                {"name": line.name, "quantity": line.quantity, "price": money(item.price)}
                for line, item in self.items
            ],
        }


def alternatives(store: Store, order: Order) -> list[Alternative]:
    """Open kitchens with every item of the order on the menu, nearest and cheapest first."""
    found: list[Alternative] = []
    for restaurant in store.restaurants.values():
        if restaurant.id == order.restaurant_id or restaurant.status != "OPEN":
            continue
        if restaurant.approval_status != "APPROVED":
            continue
        away: float | None = None
        if order.delivery_latitude is not None and order.delivery_longitude is not None:
            away = distance_km(
                restaurant.latitude,
                restaurant.longitude,
                order.delivery_latitude,
                order.delivery_longitude,
            )
            if away > restaurant.delivery_radius_km:
                continue
        menu = {
            i.name.casefold(): i
            for i in store.menu_items.values()
            if i.restaurant_id == restaurant.id and i.available and not i.archived
        }
        mapped = []
        for line in order.items:
            item = menu.get(line.name.casefold())
            if item is None:
                break
            mapped.append((line, item))
        else:
            found.append(Alternative(restaurant, mapped, away))
    found.sort(key=lambda a: (a.subtotal, a.distance_km or 0.0, a.restaurant.name))
    return found


def replace(store: Store, order: Order, alternative: Alternative, reason: str) -> Order:
    """Create the replacement, carry the payment over, close out the original."""
    if order.replaced_by_order_id is not None:
        raise ApiError(409, "This order was already replaced", error="Conflict")
    if order.status == "DELIVERED" or order.status in {"PICKED_UP"}:
        raise ApiError(409, f"A {order.status} order cannot be replaced", error="InvalidTransition")
    payment = payment_for_order(store, order.id)
    if payment is None or payment.status != "COMPLETED":
        raise ApiError(409, "Only a paid order can be replaced", error="NotPaid")
    replacement = Order(
        customer_id=order.customer_id,
        restaurant_id=alternative.restaurant.id,
        items=[
            OrderItem(item.id, item.name, item.price, line.quantity)
            for line, item in alternative.items
        ],
        delivery_address=order.delivery_address,
        delivery_notes=order.delivery_notes,
        delivery_latitude=order.delivery_latitude,
        delivery_longitude=order.delivery_longitude,
        replaces_order_id=order.id,
        promise_minutes=promise_minutes(
            alternative.restaurant, order.delivery_latitude, order.delivery_longitude
        ),
    )
    store.orders[replacement.id] = replacement
    # the money follows the food
    payment.order_id = replacement.id
    payment.updated_at = now()
    difference = alternative.difference(order)
    if difference < 0:
        store.refunds[payment.id] = Refund(
            payment.id, order.id, -difference, "REPLACEMENT_DIFFERENCE"
        )
    replacement.payment_status = "COMPLETED"
    transition(store, replacement, "PAYMENT_PENDING")
    transition(store, replacement, "CONFIRMED")
    # what is left of the original
    order.replaced_by_order_id = replacement.id
    delivery = delivery_for_order(store, order.id)
    if delivery is not None and delivery.status not in {"DELIVERED", "CANCELLED"}:
        cancel_delivery(store, delivery, reason, "SYSTEM")
    if order.status not in TERMINAL:
        transition(
            store,
            order,
            "CANCELLED",
            cancelled_by="SYSTEM",
            cancellation_reason="REPLACED",
            cancellation_note=reason,
        )
    store.publish("order.replaced", original=order, replacement=replacement, reason=reason)
    return replacement


# --- routes (outside the Delivery Plus contract; support and engines) -------------------------


class ReplaceRequest(BaseModel):
    restaurantId: str | None = None
    reason: str = "The kitchen could not make your order"


def _support_only(user: User) -> None:
    if user.role != "ADMIN":
        raise ApiError(403, "Support only")


@router.get("/orders/{id}/alternatives")
async def list_alternatives(id: str, user: CurrentUser, store: StoreDep) -> dict[str, Any]:
    _support_only(user)
    order = get_order(store, id)
    customer = store.users[order.customer_id]
    return {"items": [a.to_json(order, customer) for a in alternatives(store, order)]}


@router.post("/orders/{id}/replacement", status_code=201)
async def create_replacement(
    id: str, body: ReplaceRequest, user: CurrentUser, store: StoreDep
) -> dict[str, Any]:
    _support_only(user)
    order = get_order(store, id)
    options = alternatives(store, order)
    if body.restaurantId is not None:
        options = [a for a in options if a.restaurant.id == body.restaurantId]
    if not options:
        raise ApiError(409, "No other kitchen can make this order", error="NoAlternative")
    return replace(store, order, options[0], body.reason).to_json()
