"""Driver locations, the delivery tracking snapshot, its SSE stream, and a simulated phone.

The snapshot's ``tracking`` state is what the customer app renders::

    NO_DRIVER -> AWAITING_LOCATION -> LIVE | STALE -> ENDED

The stream sends a ``tracking`` event with the snapshot on open, on every
location report or delivery change, then an ``ENDED`` snapshot and closes.
When no real device shares a driver's position, ``simulate_step`` moves a
phone along a straight line to the restaurant and then to the drop-off.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from foood.auth import CurrentUser, StoreDep
from foood.catalog import distance_km
from foood.deliveries import ACTIVE, Delivery, current_delivery, delivery_for_order
from foood.drivers import driver_of
from foood.errors import ApiError
from foood.orders import get_order
from foood.store import Store, User, iso, now

router = APIRouter(prefix="/api", tags=["tracking"])

STALE_AFTER = timedelta(seconds=90)
MAX_AGE = timedelta(minutes=10)
MAX_SKEW = timedelta(seconds=60)
ETA_VALID = timedelta(seconds=60)
AVERAGE_SPEED_KMH = 20.0
HEARTBEAT_SECONDS = 15
SIM_TICK_SECONDS = 5
SIM_START_KM = 1.5  # where a simulated phone first appears, relative to the restaurant
SIM_DROP_OFFSET = (0.012, 0.012)  # degrees; used when an order has no drop-off coordinates
REAL_REPORT_GRACE = timedelta(seconds=30)  # a real phone silences the simulation for this long


@dataclass
class Location:
    user_id: str
    latitude: float
    longitude: float
    recorded_at: datetime
    received_at: datetime = field(default_factory=now)
    simulated: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "latitude": self.latitude,
            "longitude": self.longitude,
            "updatedAt": iso(self.received_at),
        }


# --- snapshot --------------------------------------------------------------------------


def _eta(store: Store, delivery: Delivery, location: Location | None) -> dict[str, Any]:
    if delivery.driver_id is None or location is None:
        return {"status": "UNAVAILABLE", "reason": "NO_LOCATION"}
    order = get_order(store, delivery.order_id)
    restaurant = store.restaurants[delivery.restaurant_id]
    if delivery.status == "DRIVER_ASSIGNED":
        target: tuple[float, float] | None = (restaurant.latitude, restaurant.longitude)
    elif order.delivery_latitude is not None and order.delivery_longitude is not None:
        target = (order.delivery_latitude, order.delivery_longitude)
    else:
        target = None
    if target is None:
        return {"status": "UNAVAILABLE", "reason": "NO_DESTINATION"}
    km = distance_km(location.latitude, location.longitude, *target)
    computed = now()
    return {
        "status": "ESTIMATED",
        "seconds": max(1, int(km / AVERAGE_SPEED_KMH * 3600)),
        "driverId": delivery.driver_id,
        "assignedAt": iso(delivery.assigned_at) if delivery.assigned_at else None,
        "basedOnLocationAt": iso(location.received_at),
        "computedAt": iso(computed),
        "validUntil": iso(computed + ETA_VALID),
    }


def snapshot(store: Store, delivery: Delivery) -> dict[str, Any]:
    driver = store.drivers.get(delivery.driver_id) if delivery.driver_id else None
    location = store.locations.get(driver.user_id) if driver else None
    age = (now() - location.received_at).total_seconds() if location else None
    if delivery.status in {"DELIVERED", "CANCELLED"}:
        state = "ENDED"
    elif driver is None:
        state = "NO_DRIVER"
    elif location is None:
        state = "AWAITING_LOCATION"
    elif age is not None and age > STALE_AFTER.total_seconds():
        state = "STALE"
    else:
        state = "LIVE"
    return {
        "deliveryId": delivery.id,
        "status": delivery.status,
        "driverId": delivery.driver_id,
        "location": location.to_json() if location else None,
        "tracking": state,
        "locationAgeSeconds": int(age) if age is not None else None,
        "assignment": (
            {
                "driverId": delivery.driver_id,
                "assignedAt": iso(delivery.assigned_at) if delivery.assigned_at else None,
            }
            if delivery.driver_id
            else None
        ),
        "eta": (
            _eta(store, delivery, location)
            if state not in {"ENDED"}
            else {"status": "UNAVAILABLE", "reason": "ENDED"}
        ),
    }


def _may_track(store: Store, delivery: Delivery, user: User) -> bool:
    if user.role == "ADMIN" or user.id == delivery.customer_id:
        return True
    driver = driver_of(store, user.id)
    if driver is not None and driver.id == delivery.driver_id:
        return True
    restaurant = store.restaurants.get(delivery.restaurant_id)
    return restaurant is not None and restaurant.owner_id == user.id


def _tracked_delivery(store: Store, delivery_id: str, user: User) -> Delivery:
    delivery = store.deliveries.get(delivery_id)
    if delivery is None:
        raise ApiError(404, "Delivery not found")
    if not _may_track(store, delivery, user):
        raise ApiError(403, "You do not have access to this delivery")
    return delivery


# --- fan-out to open streams ---------------------------------------------------------------


def _notify(store: Store, delivery: Delivery) -> None:
    for queue in store.watchers.get(delivery.id, []):
        queue.put_nowait(snapshot(store, delivery))


def _on_location(store: Store, location: Location) -> None:
    driver = driver_of(store, location.user_id)
    if driver is None:
        return
    delivery = current_delivery(store, driver)
    if delivery is not None:
        _notify(store, delivery)


def install(store: Store) -> None:
    store.subscribe("tracking.location", lambda location: _on_location(store, location))
    store.subscribe("delivery.assigned", lambda delivery, driver: _notify(store, delivery))
    store.subscribe("delivery.status", lambda delivery: _notify(store, delivery))


# --- the simulated phone --------------------------------------------------------------------


def _step_towards(
    origin: tuple[float, float], target: tuple[float, float], km: float
) -> tuple[float, float]:
    total = distance_km(origin[0], origin[1], target[0], target[1])
    if total <= km or total == 0:
        return target
    share = km / total
    return (
        origin[0] + (target[0] - origin[0]) * share,
        origin[1] + (target[1] - origin[1]) * share,
    )


def _destination(store: Store, delivery: Delivery) -> tuple[float, float]:
    restaurant = store.restaurants[delivery.restaurant_id]
    if delivery.status == "DRIVER_ASSIGNED":
        return restaurant.latitude, restaurant.longitude
    order = get_order(store, delivery.order_id)
    if order.delivery_latitude is not None and order.delivery_longitude is not None:
        return order.delivery_latitude, order.delivery_longitude
    return restaurant.latitude + SIM_DROP_OFFSET[0], restaurant.longitude + SIM_DROP_OFFSET[1]


def simulated_phones_win() -> bool:
    """``FOOOD_SIMULATED_PHONES=true`` (set by ``foood up``): browsers cannot move, so the
    simulated phone keeps driving even while a web build reports its fixed position."""
    return os.environ.get("FOOOD_SIMULATED_PHONES", "").lower() in {"1", "true", "yes"}


def simulate_step(store: Store, seconds: float = SIM_TICK_SECONDS) -> None:
    """Move every active delivery's driver whose real phone is silent."""
    moment = now()
    step_km = AVERAGE_SPEED_KMH * seconds / 3600
    for delivery in list(store.deliveries.values()):
        if delivery.status not in ACTIVE or delivery.driver_id is None:
            continue
        driver = store.drivers.get(delivery.driver_id)
        if driver is None:
            continue
        current = store.locations.get(driver.user_id)
        if current is not None and not current.simulated and not simulated_phones_win():
            if moment - current.received_at < REAL_REPORT_GRACE:
                continue  # a real device is sharing; leave it alone
        target = _destination(store, delivery)
        step = step_km * store.speed_factors.get(driver.user_id, 1.0)
        if current is None:
            restaurant = store.restaurants[delivery.restaurant_id]
            # appear SIM_START_KM south-west of the restaurant, then drive in
            lat_offset = SIM_START_KM / 111.0 / math.sqrt(2)
            lng_offset = lat_offset / max(math.cos(math.radians(restaurant.latitude)), 1e-6)
            origin = (restaurant.latitude - lat_offset, restaurant.longitude - lng_offset)
            position = _step_towards(origin, target, step)
        else:
            position = _step_towards((current.latitude, current.longitude), target, step)
        location = Location(
            driver.user_id, *position, recorded_at=moment, received_at=moment, simulated=True
        )
        store.locations[driver.user_id] = location
        store.publish("tracking.location", location=location)


async def run_simulator(store: Store, tick: float = SIM_TICK_SECONDS) -> None:
    """Background task started with the server: the phones move, then the timers run."""
    while True:
        await asyncio.sleep(tick)
        simulate_step(store, tick)
        for ticker in list(store.tickers):
            ticker()


# --- routes -----------------------------------------------------------------------------------


class LocationReport(BaseModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    recordedAt: datetime | None = None


@router.post("/tracking/location", status_code=201)
async def report_location(
    body: LocationReport, user: CurrentUser, store: StoreDep
) -> dict[str, Any]:
    if driver_of(store, user.id) is None:
        raise ApiError(403, "Only drivers report a location")
    moment = now()
    recorded = (
        body.recordedAt.astimezone(moment.tzinfo)
        if body.recordedAt and body.recordedAt.tzinfo
        else body.recordedAt
    )
    if recorded is not None:
        if recorded.tzinfo is None:
            recorded = recorded.replace(tzinfo=moment.tzinfo)
        if recorded > moment + MAX_SKEW:
            raise ApiError(400, "recordedAt is in the future")
        if recorded < moment - MAX_AGE:
            raise ApiError(400, "recordedAt is too old")
    location = Location(user.id, body.latitude, body.longitude, recorded or moment, moment)
    store.locations[user.id] = location
    store.publish("tracking.location", location=location)
    return {"driverId": user.id, **location.to_json()}


@router.get("/tracking/driver/{userId}")
async def driver_location(userId: str, user: CurrentUser, store: StoreDep) -> dict[str, Any]:
    if user.role != "ADMIN" and user.id != userId:
        raise ApiError(403, "You do not have access to this driver's location")
    location = store.locations.get(userId)
    if location is None:
        raise ApiError(404, "No location for this driver")
    return {"driverId": userId, **location.to_json()}


@router.get("/tracking/delivery/{deliveryId}")
async def delivery_tracking(deliveryId: str, user: CurrentUser, store: StoreDep) -> dict[str, Any]:
    return snapshot(store, _tracked_delivery(store, deliveryId, user))


def _frame(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


@router.get("/tracking/delivery/{deliveryId}/stream")
async def delivery_stream(deliveryId: str, user: CurrentUser, store: StoreDep) -> StreamingResponse:
    """Closes after ENDED; a client that leaves early cancels the generator."""
    delivery = _tracked_delivery(store, deliveryId, user)
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    store.watchers.setdefault(delivery.id, []).append(queue)

    async def frames() -> AsyncIterator[str]:
        try:
            current = snapshot(store, delivery)
            yield _frame("tracking", current)
            while current["tracking"] != "ENDED":
                try:
                    current = await asyncio.wait_for(queue.get(), HEARTBEAT_SECONDS)
                except TimeoutError:
                    yield ": ping\n\n"
                    continue
                yield _frame("tracking", current)
        finally:
            store.watchers[delivery.id].remove(queue)

    return StreamingResponse(
        frames(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


__all__ = [
    "Location",
    "delivery_for_order",
    "install",
    "router",
    "run_simulator",
    "simulate_step",
    "snapshot",
]
