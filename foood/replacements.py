"""Replacement orders: the same items from another kitchen when the first one fails.

A recovery action for any engine. ``alternatives`` ranks the open restaurants
that carry the order's items by name, inside the customer's delivery radius,
with the price difference and whether the customer's substitution policy lets
it go ahead without asking support. ``replace`` creates the new order with the
original payment carried over (a cheaper kitchen refunds the difference, a
dearer one is waived), cancels what is left of the original and tells the
customer. Nothing here decides; engines do.

Substitutes (#45) are the same idea one step further: when the item itself is gone,
whoever proposes candidate dishes (a model, a person) hands their names to
``substitutes``, which keeps only what an open kitchen within reach can make now
and prices each option as a replacement; ``offer_substitutes`` puts the priced
options to the customer, who accepts one or declines through the order's
``substitution`` route. Nothing not on an open menu is ever offered.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from foood.auth import CurrentUser, StoreDep
from foood.catalog import distance_km
from foood.deliveries import cancel_delivery, delivery_for_order
from foood.errors import ApiError
from foood.models import MenuItem, Restaurant, money, new_id
from foood.notifications import notify
from foood.orders import TERMINAL, Order, OrderItem, get_order, promise_minutes, transition
from foood.payments import Refund, payment_for_order
from foood.store import Store, User, iso, now

router = APIRouter(prefix="/api", tags=["replacements"])


@dataclass
class Alternative:
    restaurant: Restaurant
    items: list[tuple[OrderItem, MenuItem]]  # original line -> the other kitchen's item
    distance_km: float | None
    substitutions: list[tuple[str, str]] = field(default_factory=list)  # (asked for, offered)

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
                {"name": item.name, "quantity": line.quantity, "price": money(item.price)}
                for line, item in self.items
            ],
            "substitutes": [
                {"for": asked, "item": offered} for asked, offered in self.substitutions
            ],
        }


def _within_reach(store: Store, order: Order, restaurant: Restaurant) -> float | None | bool:
    """The distance to the door when the kitchen is open, approved and in range; False if not."""
    if restaurant.status != "OPEN" or restaurant.approval_status != "APPROVED":
        return False
    if order.delivery_latitude is None or order.delivery_longitude is None:
        return None
    away = distance_km(
        restaurant.latitude, restaurant.longitude, order.delivery_latitude, order.delivery_longitude
    )
    return False if away > restaurant.delivery_radius_km else away


def _menu(store: Store, restaurant: Restaurant) -> list[MenuItem]:
    return [
        i
        for i in store.menu_items.values()
        if i.restaurant_id == restaurant.id and i.available and not i.archived
    ]


STOP_WORDS = {"the", "a", "an", "with", "and", "of", "style"}


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.casefold()) if w not in STOP_WORDS}


def matches(candidate: str, item_name: str) -> bool:
    """A candidate names an item exactly, or one's words are all in the other's."""
    if candidate.casefold().strip() == item_name.casefold().strip():
        return True
    asked, offered = _words(candidate), _words(item_name)
    return bool(asked) and bool(offered) and (asked <= offered or offered <= asked)


def candidate_pool(store: Store, order: Order) -> list[dict[str, Any]]:
    """What open kitchens within reach can make now, minus the order's own sold-out items: the
    list a proposer (a model with its culinary knowledge, a person) picks substitutes from."""
    gone = {
        line.name.casefold()
        for line in order.items
        if not any(
            i.name.casefold() == line.name.casefold()
            for i in _menu(store, store.restaurants[order.restaurant_id])
        )
    }
    pool: list[dict[str, Any]] = []
    for restaurant in store.restaurants.values():
        reach = _within_reach(store, order, restaurant)
        if reach is False:
            continue
        for item in sorted(_menu(store, restaurant), key=lambda i: i.name):
            if restaurant.id == order.restaurant_id and item.name.casefold() in gone:
                continue
            pool.append(
                {
                    "restaurantId": restaurant.id,
                    "restaurant": restaurant.name,
                    "item": item.name,
                    "price": money(item.price),
                }
            )
    return pool


def substitutes(store: Store, order: Order, candidates: list[str]) -> list[Alternative]:
    """Kitchens that can make the order with the candidates standing in for what they lack.

    A line the kitchen still carries maps to itself; a line it lacks maps to the cheapest of
    its items that a candidate names; a kitchen that cannot cover every line is left out. The
    order's own kitchen counts (a substitute from the same kitchen spares a new delivery).
    """
    found: list[Alternative] = []
    for restaurant in store.restaurants.values():
        reach = _within_reach(store, order, restaurant)
        if reach is False:
            continue
        menu = _menu(store, restaurant)
        by_name = {i.name.casefold(): i for i in menu}
        mapped: list[tuple[OrderItem, MenuItem]] = []
        swapped: list[tuple[str, str]] = []
        for line in order.items:
            item = by_name.get(line.name.casefold())
            if item is None:
                options = [i for i in menu if any(matches(c, i.name) for c in candidates)]
                if not options:
                    break
                item = min(options, key=lambda i: (i.price, i.name))
                swapped.append((line.name, item.name))
            mapped.append((line, item))
        else:
            if restaurant.id == order.restaurant_id and not swapped:
                continue  # nothing to offer from the kitchen that already has the order
            found.append(Alternative(restaurant, mapped, None if reach is None else reach, swapped))
    found.sort(key=lambda a: (a.subtotal, a.distance_km or 0.0, a.restaurant.name))
    return found


@dataclass
class Offer:
    """Priced substitutes put to the customer; one open offer per order at a time."""

    order_id: str
    reason: str
    options: list[Alternative]
    status: str = "OPEN"  # OPEN | ACCEPTED | DECLINED
    choice: int | None = None
    answered_by: str | None = None
    answered_at: datetime | None = None
    id: str = field(default_factory=new_id)
    offered_at: datetime = field(default_factory=now)

    @property
    def chosen(self) -> Alternative | None:
        return self.options[self.choice] if self.choice is not None else None

    def to_json(self, order: Order, customer: User) -> dict[str, Any]:
        return {
            "id": self.id,
            "orderId": self.order_id,
            "reason": self.reason,
            "status": self.status,
            "choice": self.choice,
            "options": [
                {"optionId": index, **option.to_json(order, customer)}
                for index, option in enumerate(self.options)
            ],
            "offeredAt": iso(self.offered_at),
            "answeredAt": iso(self.answered_at) if self.answered_at else None,
        }


def offer_substitutes(store: Store, order: Order, options: list[Alternative], reason: str) -> Offer:
    """Put priced substitutes to the customer: a notification, an open offer, an event."""
    if not options:
        raise ValueError("an offer needs at least one option")
    open_offer = store.offers.get(order.id)
    if open_offer is not None and open_offer.status == "OPEN":
        raise ValueError("the customer already has an open offer on this order")
    offer = Offer(order.id, reason, list(options))
    store.offers[order.id] = offer
    notify(
        store,
        order.customer_id,
        "order.substitution_offered",
        orderId=order.id,
        reason=reason,
        options=str(len(options)),
    )
    store.publish("order.substitution_offered", order=order, offer=offer)
    return offer


def answer_substitution(store: Store, order: Order, choice: int | None, by: str) -> Offer:
    """The customer's answer: the index of the option taken, or None to decline."""
    offer: Offer | None = store.offers.get(order.id)
    if offer is None:
        raise ValueError("no substitution was offered on this order")
    if offer.status != "OPEN":
        raise ApiError(409, "This offer was already answered", error="Conflict")
    if choice is not None and not 0 <= choice < len(offer.options):
        raise ApiError(400, "No such option", error="Bad Request")
    offer.choice = choice
    offer.status = "DECLINED" if choice is None else "ACCEPTED"
    offer.answered_by = by
    offer.answered_at = now()
    store.publish("order.substitution_answered", order=order, offer=offer, alternative=offer.chosen)
    return offer


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


class SubstitutionAnswer(BaseModel):
    accept: int | None = None
    decline: bool = False


def _party(order: Order, user: User, store: Store) -> None:
    restaurant = store.restaurants.get(order.restaurant_id)
    owner = restaurant is not None and restaurant.owner_id == user.id
    if not (user.role == "ADMIN" or order.customer_id == user.id or owner):
        raise ApiError(403, "You do not have access to this order")


@router.get("/orders/{id}/substitution")
async def show_substitution(id: str, user: CurrentUser, store: StoreDep) -> dict[str, Any]:
    order = get_order(store, id)
    _party(order, user, store)
    offer: Offer | None = store.offers.get(order.id)
    if offer is None:
        raise ApiError(404, "No substitution was offered on this order", error="NotFound")
    return offer.to_json(order, store.users[order.customer_id])


@router.post("/orders/{id}/substitution")
async def answer_substitution_route(
    id: str, body: SubstitutionAnswer, user: CurrentUser, store: StoreDep
) -> dict[str, Any]:
    order = get_order(store, id)
    if not (user.role == "ADMIN" or order.customer_id == user.id):
        raise ApiError(403, "Only the customer answers")
    if body.accept is None and not body.decline:
        raise ApiError(400, "Say which option you accept, or decline", error="Bad Request")
    if store.offers.get(order.id) is None:
        raise ApiError(404, "No substitution was offered on this order", error="NotFound")
    offer = answer_substitution(store, order, None if body.decline else body.accept, by=user.id)
    return offer.to_json(order, store.users[order.customer_id])
