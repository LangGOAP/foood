"""The scenario engine (issue #14): one test per failure, each ending in a consistent state."""

from __future__ import annotations

import pytest

from foood.scenario import FAILURES, SCENARIOS, Injection, Kitchen, Scenario, load, parse
from foood.store import CLOCK
from tests.support import login

QA_OWNER = "qa.restaurant@delivery-plus.test"


async def _order(client, customer, item: str = "QA Pasta", pay: bool = True) -> dict:
    restaurants = (await client.get("/api/restaurants?search=QA Kitchen")).json()["items"]
    menu = (await client.get(f"/api/menus/restaurants/{restaurants[0]['id']}/menu")).json()
    chosen = next(i for i in menu["items"] if i["name"] == item)
    await client.post(
        "/api/cart/items", json={"menuItemId": chosen["id"], "quantity": 1}, headers=customer
    )
    created = await client.post("/api/orders", json={}, headers=customer)
    assert created.status_code == 201, created.text
    order = created.json()
    if pay:
        payment = (
            await client.post("/api/payments", json={"orderId": order["id"]}, headers=customer)
        ).json()
        await client.post(f"/api/payments/{payment['id']}/process", json={}, headers=customer)
    return order


async def _status(client, headers, order_id: str) -> dict:
    return (await client.get(f"/api/orders/{order_id}", headers=headers)).json()


async def _location(client, headers, delivery_id: str) -> dict:
    return (await client.get(f"/api/tracking/delivery/{delivery_id}", headers=headers)).json()[
        "location"
    ]


def _advance(engine, seconds: float) -> None:
    CLOCK.advance(seconds)
    engine.tick()


# --- files and parsing --------------------------------------------------------------------------


def test_bundled_scenarios_parse() -> None:
    names = sorted(p.stem for p in SCENARIOS.glob("*.toml"))
    assert names == [
        "bridge-closed",
        "delivery-trouble",
        "happy-path",
        "incident",
        "kitchen-trouble",
        "payment-trouble",
        "storm",
    ]
    for name in names:
        scenario = load(name)
        assert scenario.name == name and scenario.description
        assert all(i.failure in FAILURES for i in scenario.injections)
    assert load("happy-path").kitchen.autopilot and not load("happy-path").injections
    assert len(load("delivery-trouble").injections) == 4


def test_parse_rejects_bad_files() -> None:
    with pytest.raises(ValueError, match="unknown failure"):
        parse('[[inject]]\norder = 1\nfailure = "meteor"\n')
    with pytest.raises(ValueError, match="order"):
        parse('[[inject]]\norder = 0\nfailure = "no_driver"\n')
    with pytest.raises(ValueError, match="after"):
        parse('[[inject]]\norder = 1\nfailure = "driver_drops"\nafter = "lunch"\n')
    with pytest.raises(FileNotFoundError):
        load("does-not-exist")


async def test_without_a_scenario_nothing_is_wired(client) -> None:
    assert client.store.engine is None and client.store.hooks == {}
    # support's deadline sweep and the world's clock, never a scenario timer
    assert len(client.store.tickers) == 2
    assert all(getattr(t, "__self__", None) is None for t in client.store.tickers)
    assert (await client.get("/api/_scenario")).json() == {"name": None, "log": []}


# --- the failures -------------------------------------------------------------------------------


async def test_restaurant_rejects_after_a_delay(scenario_client) -> None:
    scenario = Scenario(
        "t", injections=(Injection("restaurant_rejects", 1, after_seconds=8, reason="No buns"),)
    )
    async with scenario_client(scenario) as client:
        sara = await login(client, "demo.customer@example.com")
        order = await _order(client, sara)
        assert (await _status(client, sara, order["id"]))["status"] == "CONFIRMED"
        _advance(client.engine, 5)
        assert (await _status(client, sara, order["id"]))["status"] == "CONFIRMED"
        _advance(client.engine, 4)
        final = await _status(client, sara, order["id"])
        assert final["status"] == "CANCELLED" and final["cancelledBy"] == "RESTAURANT"
        assert final["cancellationNote"] == "No buns" and final["paymentStatus"] == "REFUNDED"
        alerts = (await client.get("/api/notifications", headers=sara)).json()["items"]
        assert (
            alerts[1]["messageKey"] == "order.cancelled"
            and alerts[1]["params"]["note"] == "No buns"
        )
        status = (await client.get("/api/_scenario")).json()
        assert status["name"] == "t" and status["ordersPlaced"] == 1
        assert [e["event"] for e in status["log"]] == ["armed", "fired"]
        # the next order is untouched
        second = await _order(client, sara)
        _advance(client.engine, 60)
        assert (await _status(client, sara, second["id"]))["status"] == "CONFIRMED"


async def test_accepts_then_cancels_marks_the_item_sold_out(scenario_client) -> None:
    scenario = Scenario(
        "t",
        kitchen=Kitchen(start_after=3),
        injections=(
            Injection("accepts_then_cancels", 1, after_seconds=20, reason="Pasta ran out"),
        ),
    )
    async with scenario_client(scenario) as client:
        sara = await login(client, "demo.customer@example.com")
        order = await _order(client, sara)
        _advance(client.engine, 3)
        assert (await _status(client, sara, order["id"]))["status"] == "PREPARING"
        _advance(client.engine, 20)
        final = await _status(client, sara, order["id"])
        assert final["status"] == "CANCELLED" and final["cancellationNote"] == "Pasta ran out"
        assert final["paymentStatus"] == "REFUNDED"
        item = (await client.get(f"/api/menus/menu-items/{order['items'][0]['menuItemId']}")).json()
        assert item["available"] is False


async def test_prep_overrun_with_the_kitchen_on_autopilot(scenario_client) -> None:
    scenario = Scenario(
        "t",
        kitchen=Kitchen(autopilot=True, start_after=5, ready_after=45),
        injections=(Injection("prep_overrun", 2, after_seconds=240),),
    )
    async with scenario_client(scenario) as client:
        sara = await login(client, "demo.customer@example.com")
        normal = await _order(client, sara)
        slow = await _order(client, sara, item="QA Burger")
        _advance(client.engine, 5)
        assert (await _status(client, sara, normal["id"]))["status"] == "PREPARING"
        assert (await _status(client, sara, slow["id"]))["status"] == "PREPARING"
        _advance(client.engine, 45)
        assert (await _status(client, sara, normal["id"]))["status"] == "READY_FOR_PICKUP"
        assert (await _status(client, sara, slow["id"]))["status"] == "PREPARING"
        _advance(client.engine, 195)
        assert (await _status(client, sara, slow["id"]))["status"] == "READY_FOR_PICKUP"


async def test_payment_declines_only_for_the_chosen_order(scenario_client) -> None:
    scenario = Scenario(
        "t", injections=(Injection("payment_declines", 1, reason="Insufficient funds"),)
    )
    async with scenario_client(scenario) as client:
        sara = await login(client, "demo.customer@example.com")
        order = await _order(client, sara, pay=False)
        payment = (
            await client.post("/api/payments", json={"orderId": order["id"]}, headers=sara)
        ).json()
        processed = (
            await client.post(f"/api/payments/{payment['id']}/process", json={}, headers=sara)
        ).json()
        assert (
            processed["status"] == "FAILED" and processed["failureReason"] == "Insufficient funds"
        )
        final = await _status(client, sara, order["id"])
        assert final["status"] == "CANCELLED" and final["cancelledBy"] == "PAYMENT"
        again = await _order(client, sara)
        assert (await _status(client, sara, again["id"]))["status"] == "CONFIRMED"


async def test_no_driver_for_a_while(scenario_client) -> None:
    scenario = Scenario("t", injections=(Injection("no_driver", 1, for_seconds=90),))
    async with scenario_client(scenario) as client:
        sara = await login(client, "demo.customer@example.com")
        owner = await login(client, QA_OWNER)
        driver = await login(client, "driver@example.com")
        await client.post("/api/drivers/me/online", json={}, headers=driver)
        order = await _order(client, sara)
        for status in ("PREPARING", "READY_FOR_PICKUP"):
            await client.patch(
                f"/api/orders/{order['id']}/status", json={"status": status}, headers=owner
            )
        delivery = (
            await client.get(f"/api/deliveries/by-order/{order['id']}", headers=sara)
        ).json()
        assert delivery["status"] == "CREATED"
        assert (await client.get("/api/drivers/me", headers=driver)).json()["status"] == "AVAILABLE"
        _advance(client.engine, 60)
        assert (await client.get(f"/api/deliveries/by-order/{order['id']}", headers=sara)).json()[
            "status"
        ] == "CREATED"
        _advance(client.engine, 31)
        assigned = (
            await client.get(f"/api/deliveries/by-order/{order['id']}", headers=sara)
        ).json()
        assert (
            assigned["status"] == "DRIVER_ASSIGNED"
            and assigned["driver"]["licensePlate"] == "DMO-2030"
        )
        assert (await _status(client, sara, order["id"]))["status"] == "DRIVER_ASSIGNED"


async def test_driver_drops_before_pickup_and_the_job_is_reassigned(scenario_client) -> None:
    scenario = Scenario("t", injections=(Injection("driver_drops", 1, after_seconds=10),))
    async with scenario_client(scenario) as client:
        sara = await login(client, "demo.customer@example.com")
        owner = await login(client, QA_OWNER)
        first = await login(client, "driver@example.com")
        second = await login(client, "qa.driver@delivery-plus.test")
        await client.post("/api/drivers/me/online", json={}, headers=first)
        order = await _order(client, sara)
        for status in ("PREPARING", "READY_FOR_PICKUP"):
            await client.patch(
                f"/api/orders/{order['id']}/status", json={"status": status}, headers=owner
            )
        assert (await client.get("/api/drivers/me", headers=first)).json()["status"] == "BUSY"
        _advance(client.engine, 10)
        assert (await client.get("/api/drivers/me", headers=first)).json()["status"] == "OFFLINE"
        waiting = (await client.get(f"/api/deliveries/by-order/{order['id']}", headers=sara)).json()
        assert waiting["status"] == "CREATED" and waiting["driver"] is None
        assert (await _status(client, sara, order["id"]))["status"] == "READY_FOR_PICKUP"
        await client.post("/api/drivers/me/online", json={}, headers=second)
        taken = (await client.get(f"/api/deliveries/by-order/{order['id']}", headers=sara)).json()
        assert taken["status"] == "DRIVER_ASSIGNED" and taken["driver"]["licensePlate"] == "QA-0001"
        _advance(client.engine, 30)  # the drop does not fire twice
        assert (await client.get(f"/api/deliveries/by-order/{order['id']}", headers=sara)).json()[
            "status"
        ] == "DRIVER_ASSIGNED"


async def test_driver_drops_after_pickup_cancels_and_compensates(scenario_client) -> None:
    scenario = Scenario(
        "t",
        injections=(
            Injection("driver_drops", 1, after="pickup", after_seconds=15, reason="Flat tyre"),
        ),
    )
    async with scenario_client(scenario) as client:
        sara = await login(client, "demo.customer@example.com")
        owner = await login(client, QA_OWNER)
        driver = await login(client, "driver@example.com")
        await client.post("/api/drivers/me/online", json={}, headers=driver)
        order = await _order(client, sara)
        for status in ("PREPARING", "READY_FOR_PICKUP"):
            await client.patch(
                f"/api/orders/{order['id']}/status", json={"status": status}, headers=owner
            )
        job = (await client.get("/api/deliveries/me/current", headers=driver)).json()
        await client.post(f"/api/deliveries/{job['id']}/pickup", json={}, headers=driver)
        _advance(client.engine, 15)
        final = await _status(client, sara, order["id"])
        assert final["status"] == "CANCELLED" and final["cancelledBy"] == "SYSTEM"
        assert final["cancellationNote"] == "Flat tyre" and final["paymentStatus"] == "REFUNDED"
        assert (await client.get(f"/api/deliveries/{job['id']}", headers=sara)).json()[
            "status"
        ] == "CANCELLED"
        assert (await client.get("/api/drivers/me", headers=driver)).json()["status"] == "OFFLINE"
        earnings = (await client.get("/api/deliveries/me/earnings", headers=driver)).json()
        assert earnings["entries"][0]["type"] == "CANCELLATION_COMPENSATION"


async def test_courier_delay_slows_the_simulated_phone(scenario_client) -> None:
    from foood.catalog import distance_km
    from foood.tracking import simulate_step

    scenario = Scenario(
        "t", injections=(Injection("courier_delay", 1, factor=0.2, for_seconds=180),)
    )
    async with scenario_client(scenario) as client:
        sara = await login(client, "demo.customer@example.com")
        owner = await login(client, QA_OWNER)
        driver = await login(client, "driver@example.com")
        await client.post("/api/drivers/me/online", json={}, headers=driver)
        order = await _order(client, sara)
        for status in ("PREPARING", "READY_FOR_PICKUP"):
            await client.patch(
                f"/api/orders/{order['id']}/status", json={"status": status}, headers=owner
            )
        delivery = (
            await client.get(f"/api/deliveries/by-order/{order['id']}", headers=sara)
        ).json()
        me = (await client.get("/api/auth/me", headers=driver)).json()
        assert client.store.speed_factors == {me["sub"]: 0.2}
        simulate_step(client.store)
        first = await _location(client, sara, delivery["id"])
        simulate_step(client.store)
        second = await _location(client, sara, delivery["id"])
        slow = distance_km(
            first["latitude"], first["longitude"], second["latitude"], second["longitude"]
        )
        _advance(client.engine, 180)
        assert client.store.speed_factors == {}
        simulate_step(client.store)
        third = await _location(client, sara, delivery["id"])
        full = distance_km(
            second["latitude"], second["longitude"], third["latitude"], third["longitude"]
        )
        assert slow == pytest.approx(full * 0.2, rel=0.05)


async def test_duplicate_submission_is_refused(scenario_client) -> None:
    scenario = Scenario("t", injections=(Injection("duplicate_submission", 1),))
    async with scenario_client(scenario) as client:
        sara = await login(client, "demo.customer@example.com")
        order = await _order(client, sara, pay=False)
        mine = (await client.get("/api/orders", headers=sara)).json()
        assert mine["total"] == 1 and mine["items"][0]["id"] == order["id"]
        log = (await client.get("/api/_scenario")).json()["log"]
        assert [e["event"] for e in log] == ["armed", "duplicate refused"]
        assert log[1]["error"] == "DuplicateOrder"
        assert (await client.get("/api/cart", headers=sara)).json()["items"] == []


async def test_the_platform_refuses_a_double_tap_itself(client) -> None:
    sara = await login(client, "demo.customer@example.com")
    restaurants = (await client.get("/api/restaurants?search=QA Kitchen")).json()["items"]
    menu = (await client.get(f"/api/menus/restaurants/{restaurants[0]['id']}/menu")).json()
    pasta = next(i for i in menu["items"] if i["name"] == "QA Pasta")
    for _ in range(2):
        await client.post(
            "/api/cart/items", json={"menuItemId": pasta["id"], "quantity": 1}, headers=sara
        )
        response = await client.post("/api/orders", json={}, headers=sara)
    assert response.status_code == 409 and response.json()["error"] == "DuplicateOrder"
    assert (await client.get("/api/orders", headers=sara)).json()["total"] == 1
    # once the first one is paid, ordering the same thing again is fine
    first = (await client.get("/api/orders", headers=sara)).json()["items"][0]
    payment = (
        await client.post("/api/payments", json={"orderId": first["id"]}, headers=sara)
    ).json()
    await client.post(f"/api/payments/{payment['id']}/process", json={}, headers=sara)
    assert (await client.post("/api/orders", json={}, headers=sara)).status_code == 201
