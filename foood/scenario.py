"""The scenario engine: scheduled, reproducible failures on the platform that already exists.

A scenario is a TOML file. It can put the kitchen on autopilot (so the demo
needs nobody in the restaurant app) and inject failures by order number: the
*n*-th order placed after the scenario loads. Every injection is a flag on a
simulator that already exists (the charge hook, the dispatch hook, the kitchen
transitions, the simulated phone, the driver's drop), so with no scenario
loaded nothing changes. Timers run on the simulator tick and on the
scenario clock, which tests move by hand.
"""

from __future__ import annotations

import random
import tomllib
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request

from foood.cart import CartItem, cart_of
from foood.deliveries import Delivery, dispatch_pending, drop
from foood.drivers import Driver, available_drivers, go_offline, go_online
from foood.errors import ApiError
from foood.orders import Order, get_order, place_order, transition
from foood.payments import Payment
from foood.store import Store, iso, now
from foood.world import (
    CORRIDORS,
    ZONES,
    close_road,
    prep_factor,
    set_traffic,
    set_weather,
    zone_of,
)

router = APIRouter(prefix="/api", tags=["scenario"])

SCENARIOS = Path(__file__).resolve().parents[1] / "scenarios"
FAILURES = {
    "restaurant_rejects": "the kitchen rejects the confirmed order after a delay",
    "accepts_then_cancels": "the kitchen starts cooking, then cancels: an item ran out",
    "prep_overrun": "the kitchen takes far longer than usual to ready the order",
    "payment_declines": "the card is declined when the payment is processed",
    "no_driver": "no driver is matched for a while after the order is ready",
    "driver_drops": "the assigned driver walks away, before or after pickup",
    "courier_delay": "the driver's phone moves at a fraction of the usual speed",
    "duplicate_submission": "the same order is submitted twice in a row",
    "gateway_down": (
        "the payment gateway is down for a while; every payment processed meanwhile is refused"
    ),
    "storm": "a storm over a zone (the kitchen's by default): couriers slow, kitchens take longer",
    "traffic": "heavy traffic in a zone (the kitchen's by default): every route through it slows",
    "road_closed": "a corridor between two zones is closed; routes between them become a detour",
    "driver_offline": "the longest-idle driver goes offline for a while; the pool thins",
}
WORLD_FAILURES = {"storm", "traffic", "road_closed", "driver_offline"}
DEFAULT_DELAY = {
    "restaurant_rejects": 8.0,
    "accepts_then_cancels": 20.0,
    "prep_overrun": 240.0,
    "driver_drops": 10.0,
}
DEFAULT_REASON = {
    "restaurant_rejects": "The kitchen cannot take this order right now",
    "accepts_then_cancels": "An item on the order just ran out",
    "driver_drops": "The driver dropped the job",
    "payment_declines": "Card declined",
}


@dataclass(frozen=True)
class Injection:
    failure: str
    order: int
    after_seconds: float | None = None
    for_seconds: float = 60.0
    reason: str | None = None
    after: str = "assigned"  # driver_drops: "assigned" or "pickup"
    factor: float = 0.25  # courier_delay: speed multiplier; traffic: delay factor (1.0 or more)
    times: int = 1  # payment_declines: how many attempts (cards) decline
    zone: str | None = None  # storm, traffic: the zone; default the kitchen's
    corridor: str | None = None  # road_closed: the corridor; default market-street

    @property
    def delay(self) -> float:
        if self.after_seconds is not None:
            return self.after_seconds
        return DEFAULT_DELAY.get(self.failure, 0.0)

    @property
    def note(self) -> str | None:
        return self.reason or DEFAULT_REASON.get(self.failure)


@dataclass(frozen=True)
class Kitchen:
    autopilot: bool = False
    start_after: float = 5.0
    ready_after: float = 60.0


@dataclass(frozen=True)
class Bench:
    """How the headless bench drives a scenario: who orders what, and who delivers."""

    orders: int | None = None  # default: enough to reach the last injected order, at least 2
    interval_seconds: float = 60.0
    restaurant: str = "QA Kitchen"
    items: tuple[str, ...] = ("QA Pasta",)
    drop_off: tuple[float, float] = (37.787, -122.3985)
    drivers: int = 2
    support_delay_seconds: float = 30.0
    customer_delay_seconds: float = 20.0  # before the customer accepts a substitute offered


@dataclass(frozen=True)
class Scenario:
    name: str
    description: str = ""
    seed: int = 0
    kitchen: Kitchen = Kitchen()
    injections: tuple[Injection, ...] = ()
    bench: Bench = Bench()


def parse(text: str) -> Scenario:
    raw = tomllib.loads(text)
    injections = []
    for item in raw.get("inject", []):
        failure = item.get("failure")
        if failure not in FAILURES:
            raise ValueError(f"unknown failure {failure!r}; one of {sorted(FAILURES)}")
        order = item.get("order")
        if not isinstance(order, int) or order < 1:
            raise ValueError(f"{failure}: 'order' must be a positive order number")
        if item.get("after", "assigned") not in {"assigned", "pickup"}:
            raise ValueError(f"{failure}: 'after' must be 'assigned' or 'pickup'")
        if item.get("zone") is not None and item["zone"] not in ZONES:
            raise ValueError(f"{failure}: no zone {item['zone']!r}; one of {sorted(ZONES)}")
        if item.get("corridor") is not None and item["corridor"] not in CORRIDORS:
            raise ValueError(
                f"{failure}: no corridor {item['corridor']!r}; one of {sorted(CORRIDORS)}"
            )
        injections.append(
            Injection(
                failure=failure,
                order=order,
                after_seconds=item.get("after_seconds"),
                for_seconds=float(item.get("for_seconds", 60)),
                reason=item.get("reason"),
                after=item.get("after", "assigned"),
                factor=float(item.get("factor", 2.0 if failure == "traffic" else 0.25)),
                times=int(item.get("times", 1)),
                zone=item.get("zone"),
                corridor=item.get("corridor"),
            )
        )
    kitchen = Kitchen(**raw.get("kitchen", {}))
    bench_raw = dict(raw.get("bench", {}))
    if "items" in bench_raw:
        bench_raw["items"] = tuple(bench_raw["items"])
    if "drop_off" in bench_raw:
        bench_raw["drop_off"] = tuple(bench_raw["drop_off"])
    return Scenario(
        name=raw.get("name", "unnamed"),
        description=raw.get("description", ""),
        seed=int(raw.get("seed", 0)),
        kitchen=kitchen,
        injections=tuple(injections),
        bench=Bench(**bench_raw),
    )


def resolve(name_or_path: str) -> Path:
    candidate = Path(name_or_path)
    if candidate.is_file():
        return candidate
    bundled = SCENARIOS / f"{name_or_path}.toml"
    if bundled.is_file():
        return bundled
    raise FileNotFoundError(
        f"no scenario {name_or_path!r}; bundled: {sorted(p.stem for p in SCENARIOS.glob('*.toml'))}"
    )


def load(name_or_path: str) -> Scenario:
    return parse(resolve(name_or_path).read_text(encoding="utf-8"))


# --- the engine ------------------------------------------------------------------------------


@dataclass
class Timer:
    due: datetime
    label: str
    action: Callable[[], None]


class Engine:
    def __init__(self, store: Store, scenario: Scenario) -> None:
        self.store = store
        self.scenario = scenario
        self.random = random.Random(scenario.seed)
        self.placed = 0
        self.armed: dict[str, list[Injection]] = {}  # order id -> injections
        self.timers: list[Timer] = []
        self.log: list[dict[str, Any]] = []
        self.no_driver_until: dict[str, datetime] = {}
        self.declines: dict[str, tuple[str, int]] = {}  # order id -> (reason, attempts left)
        self.previous_charge = store.hooks.get("charge")
        self.previous_dispatch = store.hooks.get("dispatch")

    # wiring ------------------------------------------------------------------------------

    def install(self) -> Engine:
        store = self.store
        store.subscribe("order.placed", lambda order: self._placed(order))
        store.subscribe("order.status", lambda order, previous: self._order_status(order))
        store.subscribe("delivery.created", lambda delivery: self._delivery_created(delivery))
        store.subscribe(
            "delivery.assigned", lambda delivery, driver: self._assigned(delivery, driver)
        )
        store.subscribe("delivery.status", lambda delivery: self._delivery_status(delivery))
        store.hooks["charge"] = self._charge
        store.hooks["dispatch"] = self._dispatch
        store.tickers.append(self.tick)
        store.engine = self
        return self

    def record(self, event: str, order_id: str | None = None, **extra: Any) -> None:
        self.log.append({"at": iso(now()), "event": event, "orderId": order_id, **extra})

    def schedule(self, seconds: float, label: str, action: Callable[[], None]) -> None:
        if seconds <= 0:
            action()
            return
        self.timers.append(Timer(now() + timedelta(seconds=seconds), label, action))

    def tick(self) -> None:
        """Run every timer that is due; called on each simulator tick and by tests."""
        moment = now()
        due: list[Timer] = []
        later: list[Timer] = []
        for timer in self.timers:
            (due if timer.due <= moment else later).append(timer)
        self.timers = later
        for timer in sorted(due, key=lambda t: t.due):
            timer.action()

    def injections_for(self, order: Order, failure: str) -> list[Injection]:
        return [i for i in self.armed.get(order.id, []) if i.failure == failure]

    # events ------------------------------------------------------------------------------

    def _placed(self, order: Order) -> None:
        self.placed += 1
        mine = [i for i in self.scenario.injections if i.order == self.placed]
        if mine:
            self.armed[order.id] = mine
        for injection in mine:
            self.record("armed", order.id, failure=injection.failure, orderNumber=self.placed)
            if injection.failure == "payment_declines":
                self.declines[order.id] = (injection.note or "Card declined", injection.times)
            elif injection.failure == "duplicate_submission":
                self._resubmit(order)
            elif injection.failure == "gateway_down":
                self._gateway_down(order, injection)
            elif injection.failure in WORLD_FAILURES:
                self.schedule(
                    injection.delay,
                    f"{injection.failure} begins",
                    lambda inj=injection: self._world(order, inj),  # type: ignore[misc]
                )

    def _world(self, order: Order, injection: Injection) -> None:
        """Move the world (or the driver pool) for the injection's window, then move it back."""
        store = self.store
        restaurant = store.restaurants[order.restaurant_id]
        zone = injection.zone or zone_of(restaurant.latitude, restaurant.longitude)
        failure = injection.failure
        if failure == "storm":
            set_weather(store, zone, "storm")
            self.record("fired", order.id, failure=failure, zone=zone)
            undo: Callable[[], Any] = lambda: set_weather(store, zone, "clear")  # noqa: E731
        elif failure == "traffic":
            set_traffic(store, zone, max(1.0, injection.factor))
            self.record("fired", order.id, failure=failure, zone=zone, factor=injection.factor)
            undo = lambda: set_traffic(store, zone, 1.0)  # noqa: E731
        elif failure == "road_closed":
            corridor = injection.corridor or "market-street"
            close_road(store, corridor, closed=True)
            self.record("fired", order.id, failure=failure, corridor=corridor)
            undo = lambda: close_road(store, corridor, closed=False)  # noqa: E731
        else:  # driver_offline
            queue = available_drivers(store)
            if not queue:
                self.record("fired", order.id, failure=failure, driver=None)
                return
            driver = queue[0]
            go_offline(store, driver)
            self.record("fired", order.id, failure=failure, driver=driver.user_id)
            undo = lambda: go_online(store, driver)  # noqa: E731

        def clear() -> None:
            undo()
            self.record("cleared", order.id, failure=failure)

        self.schedule(injection.for_seconds, f"{failure} clears", clear)

    def _gateway_down(self, order: Order, injection: Injection) -> None:
        """The gateway goes down as this order is placed and comes back after the window."""
        until = now() + timedelta(seconds=injection.for_seconds)
        self.store.gateway_down_until = until
        self.record("fired", order.id, failure="gateway_down", forSeconds=injection.for_seconds)
        self.store.publish("payment.gateway_down", until=until)

        def back() -> None:
            if self.store.gateway_down_until == until:
                self.store.gateway_down_until = None
            self.record("cleared", order.id, failure="gateway_down")
            self.store.publish("payment.gateway_up")

        self.schedule(injection.for_seconds, "gateway up", back)

    def _resubmit(self, order: Order) -> None:
        """The double tap: the same customer sends the same order again, right away."""
        customer = self.store.users[order.customer_id]
        cart = cart_of(self.store, customer.id)
        cart.restaurant_id = order.restaurant_id
        cart.items = [CartItem(i.menu_item_id, i.name, i.price, i.quantity) for i in order.items]
        try:
            duplicate = place_order(
                self.store,
                customer,
                delivery_address=order.delivery_address,
                delivery_notes=order.delivery_notes,
                latitude=order.delivery_latitude,
                longitude=order.delivery_longitude,
            )
        except ApiError as refused:
            cart.items.clear()
            cart.touch()
            self.record("duplicate refused", order.id, error=refused.error)
            return
        self.record("duplicate accepted", order.id, duplicateOrderId=duplicate.id)

    def _order_status(self, order: Order) -> None:
        if order.status != "CONFIRMED":
            return
        rejects = self.injections_for(order, "restaurant_rejects")
        cancels = self.injections_for(order, "accepts_then_cancels")
        overruns = self.injections_for(order, "prep_overrun")
        kitchen = self.scenario.kitchen
        if rejects:
            injection = rejects[0]
            self.schedule(
                injection.delay, "kitchen rejects", lambda: self._reject(order, injection)
            )
        elif cancels:
            injection = cancels[0]
            self.schedule(
                kitchen.start_after, "kitchen starts", lambda: self._kitchen(order, "PREPARING")
            )
            self.schedule(
                kitchen.start_after + injection.delay,
                "kitchen cancels",
                lambda: self._cancel_mid_prep(order, injection),
            )
        elif overruns:
            injection = overruns[0]
            self.schedule(
                kitchen.start_after, "kitchen starts", lambda: self._kitchen(order, "PREPARING")
            )
            self.schedule(
                kitchen.start_after + kitchen.ready_after,
                "kitchen is late",
                lambda: self._late(order, injection),
            )
            self.schedule(
                kitchen.start_after + injection.delay,
                "kitchen finally ready",
                lambda: self._kitchen(order, "READY_FOR_PICKUP"),
            )
        elif kitchen.autopilot:
            restaurant = self.store.restaurants[order.restaurant_id]
            weather = prep_factor(self.store, zone_of(restaurant.latitude, restaurant.longitude))
            self.schedule(
                kitchen.start_after, "kitchen starts", lambda: self._kitchen(order, "PREPARING")
            )
            self.schedule(
                kitchen.start_after + kitchen.ready_after * weather,
                "kitchen ready",
                lambda: self._kitchen(order, "READY_FOR_PICKUP"),
            )

    def _late(self, order: Order, injection: Injection) -> None:
        if order.status == "PREPARING":
            self.record("fired", order.id, failure="prep_overrun", readyAfter=injection.delay)

    def _kitchen(self, order: Order, status: str, failure: str | None = None) -> None:
        expected = {"PREPARING": "CONFIRMED", "READY_FOR_PICKUP": "PREPARING"}[status]
        if order.status != expected:
            return  # cancelled or moved by a human in the meantime
        transition(self.store, order, status)
        if failure:
            self.record("fired", order.id, failure=failure, status=status)

    def _reject(self, order: Order, injection: Injection) -> None:
        if order.status not in {"CONFIRMED", "PREPARING"}:
            return
        transition(
            self.store,
            order,
            "CANCELLED",
            cancelled_by="RESTAURANT",
            cancellation_reason="RESTAURANT_REJECTED",
            cancellation_note=injection.note,
        )
        self.record("fired", order.id, failure=injection.failure, note=injection.note)

    def _cancel_mid_prep(self, order: Order, injection: Injection) -> None:
        if order.status != "PREPARING":
            return
        item = self.store.menu_items.get(order.items[0].menu_item_id)
        if item is not None:
            item.available = False  # the kitchen marks it sold out for everyone
        self._reject(order, injection)

    def _delivery_created(self, delivery: Delivery) -> None:
        order = get_order(self.store, delivery.order_id)
        for injection in self.injections_for(order, "no_driver"):
            until = now() + timedelta(seconds=injection.for_seconds)
            self.no_driver_until[order.id] = until
            self.record("fired", order.id, failure="no_driver", until=iso(until))
            self.schedule(
                injection.for_seconds, "driver search resumes", lambda: self._search_again(order)
            )

    def _search_again(self, order: Order) -> None:
        self.no_driver_until.pop(order.id, None)
        self.record("driver search resumes", order.id)
        dispatch_pending(self.store)

    def _assigned(self, delivery: Delivery, driver: Driver) -> None:
        order = get_order(self.store, delivery.order_id)
        for injection in self.injections_for(order, "driver_drops"):
            if injection.after == "assigned":
                self.schedule(
                    injection.delay,
                    "driver drops",
                    lambda inj=injection: self._drop(delivery, inj),  # type: ignore[misc]
                )
        for injection in self.injections_for(order, "courier_delay"):
            self.store.speed_factors[driver.user_id] = injection.factor
            self.record("fired", order.id, failure="courier_delay", factor=injection.factor)
            self.schedule(
                injection.for_seconds,
                "courier back to speed",
                lambda: self._back_to_speed(driver),
            )

    def _back_to_speed(self, driver: Driver) -> None:
        self.store.speed_factors.pop(driver.user_id, None)
        self.record("courier back to speed")

    def _delivery_status(self, delivery: Delivery) -> None:
        if delivery.status != "PICKED_UP":
            return
        order = get_order(self.store, delivery.order_id)
        for injection in self.injections_for(order, "driver_drops"):
            if injection.after == "pickup":
                self.schedule(
                    injection.delay,
                    "driver drops",
                    lambda inj=injection: self._drop(delivery, inj),  # type: ignore[misc]
                )

    def _drop(self, delivery: Delivery, injection: Injection) -> None:
        if delivery.driver_id is None or delivery.status not in {
            "DRIVER_ASSIGNED",
            "PICKED_UP",
            "IN_TRANSIT",
        }:
            return
        drop(self.store, delivery, injection.note)
        self.record("fired", delivery.order_id, failure="driver_drops", after=injection.after)
        # the same job must not drop again if another driver takes it
        self.armed[delivery.order_id] = [
            i for i in self.armed.get(delivery.order_id, []) if i is not injection
        ]

    # hooks -------------------------------------------------------------------------------

    def _charge(self, payment: Payment) -> tuple[str, str | None]:
        pending = self.declines.get(payment.order_id)
        if pending is not None:
            reason, left = pending
            if left <= 1:
                self.declines.pop(payment.order_id)
            else:
                self.declines[payment.order_id] = (reason, left - 1)
            self.record(
                "fired",
                payment.order_id,
                failure="payment_declines",
                reason=reason,
                attempt=payment.attempt,
            )
            return "FAILED", reason
        if self.previous_charge is not None:
            outcome: tuple[str, str | None] = self.previous_charge(payment)
            return outcome
        return "COMPLETED", None

    def _dispatch(self, delivery: Delivery, queue: list[Driver]) -> Driver | None:
        until = self.no_driver_until.get(delivery.order_id)
        if until is not None and now() < until:
            return None
        if self.previous_dispatch is not None:
            chosen: Driver | None = self.previous_dispatch(delivery, queue)
            return chosen
        return queue[0] if queue else None

    # status ------------------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        return {
            "name": self.scenario.name,
            "description": self.scenario.description,
            "seed": self.scenario.seed,
            "kitchen": asdict(self.scenario.kitchen),
            "injections": [asdict(i) for i in self.scenario.injections],
            "ordersPlaced": self.placed,
            "pendingTimers": [{"due": iso(t.due), "label": t.label} for t in self.timers],
            "log": list(self.log),
        }


def install(store: Store, scenario: Scenario) -> Engine:
    return Engine(store, scenario).install()


@router.get("/_scenario")
async def scenario_status(request: Request) -> dict[str, Any]:
    """What the loaded scenario is and what it has fired so far (not part of the contract)."""
    engine: Engine | None = getattr(request.app.state.store, "engine", None)
    if engine is None:
        return {"name": None, "log": []}
    return engine.status()
