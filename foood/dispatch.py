"""Dispatch strategies: who gets a ready order.

Plain functions an engine selects per delivery through the ``dispatch`` hook
on the store. The hand-wired engine keeps ``longest_idle``, the platform's
default. ``nearest_first`` uses the drivers' last known positions, and
``stack_on_finishing`` hands a ready order to a busy driver whose current
drop-off is close to the restaurant, so the next job starts where the last one
ends.
"""

from __future__ import annotations

from collections.abc import Callable

from foood.catalog import distance_km
from foood.deliveries import ACTIVE, Delivery, current_delivery
from foood.drivers import Driver
from foood.orders import get_order
from foood.store import Store, now

STACK_WITHIN_KM = 1.5
Strategy = Callable[[Store, Delivery, list[Driver]], Driver | None]


def longest_idle(store: Store, delivery: Delivery, queue: list[Driver]) -> Driver | None:
    """The platform default: the verified, online driver idle the longest."""
    return queue[0] if queue else None


def nearest_first(store: Store, delivery: Delivery, queue: list[Driver]) -> Driver | None:
    """The idle driver nearest the restaurant by last known position; unknown positions last."""
    if not queue:
        return None
    restaurant = store.restaurants[delivery.restaurant_id]

    def key(driver: Driver) -> tuple[int, float, int]:
        location = store.locations.get(driver.user_id)
        if location is None:
            return (1, 0.0, queue.index(driver))
        km = distance_km(
            location.latitude, location.longitude, restaurant.latitude, restaurant.longitude
        )
        return (0, km, queue.index(driver))

    return min(queue, key=key)


def finishing_near(
    store: Store, delivery: Delivery, within_km: float = STACK_WITHIN_KM
) -> Driver | None:
    """A busy driver on their way to a drop-off within ``within_km`` of this restaurant."""
    restaurant = store.restaurants[delivery.restaurant_id]
    candidates: list[tuple[float, Driver]] = []
    for driver in store.drivers.values():
        if driver.status != "BUSY" or driver.verification_status != "VERIFIED":
            continue
        current = current_delivery(store, driver)
        if current is None or current.status not in {"PICKED_UP", "IN_TRANSIT"}:
            continue
        if (
            sum(d.driver_id == driver.id and d.status in ACTIVE for d in store.deliveries.values())
            > 1
        ):
            continue  # already carrying a stacked job
        order = get_order(store, current.order_id)
        if order.delivery_latitude is None or order.delivery_longitude is None:
            continue
        km = distance_km(
            order.delivery_latitude,
            order.delivery_longitude,
            restaurant.latitude,
            restaurant.longitude,
        )
        if km <= within_km:
            candidates.append((km, driver))
    return min(candidates, key=lambda pair: pair[0])[1] if candidates else None


def stack_on_finishing(store: Store, delivery: Delivery, queue: list[Driver]) -> Driver | None:
    """Nearest idle driver first; with nobody idle, a busy driver finishing nearby takes it next."""
    idle = nearest_first(store, delivery, queue)
    return idle if idle is not None else finishing_near(store, delivery)


STRATEGIES: dict[str, Strategy] = {
    "longest-idle": longest_idle,
    "nearest": nearest_first,
    "stacking": stack_on_finishing,
}


def search_seconds(delivery: Delivery) -> int | None:
    """How long a delivery has waited for a driver; None once it has one."""
    if delivery.status != "CREATED":
        return None
    return int((now() - delivery.created_at).total_seconds())


def use(store: Store, strategy: Strategy) -> None:
    """Install ``strategy`` as the store's dispatch hook (what an engine does at start-up)."""
    store.hooks["dispatch"] = lambda delivery, queue: strategy(store, delivery, queue)
