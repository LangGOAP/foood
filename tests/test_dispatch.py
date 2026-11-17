"""Dispatch strategies (issue #25): nearest first, stacking, and a visible search wait."""

from __future__ import annotations

from foood.deliveries import Delivery
from foood.dispatch import (
    STRATEGIES,
    finishing_near,
    longest_idle,
    nearest_first,
    stack_on_finishing,
    use,
)
from foood.drivers import Driver, available_drivers
from foood.models import Restaurant
from foood.orders import Order, OrderItem
from foood.store import CLOCK, Store
from foood.tracking import Location
from tests.support import login

QA_OWNER = "qa.restaurant@delivery-plus.test"


def _city() -> tuple[Store, Restaurant, list[Driver]]:
    """Three idle drivers around a restaurant, one of them without a known position."""
    store = Store()
    kitchen = Restaurant("K", "owner", "1 St", 37.7731, -122.4167)
    store.restaurants[kitchen.id] = kitchen
    drivers = []
    for n, position in enumerate([(37.7931, -122.3967), (37.774, -122.416), None]):
        driver = Driver(user_id=f"u{n}", vehicle_type="Bike", license_plate=f"P{n}")
        driver.verify("VERIFIED", None)
        driver.set_status("AVAILABLE")
        store.drivers[driver.id] = driver
        drivers.append(driver)
        if position is not None:
            store.locations[driver.user_id] = Location(
                driver.user_id, *position, recorded_at=CLOCK.now()
            )
    return store, kitchen, drivers


def test_strategies_on_a_small_city() -> None:
    store, kitchen, drivers = _city()
    delivery = Delivery("o", kitchen.id, "c")
    queue = available_drivers(store)
    assert [d.license_plate for d in queue] == ["P0", "P1", "P2"]  # online in that order
    assert longest_idle(store, delivery, queue) is drivers[0]
    assert nearest_first(store, delivery, queue) is drivers[1]  # 100 m away beats 2.8 km
    assert (
        nearest_first(store, delivery, [drivers[2], drivers[0]]) is drivers[0]
    )  # unknown goes last
    assert nearest_first(store, delivery, []) is None
    assert set(STRATEGIES) == {"longest-idle", "nearest", "stacking"}


def test_stacking_finds_a_driver_finishing_nearby() -> None:
    store, kitchen, drivers = _city()
    busy = drivers[0]
    busy.set_status("BUSY")
    order = Order(
        "c",
        kitchen.id,
        [OrderItem("m", "x", 1, 1)],
        "near the kitchen",
        delivery_latitude=37.7781,
        delivery_longitude=-122.4112,
    )
    store.orders[order.id] = order
    current = Delivery(order.id, kitchen.id, "c", driver_id=busy.id, status="IN_TRANSIT")
    store.deliveries[current.id] = current
    new = Delivery("o2", kitchen.id, "c2")
    assert finishing_near(store, new) is busy  # drop-off about 700 m from the restaurant
    order.delivery_latitude = 37.8451  # about 8 km away: too far to stack
    assert finishing_near(store, new) is None
    order.delivery_latitude = 37.7781
    current.status = "DRIVER_ASSIGNED"  # not even picked up yet: no stacking
    assert finishing_near(store, new) is None
    current.status = "IN_TRANSIT"
    # idle drivers win over stacking; with nobody idle the busy one takes it
    assert stack_on_finishing(store, new, available_drivers(store)) is drivers[1]
    for d in drivers[1:]:
        d.set_status("OFFLINE")
    assert stack_on_finishing(store, new, []) is busy


async def test_a_stacked_delivery_is_the_drivers_next_job(client) -> None:
    sara = await login(client, "demo.customer@example.com")
    owner = await login(client, QA_OWNER)
    driver = await login(client, "driver@example.com")
    use(client.store, stack_on_finishing)

    async def ready(item: str) -> dict:
        restaurants = (await client.get("/api/restaurants?search=QA Kitchen")).json()["items"]
        menu = (await client.get(f"/api/menus/restaurants/{restaurants[0]['id']}/menu")).json()
        chosen = next(i for i in menu["items"] if i["name"] == item)
        await client.post(
            "/api/cart/items", json={"menuItemId": chosen["id"], "quantity": 1}, headers=sara
        )
        order = (
            await client.post(
                "/api/orders",
                json={
                    "deliveryAddress": "Round the corner",
                    "deliveryLatitude": 37.7771,
                    "deliveryLongitude": -122.4121,
                },
                headers=sara,
            )
        ).json()
        payment = (
            await client.post("/api/payments", json={"orderId": order["id"]}, headers=sara)
        ).json()
        await client.post(f"/api/payments/{payment['id']}/process", json={}, headers=sara)
        for status in ("PREPARING", "READY_FOR_PICKUP"):
            await client.patch(
                f"/api/orders/{order['id']}/status", json={"status": status}, headers=owner
            )
        return order

    first = await ready("QA Pasta")
    await client.post("/api/drivers/me/online", json={}, headers=driver)
    job = (await client.get("/api/deliveries/me/current", headers=driver)).json()
    assert job["orderId"] == first["id"]
    await client.post(f"/api/deliveries/{job['id']}/pickup", json={}, headers=driver)
    await client.post(f"/api/deliveries/{job['id']}/start", json={}, headers=driver)
    second = await ready(
        "QA Burger"
    )  # nobody idle; the busy driver's drop-off is 500 m from the kitchen
    stacked = (await client.get(f"/api/deliveries/by-order/{second['id']}", headers=sara)).json()
    assert (
        stacked["status"] == "DRIVER_ASSIGNED" and stacked["driver"]["licensePlate"] == "DMO-2030"
    )
    assert (await client.get("/api/deliveries/me/current", headers=driver)).json()[
        "orderId"
    ] == first["id"]
    await client.post(f"/api/deliveries/{job['id']}/complete", json={}, headers=driver)
    assert (await client.get("/api/drivers/me", headers=driver)).json()[
        "status"
    ] == "BUSY"  # straight on
    next_job = (await client.get("/api/deliveries/me/current", headers=driver)).json()
    assert next_job["orderId"] == second["id"] and next_job["nextActions"] == ["pickup"]
    history = (await client.get("/api/deliveries/me/history", headers=driver)).json()
    assert {h["status"] for h in history["items"]} == {"DELIVERED", "DRIVER_ASSIGNED"}
    for action in ("pickup", "start", "complete"):
        await client.post(f"/api/deliveries/{next_job['id']}/{action}", json={}, headers=driver)
    assert (await client.get("/api/drivers/me", headers=driver)).json()["status"] == "AVAILABLE"


async def test_the_search_wait_is_visible(client) -> None:
    sara = await login(client, "demo.customer@example.com")
    owner = await login(client, QA_OWNER)
    restaurants = (await client.get("/api/restaurants?search=QA Kitchen")).json()["items"]
    menu = (await client.get(f"/api/menus/restaurants/{restaurants[0]['id']}/menu")).json()
    pasta = next(i for i in menu["items"] if i["name"] == "QA Pasta")
    await client.post(
        "/api/cart/items", json={"menuItemId": pasta["id"], "quantity": 1}, headers=sara
    )
    order = (await client.post("/api/orders", json={}, headers=sara)).json()
    payment = (
        await client.post("/api/payments", json={"orderId": order["id"]}, headers=sara)
    ).json()
    await client.post(f"/api/payments/{payment['id']}/process", json={}, headers=sara)
    for status in ("PREPARING", "READY_FOR_PICKUP"):
        await client.patch(
            f"/api/orders/{order['id']}/status", json={"status": status}, headers=owner
        )
    waiting = (await client.get(f"/api/deliveries/by-order/{order['id']}", headers=sara)).json()
    assert waiting["status"] == "CREATED" and waiting["searchSeconds"] == 0
    CLOCK.advance(95)
    assert (await client.get(f"/api/deliveries/by-order/{order['id']}", headers=sara)).json()[
        "searchSeconds"
    ] == 95
    driver = await login(client, "driver@example.com")
    await client.post("/api/drivers/me/online", json={}, headers=driver)
    assert (await client.get(f"/api/deliveries/by-order/{order['id']}", headers=sara)).json()[
        "searchSeconds"
    ] is None


def test_hand_wired_keeps_longest_idle(client) -> None:
    assert "dispatch" not in client.store.hooks
