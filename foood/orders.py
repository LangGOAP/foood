"""Orders: created from the cart, priced, cancelled, and worked by the kitchen.

The status machine follows the Delivery Plus apps' ``OrderStatus``::

    CREATED -> PAYMENT_PENDING -> CONFIRMED -> PREPARING -> READY_FOR_PICKUP
            -> DRIVER_ASSIGNED -> PICKED_UP -> DELIVERED      (+ CANCELLED, FAILED)

``transition`` is the only way a status changes; every change is published on
the store so dispatch, notifications and the scenario engine can react.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Header, Query
from pydantic import BaseModel, Field

from foood.auth import CurrentUser, StoreDep
from foood.cart import cart_changed, cart_of, reconcile
from foood.catalog import distance_km, owned_restaurant, page_of
from foood.errors import ApiError
from foood.models import Restaurant, money, new_id
from foood.store import Store, User, iso, now

router = APIRouter(prefix="/api", tags=["orders"])

DELIVERY_FEE = Decimal("3.00")
CURRENCY = "EGP"
DUPLICATE_WINDOW = timedelta(seconds=60)
#: the promise: the kitchen's prep time, a ride at city speed, and a margin (simulator constants)
RIDE_SPEED_KMH = 20.0
DEFAULT_RIDE_MINUTES = 9  # about 3 km when the drop-off has no coordinates yet
PROMISE_MARGIN_MINUTES = 5

PROGRESS = [
    "CREATED",
    "PAYMENT_PENDING",
    "CONFIRMED",
    "PREPARING",
    "READY_FOR_PICKUP",
    "DRIVER_ASSIGNED",
    "PICKED_UP",
    "DELIVERED",
]
TERMINAL = {"DELIVERED", "CANCELLED", "FAILED"}
TRANSITIONS: dict[str, set[str]] = {
    "CREATED": {"PAYMENT_PENDING", "CANCELLED"},
    "PAYMENT_PENDING": {"CONFIRMED", "CANCELLED", "FAILED"},
    "CONFIRMED": {"PREPARING", "CANCELLED"},
    "PREPARING": {"READY_FOR_PICKUP", "CANCELLED"},
    "READY_FOR_PICKUP": {"DRIVER_ASSIGNED", "CANCELLED"},
    "DRIVER_ASSIGNED": {"PICKED_UP", "READY_FOR_PICKUP", "CANCELLED"},  # back when a driver drops
    "PICKED_UP": {"DELIVERED", "CANCELLED"},
}
CUSTOMER_CANCELLABLE = {"CREATED", "PAYMENT_PENDING", "CONFIRMED"}
KITCHEN_MOVES = {"CONFIRMED": "PREPARING", "PREPARING": "READY_FOR_PICKUP"}
KITCHEN_CANCELLABLE = {"CONFIRMED", "PREPARING"}


@dataclass
class OrderItem:
    menu_item_id: str
    name: str
    price: Decimal
    quantity: int
    id: str = field(default_factory=new_id)

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "menuItemId": self.menu_item_id,
            "name": self.name,
            "price": money(self.price),
            "quantity": self.quantity,
        }


@dataclass
class Order:
    customer_id: str
    restaurant_id: str
    items: list[OrderItem]
    delivery_address: str
    delivery_notes: str | None = None
    delivery_latitude: float | None = None
    delivery_longitude: float | None = None
    status: str = "CREATED"
    payment_status: str = "PENDING"
    cancelled_by: str | None = None
    cancellation_reason: str | None = None
    cancellation_note: str | None = None
    credit_applied: Decimal = Decimal("0")
    replaces_order_id: str | None = None
    replaced_by_order_id: str | None = None
    promise_minutes: int = DEFAULT_RIDE_MINUTES + PROMISE_MARGIN_MINUTES
    promised_at: datetime | None = None
    promise_revisions: list[dict[str, str]] = field(default_factory=list)
    delivered_at: datetime | None = None
    id: str = field(default_factory=new_id)
    created_at: datetime = field(default_factory=now)
    updated_at: datetime = field(default_factory=now)

    def __post_init__(self) -> None:
        if self.promised_at is None:
            self.promised_at = self.created_at + timedelta(minutes=self.promise_minutes)

    @property
    def promise_breached(self) -> bool:
        """Past the promise while still on the way, or delivered after it. Never for cancelled."""
        assert self.promised_at is not None
        if self.status == "DELIVERED":
            return self.delivered_at is not None and self.delivered_at > self.promised_at
        if self.status in TERMINAL:
            return False
        return now() > self.promised_at

    @property
    def subtotal(self) -> Decimal:
        return sum((i.price * i.quantity for i in self.items), Decimal("0"))

    @property
    def gross(self) -> Decimal:
        return self.subtotal + DELIVERY_FEE

    @property
    def total(self) -> Decimal:
        """What the customer pays: the gross less the credit applied at checkout."""
        return self.gross - self.credit_applied

    def to_json(self, store: Store | None = None) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "customerId": self.customer_id,
            "restaurantId": self.restaurant_id,
            "status": self.status,
            "paymentStatus": self.payment_status,
            "items": [i.to_json() for i in self.items],
            "subtotalAmount": money(self.subtotal),
            "deliveryFee": money(DELIVERY_FEE),
            "grossAmount": money(self.gross),
            "creditApplied": money(self.credit_applied),
            "totalAmount": money(self.total),
            "deliveryAddress": self.delivery_address,
            "deliveryNotes": self.delivery_notes,
            "deliveryLatitude": self.delivery_latitude,
            "deliveryLongitude": self.delivery_longitude,
            "cancelledBy": self.cancelled_by,
            "cancellationReason": self.cancellation_reason,
            "cancellationNote": self.cancellation_note,
            "replacesOrderId": self.replaces_order_id,
            "replacedByOrderId": self.replaced_by_order_id,
            "promiseMinutes": self.promise_minutes,
            "promisedAt": iso(self.promised_at) if self.promised_at else None,
            "promiseBreached": self.promise_breached,
            "promiseRevisions": list(self.promise_revisions),
            "deliveredAt": iso(self.delivered_at) if self.delivered_at else None,
            "createdAt": iso(self.created_at),
            "updatedAt": iso(self.updated_at),
        }
        if store is not None:
            customer = store.users.get(self.customer_id)
            data["customerFirstName"] = customer.full_name.split()[0] if customer else None
        return data


# --- domain operations (also used by payments, dispatch and the scenario engine) ---


def promise_minutes(restaurant: Restaurant, latitude: float | None, longitude: float | None) -> int:
    """Prep time plus the ride at city speed (a default ride without coordinates) plus a margin."""
    if latitude is None or longitude is None:
        ride = DEFAULT_RIDE_MINUTES
    else:
        km = distance_km(restaurant.latitude, restaurant.longitude, latitude, longitude)
        ride = int(round(km / RIDE_SPEED_KMH * 60))
    return restaurant.prep_minutes + ride + PROMISE_MARGIN_MINUTES


def revise_promise(store: Store, order: Order, promised_at: datetime, reason: str) -> Order:
    """Move the promise (a replacement order, a re-dispatch) and tell the customer."""
    assert order.promised_at is not None
    previous = order.promised_at
    order.promised_at = promised_at.replace(microsecond=0)
    order.promise_revisions.append(
        {"from": iso(previous), "to": iso(order.promised_at), "reason": reason}
    )
    order.updated_at = now()
    store.publish("order.promise", order=order, previous=previous, reason=reason)
    return order


def get_order(store: Store, order_id: str) -> Order:
    order = store.orders.get(order_id)
    if order is None:
        raise ApiError(404, "Order not found")
    return order


def transition(store: Store, order: Order, status: str, **changes: Any) -> Order:
    if status not in TRANSITIONS.get(order.status, set()):
        raise ApiError(
            409, f"Cannot move an order from {order.status} to {status}", error="InvalidTransition"
        )
    previous = order.status
    order.status = status
    for attr, value in changes.items():
        setattr(order, attr, value)
    if status == "DELIVERED":
        order.delivered_at = now()
    if status in {"CANCELLED", "FAILED"} and order.credit_applied > 0:
        from foood.credits import return_credit

        return_credit(store, order.customer_id, order.id, order.credit_applied)
        order.credit_applied = Decimal("0")
    order.updated_at = now()
    store.publish("order.status", order=order, previous=previous)
    return order


def confirm_paid(store: Store, order_id: str) -> Order:
    """A completed payment confirms the order; the kitchen can start."""
    order = get_order(store, order_id)
    if order.status == "CREATED":
        transition(store, order, "PAYMENT_PENDING")
    order.payment_status = "COMPLETED"
    return transition(store, order, "CONFIRMED")


def fail_payment(store: Store, order_id: str, reason: str) -> Order:
    order = get_order(store, order_id)
    if order.status == "CREATED":
        transition(store, order, "PAYMENT_PENDING")
    order.payment_status = "FAILED"
    return transition(store, order, "CANCELLED", cancelled_by="PAYMENT", cancellation_reason=reason)


def place_order(
    store: Store,
    user: User,
    *,
    delivery_address: str | None,
    delivery_notes: str | None,
    latitude: float | None,
    longitude: float | None,
) -> Order:
    if user.phone_verified_at is None:
        raise ApiError(403, "Verify your phone number before ordering", error="PhoneNotVerified")
    cart = cart_of(store, user.id)
    if not cart.items or cart.restaurant_id is None:
        raise ApiError(400, "Your cart is empty")
    address = delivery_address or user.address
    if not address:
        raise ApiError(400, "A delivery address is required")
    restaurant = store.restaurants[cart.restaurant_id]
    if restaurant.status == "BUSY":
        raise ApiError(409, f"{restaurant.name} is too busy right now", error="RestaurantBusy")
    if restaurant.status != "OPEN":
        raise ApiError(409, f"{restaurant.name} is closed", error="RestaurantClosed")
    if latitude is not None and longitude is not None:
        away = distance_km(latitude, longitude, restaurant.latitude, restaurant.longitude)
        if away > restaurant.delivery_radius_km:
            raise ApiError(
                409, f"{restaurant.name} does not deliver there", error="OutOfDeliveryArea"
            )
    changes = reconcile(cart, store)
    if changes:
        raise cart_changed(changes)
    wanted = sorted((i.menu_item_id, i.quantity) for i in cart.items)
    for earlier in store.orders.values():
        if (
            earlier.customer_id == user.id
            and earlier.restaurant_id == restaurant.id
            and earlier.status in {"CREATED", "PAYMENT_PENDING"}
            and now() - earlier.created_at < DUPLICATE_WINDOW
            and sorted((i.menu_item_id, i.quantity) for i in earlier.items) == wanted
        ):
            raise ApiError(
                409,
                "You placed this exact order a moment ago and it is still waiting for payment",
                error="DuplicateOrder",
                details={"orderId": earlier.id},
            )
    order = Order(
        customer_id=user.id,
        restaurant_id=restaurant.id,
        items=[OrderItem(i.menu_item_id, i.name, i.price, i.quantity) for i in cart.items],
        delivery_address=address,
        delivery_notes=delivery_notes,
        delivery_latitude=latitude,
        delivery_longitude=longitude,
        promise_minutes=promise_minutes(restaurant, latitude, longitude),
    )
    from foood.credits import apply_credit

    order.credit_applied = apply_credit(store, user.id, order.id, order.gross)
    store.orders[order.id] = order
    cart.items.clear()
    cart.touch()
    store.publish("order.placed", order=order)
    return order


def is_today(moment: datetime) -> bool:
    return moment.date() == now().date()


# --- routes -------------------------------------------------------------------------


class CreateOrderRequest(BaseModel):
    deliveryAddress: str | None = Field(default=None, max_length=500)
    deliveryNotes: str | None = Field(default=None, max_length=500)
    deliveryLatitude: float | None = Field(default=None, ge=-90, le=90)
    deliveryLongitude: float | None = Field(default=None, ge=-180, le=180)


class StatusChange(BaseModel):
    status: str
    reason: str | None = Field(default=None, max_length=500)


@router.post("/orders", status_code=201)
async def create_order(
    body: CreateOrderRequest,
    user: CurrentUser,
    store: StoreDep,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    if idempotency_key:
        existing = store.idempotency.get((user.id, idempotency_key))
        if existing is not None:
            return get_order(store, existing).to_json()
    order = place_order(
        store,
        user,
        delivery_address=body.deliveryAddress,
        delivery_notes=body.deliveryNotes,
        latitude=body.deliveryLatitude,
        longitude=body.deliveryLongitude,
    )
    if idempotency_key:
        store.idempotency[(user.id, idempotency_key)] = order.id
    return order.to_json()


@router.get("/orders")
async def list_my_orders(
    user: CurrentUser,
    store: StoreDep,
    status: str | None = None,
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=50),
) -> dict[str, Any]:
    rows = [o for o in store.orders.values() if o.customer_id == user.id]
    if status == "active":
        rows = [o for o in rows if o.status not in TERMINAL]
    elif status == "completed":
        rows = [o for o in rows if o.status == "DELIVERED"]
    elif status == "cancelled":
        rows = [o for o in rows if o.status in {"CANCELLED", "FAILED"}]
    rows.sort(key=lambda o: o.created_at, reverse=True)
    return page_of([o.to_json() for o in rows], page, limit)


@router.get("/orders/quote")
async def quote(user: CurrentUser, store: StoreDep) -> dict[str, Any]:
    from foood.credits import balance

    cart = cart_of(store, user.id)
    fee = DELIVERY_FEE if cart.items else Decimal("0")
    gross = cart.subtotal + fee
    credit = min(balance(store, user.id), gross) if cart.items else Decimal("0")
    body: dict[str, Any] = {
        "subtotalAmount": money(cart.subtotal),
        "deliveryFee": money(fee),
        "creditApplied": money(credit),
        "totalAmount": money(gross - credit),
        "currency": CURRENCY,
    }
    if cart.restaurant_id is not None and cart.items:
        minutes = promise_minutes(store.restaurants[cart.restaurant_id], None, None)
        body["promiseMinutes"] = minutes
        body["promisedAt"] = iso(now().replace(microsecond=0) + timedelta(minutes=minutes))
    return body


@router.get("/orders/restaurant/{restaurantId}")
async def restaurant_orders(
    restaurantId: str,
    user: CurrentUser,
    store: StoreDep,
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=100),
) -> dict[str, Any]:
    owned_restaurant(store, user, restaurantId)
    rows = sorted(
        (o for o in store.orders.values() if o.restaurant_id == restaurantId),
        key=lambda o: o.created_at,
        reverse=True,
    )
    return page_of([o.to_json(store) for o in rows], page, limit)


@router.get("/orders/restaurant/{restaurantId}/summary")
async def restaurant_summary(
    restaurantId: str, user: CurrentUser, store: StoreDep
) -> dict[str, Any]:
    owned_restaurant(store, user, restaurantId)
    today = [
        o
        for o in store.orders.values()
        if o.restaurant_id == restaurantId and is_today(o.created_at)
    ]
    revenue = sum(
        (o.subtotal for o in today if o.status not in {"CANCELLED", "FAILED"}), Decimal("0")
    )
    return {
        "orders": len(today),
        "active": sum(o.status not in TERMINAL for o in today),
        "delivered": sum(o.status == "DELIVERED" for o in today),
        "cancelled": sum(o.status in {"CANCELLED", "FAILED"} for o in today),
        "revenue": money(revenue),
        "currency": CURRENCY,
    }


def _visible_to(order: Order, user: User, store: Store) -> bool:
    if user.role == "ADMIN" or order.customer_id == user.id:
        return True
    restaurant = store.restaurants.get(order.restaurant_id)
    return restaurant is not None and restaurant.owner_id == user.id


@router.get("/orders/{id}")
async def get_one(id: str, user: CurrentUser, store: StoreDep) -> dict[str, Any]:
    order = get_order(store, id)
    if not _visible_to(order, user, store):
        raise ApiError(403, "You do not have access to this order")
    return order.to_json(store if user.role != "CUSTOMER" else None)


@router.patch("/orders/{id}/status")
async def change_status(
    id: str, body: StatusChange, user: CurrentUser, store: StoreDep
) -> dict[str, Any]:
    order = get_order(store, id)
    if order.customer_id == user.id:
        if body.status != "CANCELLED":
            raise ApiError(403, "Customers can only cancel an order")
        if order.status not in CUSTOMER_CANCELLABLE:
            raise ApiError(409, "This order can no longer be cancelled", error="InvalidTransition")
        transition(
            store,
            order,
            "CANCELLED",
            cancelled_by="CUSTOMER",
            cancellation_reason="CUSTOMER_REQUEST",
            cancellation_note=body.reason,
        )
        return order.to_json()
    restaurant = store.restaurants.get(order.restaurant_id)
    if restaurant is None or (restaurant.owner_id != user.id and user.role != "ADMIN"):
        raise ApiError(403, "You do not have access to this order")
    if body.status == "CANCELLED":
        if order.status not in KITCHEN_CANCELLABLE:
            raise ApiError(
                409, "The kitchen can only reject a confirmed order", error="InvalidTransition"
            )
        transition(
            store,
            order,
            "CANCELLED",
            cancelled_by="RESTAURANT",
            cancellation_reason="RESTAURANT_REJECTED",
            cancellation_note=body.reason,
        )
    elif KITCHEN_MOVES.get(order.status) == body.status:
        transition(store, order, body.status)
    else:
        raise ApiError(
            409,
            f"Cannot move an order from {order.status} to {body.status}",
            error="InvalidTransition",
        )
    return order.to_json(store)
