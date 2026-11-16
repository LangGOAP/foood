"""Deliveries, dispatch, the driver's run, earnings and ratings.

The order engine creates a delivery when the kitchen marks an order
READY_FOR_PICKUP and calls ``dispatch_pending`` to hand it to the longest-idle
verified driver who is online. When nobody is, it waits (the customer sees
"Finding a driver") and the next driver to go online takes it. The ``dispatch``
hook on the store lets the scenario engine, or a planner, choose differently.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Query, Response
from pydantic import BaseModel, Field

from foood.auth import CurrentUser, StoreDep
from foood.catalog import page_of
from foood.drivers import Driver, available_drivers, driver_of
from foood.errors import ApiError
from foood.models import money, new_id
from foood.orders import DELIVERY_FEE, Order, get_order, transition
from foood.store import Store, User, iso, now

router = APIRouter(prefix="/api", tags=["deliveries"])

ACTIVE = {"DRIVER_ASSIGNED", "PICKED_UP", "IN_TRANSIT"}
NEXT_ACTION = {"DRIVER_ASSIGNED": "pickup", "PICKED_UP": "start", "IN_TRANSIT": "complete"}
ACTION_RESULT = {"pickup": "PICKED_UP", "start": "IN_TRANSIT", "complete": "DELIVERED"}
DRIVER_PAY = DELIVERY_FEE  # the delivery fee goes to the driver
SETTLEMENT_HOURS = 24
RATING_WINDOW = timedelta(days=7)


@dataclass
class Delivery:
    order_id: str
    restaurant_id: str
    customer_id: str
    driver_id: str | None = None
    status: str = "CREATED"
    assigned_at: datetime | None = None
    picked_up_at: datetime | None = None
    delivered_at: datetime | None = None
    cancelled_at: datetime | None = None
    id: str = field(default_factory=new_id)
    created_at: datetime = field(default_factory=now)
    updated_at: datetime = field(default_factory=now)

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "orderId": self.order_id,
            "restaurantId": self.restaurant_id,
            "customerId": self.customer_id,
            "driverId": self.driver_id,
            "status": self.status,
            "assignedAt": iso(self.assigned_at) if self.assigned_at else None,
            "pickedUpAt": iso(self.picked_up_at) if self.picked_up_at else None,
            "deliveredAt": iso(self.delivered_at) if self.delivered_at else None,
            "cancelledAt": iso(self.cancelled_at) if self.cancelled_at else None,
            "createdAt": iso(self.created_at),
            "updatedAt": iso(self.updated_at),
        }


@dataclass
class Earning:
    driver_id: str
    delivery_id: str
    order_id: str
    type: str  # DELIVERY_EARNING | CANCELLATION_COMPENSATION | REVERSAL
    amount: Decimal
    id: str = field(default_factory=new_id)
    created_at: datetime = field(default_factory=now)

    @property
    def available_at(self) -> datetime:
        return self.created_at + timedelta(hours=SETTLEMENT_HOURS)

    def status(self, at: datetime) -> str:
        return "AVAILABLE" if self.available_at <= at else "PENDING"

    def to_json(self, at: datetime) -> dict[str, Any]:
        return {
            "id": self.id,
            "deliveryId": self.delivery_id,
            "orderId": self.order_id,
            "type": self.type,
            "amount": money(self.amount),
            "status": self.status(at),
            "availableAt": iso(self.available_at),
            "createdAt": iso(self.created_at),
        }


@dataclass
class Rating:
    delivery_id: str
    driver_id: str
    customer_id: str
    score: int
    comment: str | None
    created_at: datetime = field(default_factory=now)

    def to_json(self) -> dict[str, Any]:
        return {"score": self.score, "comment": self.comment, "createdAt": iso(self.created_at)}


# --- domain -----------------------------------------------------------------------------


def delivery_for_order(store: Store, order_id: str) -> Delivery | None:
    return next((d for d in store.deliveries.values() if d.order_id == order_id), None)


def current_delivery(store: Store, driver: Driver) -> Delivery | None:
    return next(
        (d for d in store.deliveries.values() if d.driver_id == driver.id and d.status in ACTIVE),
        None,
    )


def create_delivery(store: Store, order: Order) -> Delivery:
    if delivery_for_order(store, order.id) is not None:
        raise ApiError(409, "This order already has a delivery")
    delivery = Delivery(order.id, order.restaurant_id, order.customer_id)
    store.deliveries[delivery.id] = delivery
    store.publish("delivery.created", delivery=delivery)
    return delivery


def assign(store: Store, delivery: Delivery, driver: Driver) -> Delivery:
    if delivery.status != "CREATED":
        raise ApiError(409, "This delivery already has a driver", error="InvalidTransition")
    if driver.status != "AVAILABLE" or driver.verification_status != "VERIFIED":
        raise ApiError(409, "That driver is not available", error="DriverUnavailable")
    delivery.driver_id = driver.id
    delivery.status = "DRIVER_ASSIGNED"
    delivery.assigned_at = delivery.updated_at = now()
    driver.set_status("BUSY")
    order = get_order(store, delivery.order_id)
    if order.status == "READY_FOR_PICKUP":
        transition(store, order, "DRIVER_ASSIGNED")
    store.publish("delivery.assigned", delivery=delivery, driver=driver)
    return delivery


def choose_driver(store: Store, delivery: Delivery) -> Driver | None:
    """Longest-idle first, unless a ``dispatch`` hook decides otherwise."""
    hook = store.hooks.get("dispatch")
    if hook is not None:
        chosen: Driver | None = hook(delivery, available_drivers(store))
        return chosen
    queue = available_drivers(store)
    return queue[0] if queue else None


def dispatch_pending(store: Store) -> list[tuple[Delivery, Driver]]:
    """Hand every waiting delivery to a driver while there are drivers; returns the pairs."""
    waiting = sorted(
        (d for d in store.deliveries.values() if d.status == "CREATED"), key=lambda d: d.created_at
    )
    assigned: list[tuple[Delivery, Driver]] = []
    for delivery in waiting:
        driver = choose_driver(store, delivery)
        if driver is None:
            break
        assign(store, delivery, driver)
        assigned.append((delivery, driver))
    return assigned


def _release(store: Store, delivery: Delivery) -> None:
    if delivery.driver_id is None:
        return
    driver = store.drivers.get(delivery.driver_id)
    if driver is not None and driver.status == "BUSY":
        driver.set_status("AVAILABLE")
        store.publish("driver.available", driver=driver)


def perform(store: Store, delivery: Delivery, action: str) -> Delivery:
    expected = NEXT_ACTION.get(delivery.status)
    if expected != action:
        raise ApiError(
            409, f"A {delivery.status} delivery cannot {action}", error="InvalidTransition"
        )
    moment = now()
    delivery.status = ACTION_RESULT[action]
    delivery.updated_at = moment
    order = get_order(store, delivery.order_id)
    if action == "pickup":
        delivery.picked_up_at = moment
        transition(store, order, "PICKED_UP")
    elif action == "complete":
        delivery.delivered_at = moment
        transition(store, order, "DELIVERED")
        assert delivery.driver_id is not None
        store.earnings.append(
            Earning(delivery.driver_id, delivery.id, order.id, "DELIVERY_EARNING", DRIVER_PAY)
        )
        _release(store, delivery)
    store.publish("delivery.status", delivery=delivery)
    return delivery


def cancel_delivery(store: Store, delivery: Delivery, reason: str | None, by: str) -> Delivery:
    if delivery.status in {"DELIVERED", "CANCELLED"}:
        raise ApiError(
            409, f"A {delivery.status} delivery cannot be cancelled", error="InvalidTransition"
        )
    past_pickup = delivery.status in {"PICKED_UP", "IN_TRANSIT"}
    delivery.status = "CANCELLED"
    delivery.cancelled_at = delivery.updated_at = now()
    if past_pickup and delivery.driver_id is not None:
        store.earnings.append(
            Earning(
                delivery.driver_id,
                delivery.id,
                delivery.order_id,
                "CANCELLATION_COMPENSATION",
                DRIVER_PAY,
            )
        )
    _release(store, delivery)
    order = get_order(store, delivery.order_id)
    if order.status not in {"CANCELLED", "DELIVERED", "FAILED"}:
        transition(
            store,
            order,
            "CANCELLED",
            cancelled_by=by,
            cancellation_reason="DELIVERY_CANCELLED",
            cancellation_note=reason,
        )
    store.publish("delivery.status", delivery=delivery)
    return delivery


def drop(store: Store, delivery: Delivery, reason: str | None = None) -> Delivery:
    """The driver walks away. Before pickup the delivery is back to CREATED and the
    driver offline; after pickup the food is gone with them, so the delivery is cancelled
    and the driver compensated. What becomes of the order is the engine's decision."""
    if delivery.status not in ACTIVE or delivery.driver_id is None:
        raise ApiError(
            409, f"A {delivery.status} delivery has no driver to drop", error="InvalidTransition"
        )
    driver = store.drivers[delivery.driver_id]
    moment = now()
    if delivery.status == "DRIVER_ASSIGNED":
        delivery.driver_id = None
        delivery.assigned_at = None
        delivery.status = "CREATED"
        delivery.updated_at = moment
        driver.set_status("OFFLINE")
        order = get_order(store, delivery.order_id)
        if order.status == "DRIVER_ASSIGNED":
            transition(store, order, "READY_FOR_PICKUP")
        stage = "before_pickup"
    else:
        delivery.status = "CANCELLED"
        delivery.cancelled_at = delivery.updated_at = moment
        store.earnings.append(
            Earning(
                driver.id, delivery.id, delivery.order_id, "CANCELLATION_COMPENSATION", DRIVER_PAY
            )
        )
        driver.set_status("OFFLINE")
        stage = "after_pickup"
    store.publish("delivery.status", delivery=delivery)
    store.publish("delivery.dropped", delivery=delivery, driver=driver, reason=reason, stage=stage)
    return delivery


# --- views ----------------------------------------------------------------------------------


def for_order_view(store: Store, delivery: Delivery, viewer: User) -> dict[str, Any]:
    driver = store.drivers.get(delivery.driver_id) if delivery.driver_id else None
    driver_user = store.users.get(driver.user_id) if driver else None
    reveal_phone = (
        viewer.id == delivery.customer_id and delivery.status in ACTIVE and driver_user is not None
    )
    return {
        **delivery.to_json(),
        "driver": driver.card(driver_user) if driver else None,
        "driverPhone": driver_user.phone if reveal_phone and driver_user else None,
    }


def current_view(store: Store, delivery: Delivery) -> dict[str, Any]:
    order = get_order(store, delivery.order_id)
    restaurant = store.restaurants[delivery.restaurant_id]
    customer = store.users.get(delivery.customer_id)
    return {
        "id": delivery.id,
        "status": delivery.status,
        "orderId": delivery.order_id,
        "createdAt": iso(delivery.created_at),
        "updatedAt": iso(delivery.updated_at),
        "pickup": {
            "restaurantId": restaurant.id,
            "name": restaurant.name,
            "address": restaurant.address,
        },
        "dropOff": {
            "address": order.delivery_address,
            "notes": order.delivery_notes,
            "latitude": order.delivery_latitude,
            "longitude": order.delivery_longitude,
        },
        "order": {
            "id": order.id,
            "items": [{"name": i.name, "quantity": i.quantity} for i in order.items],
            "totalAmount": money(order.total),
        },
        "nextActions": [NEXT_ACTION[delivery.status]] if delivery.status in NEXT_ACTION else [],
        "customerPhone": customer.phone if customer else None,
        "restaurantPhone": None,
    }


def history_view(store: Store, delivery: Delivery) -> dict[str, Any]:
    order = store.orders.get(delivery.order_id)
    restaurant = store.restaurants.get(delivery.restaurant_id)
    return {
        **delivery.to_json(),
        "restaurant": {"id": restaurant.id, "name": restaurant.name} if restaurant else None,
        "order": (
            {
                "items": [{"name": i.name, "quantity": i.quantity} for i in order.items],
                "totalAmount": money(order.total),
                "dropOffAddress": order.delivery_address,
            }
            if order
            else None
        ),
    }


def rating_status(store: Store, delivery: Delivery) -> dict[str, Any]:
    rating = store.ratings.get(delivery.id)
    closes_at = delivery.delivered_at + RATING_WINDOW if delivery.delivered_at else None
    if rating is not None:
        return {
            "rating": rating.to_json(),
            "canRate": False,
            "reason": "ALREADY_RATED",
            "closesAt": None,
        }
    if delivery.status != "DELIVERED" or closes_at is None:
        return {"rating": None, "canRate": False, "reason": "NOT_DELIVERED", "closesAt": None}
    if now() > closes_at:
        return {
            "rating": None,
            "canRate": False,
            "reason": "WINDOW_CLOSED",
            "closesAt": iso(closes_at),
        }
    return {"rating": None, "canRate": True, "reason": None, "closesAt": iso(closes_at)}


# --- routes ---------------------------------------------------------------------------------


class CreateDelivery(BaseModel):
    orderId: str


class Assign(BaseModel):
    driverId: str


class Cancel(BaseModel):
    reason: str | None = Field(default=None, max_length=200)


class RatingRequest(BaseModel):
    score: int = Field(ge=1, le=5)
    comment: str | None = Field(default=None, max_length=500)


def _get(store: Store, delivery_id: str) -> Delivery:
    delivery = store.deliveries.get(delivery_id)
    if delivery is None:
        raise ApiError(404, "Delivery not found")
    return delivery


def _may_view(store: Store, delivery: Delivery, user: User) -> bool:
    if user.role == "ADMIN" or user.id == delivery.customer_id:
        return True
    driver = driver_of(store, user.id)
    if driver is not None and driver.id == delivery.driver_id:
        return True
    restaurant = store.restaurants.get(delivery.restaurant_id)
    return restaurant is not None and restaurant.owner_id == user.id


def _my_driver(store: Store, user: User) -> Driver:
    driver = driver_of(store, user.id)
    if driver is None:
        raise ApiError(403, "You are not a driver")
    return driver


@router.post("/deliveries", status_code=201)
async def create(body: CreateDelivery, user: CurrentUser, store: StoreDep) -> dict[str, Any]:
    if user.role != "ADMIN":
        raise ApiError(403, "Only administrators can create a delivery by hand")
    order = get_order(store, body.orderId)
    delivery = create_delivery(store, order)
    dispatch_pending(store)
    return delivery.to_json()


@router.get("/deliveries/me/current")
async def my_current(user: CurrentUser, store: StoreDep) -> Any:
    delivery = current_delivery(store, _my_driver(store, user))
    if delivery is None:
        return Response(status_code=204)
    return current_view(store, delivery)


@router.get("/deliveries/me/history")
async def my_history(
    user: CurrentUser,
    store: StoreDep,
    status: str | None = None,
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=50),
) -> dict[str, Any]:
    driver = _my_driver(store, user)
    rows = [d for d in store.deliveries.values() if d.driver_id == driver.id]
    if status == "active":
        rows = [d for d in rows if d.status in ACTIVE]
    elif status == "completed":
        rows = [d for d in rows if d.status == "DELIVERED"]
    elif status == "cancelled":
        rows = [d for d in rows if d.status == "CANCELLED"]
    rows.sort(key=lambda d: d.updated_at, reverse=True)
    return page_of([history_view(store, d) for d in rows], page, limit)


@router.get("/deliveries/me/earnings")
async def my_earnings(
    user: CurrentUser,
    store: StoreDep,
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=50),
) -> dict[str, Any]:
    driver = _my_driver(store, user)
    at = now()
    entries = sorted(
        (e for e in store.earnings if e.driver_id == driver.id),
        key=lambda e: e.created_at,
        reverse=True,
    )
    pending = sum((e.amount for e in entries if e.status(at) == "PENDING"), Decimal("0"))
    available = sum((e.amount for e in entries if e.status(at) == "AVAILABLE"), Decimal("0"))
    return {
        "currency": "EGP",
        "pending": money(pending),
        "available": money(available),
        "balance": money(pending + available),
        "settlementHours": SETTLEMENT_HOURS,
        **page_of([e.to_json(at) for e in entries], page, limit),
        "entries": [e.to_json(at) for e in entries][(page - 1) * limit : page * limit],
    }


@router.get("/deliveries/me/rating-summary")
async def my_rating_summary(user: CurrentUser, store: StoreDep) -> dict[str, Any]:
    driver = _my_driver(store, user)
    mine = sorted(
        (r for r in store.ratings.values() if r.driver_id == driver.id),
        key=lambda r: r.created_at,
        reverse=True,
    )
    average = round(sum(r.score for r in mine) / len(mine), 2) if mine else None
    return {
        "average": average,
        "count": len(mine),
        "recentComments": [r.to_json() for r in mine if r.comment][:5],
    }


@router.get("/deliveries/by-order/{orderId}")
async def by_order(orderId: str, user: CurrentUser, store: StoreDep) -> dict[str, Any]:
    delivery = delivery_for_order(store, orderId)
    if delivery is None:
        raise ApiError(404, "No delivery for this order yet")
    if not _may_view(store, delivery, user):
        raise ApiError(403, "You do not have access to this order")
    return for_order_view(store, delivery, user)


@router.get("/deliveries/{id}")
async def get_one(id: str, user: CurrentUser, store: StoreDep) -> dict[str, Any]:
    delivery = _get(store, id)
    if not _may_view(store, delivery, user):
        raise ApiError(403, "You do not have access to this delivery")
    return for_order_view(store, delivery, user)


@router.post("/deliveries/{id}/assign", status_code=201)
async def assign_by_hand(
    id: str, body: Assign, user: CurrentUser, store: StoreDep
) -> dict[str, Any]:
    if user.role != "ADMIN":
        raise ApiError(403, "Only administrators can assign a delivery by hand")
    driver = store.drivers.get(body.driverId)
    if driver is None:
        raise ApiError(404, "Driver not found")
    return assign(store, _get(store, id), driver).to_json()


@router.post("/deliveries/{id}/pickup", status_code=201)
async def pickup(id: str, user: CurrentUser, store: StoreDep) -> dict[str, Any]:
    return _act(store, user, id, "pickup")


@router.post("/deliveries/{id}/start", status_code=201)
async def start(id: str, user: CurrentUser, store: StoreDep) -> dict[str, Any]:
    return _act(store, user, id, "start")


@router.post("/deliveries/{id}/complete", status_code=201)
async def complete(id: str, user: CurrentUser, store: StoreDep) -> dict[str, Any]:
    return _act(store, user, id, "complete")


def _act(store: Store, user: User, delivery_id: str, action: str) -> dict[str, Any]:
    delivery = _get(store, delivery_id)
    driver = driver_of(store, user.id)
    if user.role != "ADMIN" and (driver is None or driver.id != delivery.driver_id):
        raise ApiError(403, "This is not your delivery")
    return perform(store, delivery, action).to_json()


@router.post("/deliveries/{id}/cancel", status_code=201)
async def cancel(id: str, body: Cancel, user: CurrentUser, store: StoreDep) -> dict[str, Any]:
    if user.role != "ADMIN":
        raise ApiError(403, "Only administrators can cancel a delivery")
    return cancel_delivery(store, _get(store, id), body.reason, "SYSTEM").to_json()


@router.get("/deliveries/{id}/rating")
async def get_rating(id: str, user: CurrentUser, store: StoreDep) -> dict[str, Any]:
    delivery = _get(store, id)
    if user.role != "ADMIN" and user.id != delivery.customer_id:
        raise ApiError(403, "Only the customer can see this rating")
    return rating_status(store, delivery)


@router.post("/deliveries/{id}/rating", status_code=201)
async def rate(id: str, body: RatingRequest, user: CurrentUser, store: StoreDep) -> dict[str, Any]:
    delivery = _get(store, id)
    if user.id != delivery.customer_id:
        raise ApiError(403, "Only the customer can rate this delivery")
    status = rating_status(store, delivery)
    if not status["canRate"]:
        raise ApiError(409, "This delivery cannot be rated", error=status["reason"] or "Conflict")
    assert delivery.driver_id is not None
    comment = body.comment.strip() if body.comment else None
    rating = Rating(delivery.id, delivery.driver_id, user.id, body.score, comment or None)
    store.ratings[delivery.id] = rating
    return rating.to_json()
