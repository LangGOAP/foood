"""Deliveries and dispatch (issue #9): from READY_FOR_PICKUP to the customer's door."""

from __future__ import annotations

from tests.support import assert_conforms, login

QA_OWNER = "qa.restaurant@delivery-plus.test"


async def _ready_order(client, customer: dict[str, str], owner: dict[str, str]) -> dict:
    """Sara orders from QA Kitchen, pays, and the kitchen readies it."""
    restaurants = (await client.get("/api/restaurants?search=QA Kitchen")).json()["items"]
    menu = (await client.get(f"/api/menus/restaurants/{restaurants[0]['id']}/menu")).json()
    pasta = next(i for i in menu["items"] if i["name"] == "QA Pasta")
    await client.post(
        "/api/cart/items", json={"menuItemId": pasta["id"], "quantity": 1}, headers=customer
    )
    order = (await client.post("/api/orders", json={}, headers=customer)).json()
    payment = (
        await client.post("/api/payments", json={"orderId": order["id"]}, headers=customer)
    ).json()
    await client.post(f"/api/payments/{payment['id']}/process", json={}, headers=customer)
    for status in ("PREPARING", "READY_FOR_PICKUP"):
        moved = await client.patch(
            f"/api/orders/{order['id']}/status", json={"status": status}, headers=owner
        )
        assert moved.status_code == 200, moved.text
    return order


async def _order_status(client, headers: dict[str, str], order_id: str) -> str:
    return (await client.get(f"/api/orders/{order_id}", headers=headers)).json()["status"]


async def test_full_delivery_run(client) -> None:
    sara = await login(client, "demo.customer@example.com")
    owner = await login(client, QA_OWNER)
    driver = await login(client, "driver@example.com")
    order = await _ready_order(client, sara, owner)
    # ready, nobody online: the delivery exists and is searching for a driver
    searching = await client.get(f"/api/deliveries/by-order/{order['id']}", headers=sara)
    body = assert_conforms("GET", "/api/deliveries/by-order/{orderId}", searching)
    assert searching.status_code == 200
    assert body["status"] == "CREATED" and body["driver"] is None and body["driverPhone"] is None
    assert (await client.get("/api/deliveries/me/current", headers=driver)).status_code == 204
    # the driver goes online and is assigned at once
    online = await client.post("/api/drivers/me/online", json={}, headers=driver)
    assert online.json()["status"] == "BUSY"
    assigned = (await client.get(f"/api/deliveries/by-order/{order['id']}", headers=sara)).json()
    assert assigned["status"] == "DRIVER_ASSIGNED"
    assert assigned["driver"] == {
        "displayName": "Base",
        "avatarUrl": None,
        "vehicleType": "Scooter",
        "licensePlate": "DMO-2030",
    }
    assert assigned["driverPhone"] == "+20 101 000 2030"
    for_kitchen = (
        await client.get(f"/api/deliveries/by-order/{order['id']}", headers=owner)
    ).json()
    assert (
        for_kitchen["driver"]["licensePlate"] == "DMO-2030" and for_kitchen["driverPhone"] is None
    )
    assert await _order_status(client, sara, order["id"]) == "DRIVER_ASSIGNED"
    current = await client.get("/api/deliveries/me/current", headers=driver)
    job = assert_conforms("GET", "/api/deliveries/me/current", current)
    assert current.status_code == 200
    assert job["pickup"]["name"] == "QA Kitchen" and job["pickup"]["address"] == "100 QA Avenue"
    assert job["dropOff"]["address"] == "742 Evergreen Terrace, Apt 3B"
    assert job["order"]["items"] == [{"name": "QA Pasta", "quantity": 1}]
    assert job["order"]["totalAmount"] == "15.50" and job["nextActions"] == ["pickup"]
    assert job["customerPhone"] == "+20 101 234 5678"
    # the run
    picked = await client.post(f"/api/deliveries/{job['id']}/pickup", json={}, headers=driver)
    assert picked.status_code == 201 and picked.json()["status"] == "PICKED_UP"
    assert await _order_status(client, sara, order["id"]) == "PICKED_UP"
    assert (await client.get("/api/deliveries/me/current", headers=driver)).json()[
        "nextActions"
    ] == ["start"]
    started = await client.post(f"/api/deliveries/{job['id']}/start", json={}, headers=driver)
    assert started.json()["status"] == "IN_TRANSIT"
    assert await _order_status(client, sara, order["id"]) == "PICKED_UP"
    assert (await client.get("/api/deliveries/me/current", headers=driver)).json()[
        "nextActions"
    ] == ["complete"]
    done = await client.post(f"/api/deliveries/{job['id']}/complete", json={}, headers=driver)
    assert done.json()["status"] == "DELIVERED"
    assert await _order_status(client, sara, order["id"]) == "DELIVERED"
    assert (await client.get("/api/drivers/me", headers=driver)).json()["status"] == "AVAILABLE"
    assert (await client.get("/api/deliveries/me/current", headers=driver)).status_code == 204
    finished = (await client.get(f"/api/deliveries/by-order/{order['id']}", headers=sara)).json()
    assert finished["status"] == "DELIVERED" and finished["driverPhone"] is None
    history = await client.get("/api/deliveries/me/history?status=completed", headers=driver)
    page = assert_conforms("GET", "/api/deliveries/me/history", history)
    assert page["total"] == 1 and page["items"][0]["status"] == "DELIVERED"
    assert page["items"][0]["restaurant"]["name"] == "QA Kitchen"
    assert page["items"][0]["order"]["dropOffAddress"] == "742 Evergreen Terrace, Apt 3B"
    assert (await client.get("/api/deliveries/me/history?status=cancelled", headers=driver)).json()[
        "items"
    ] == []
    earnings = (await client.get("/api/deliveries/me/earnings", headers=driver)).json()
    assert earnings["currency"] == "EGP" and earnings["pending"] == "3.00"
    assert earnings["available"] == "0.00" and earnings["balance"] == "3.00"
    assert (
        earnings["entries"][0]["type"] == "DELIVERY_EARNING"
        and earnings["entries"][0]["status"] == "PENDING"
    )
    completed = (await client.get("/api/orders?status=completed", headers=sara)).json()
    assert [o["id"] for o in completed["items"]] == [order["id"]]


async def test_an_online_driver_is_dispatched_when_the_order_is_ready(client) -> None:
    sara = await login(client, "demo.customer@example.com")
    owner = await login(client, QA_OWNER)
    first = await login(client, "driver@example.com")
    second = await login(client, "qa.driver@delivery-plus.test")
    await client.post("/api/drivers/me/online", json={}, headers=second)  # idle the longest
    await client.post("/api/drivers/me/online", json={}, headers=first)
    order = await _ready_order(client, sara, owner)
    delivery = (await client.get(f"/api/deliveries/by-order/{order['id']}", headers=sara)).json()
    assert (
        delivery["status"] == "DRIVER_ASSIGNED" and delivery["driver"]["licensePlate"] == "QA-0001"
    )
    assert (await client.get("/api/drivers/me", headers=second)).json()["status"] == "BUSY"
    assert (await client.get("/api/drivers/me", headers=first)).json()["status"] == "AVAILABLE"
    busy_offline = await client.post("/api/drivers/me/offline", json={}, headers=second)
    assert busy_offline.status_code == 409 and busy_offline.json()["error"] == "OnDelivery"


async def test_delivery_actions_are_guarded(client) -> None:
    sara = await login(client, "demo.customer@example.com")
    owner = await login(client, QA_OWNER)
    driver = await login(client, "driver@example.com")
    other = await login(client, "qa.driver@delivery-plus.test")
    order = await _ready_order(client, sara, owner)
    await client.post("/api/drivers/me/online", json={}, headers=driver)
    job = (await client.get("/api/deliveries/me/current", headers=driver)).json()
    assert (
        await client.post(f"/api/deliveries/{job['id']}/pickup", json={}, headers=other)
    ).status_code == 403
    assert (
        await client.post(f"/api/deliveries/{job['id']}/pickup", json={}, headers=sara)
    ).status_code == 403
    early = await client.post(f"/api/deliveries/{job['id']}/start", json={}, headers=driver)
    assert early.status_code == 409 and early.json()["error"] == "InvalidTransition"
    assert (
        await client.post("/api/deliveries/nope/pickup", json={}, headers=driver)
    ).status_code == 404
    assert (await client.get(f"/api/deliveries/{job['id']}", headers=sara)).status_code == 200
    assert (await client.get(f"/api/deliveries/{job['id']}", headers=owner)).status_code == 200
    assert (await client.get(f"/api/deliveries/{job['id']}", headers=other)).status_code == 403
    assert (
        await client.get(f"/api/deliveries/by-order/{order['id']}", headers=other)
    ).status_code == 403
    assert (await client.get("/api/deliveries/by-order/nope", headers=sara)).status_code == 404


async def test_admin_cancels_a_delivery_after_pickup(client) -> None:
    sara = await login(client, "demo.customer@example.com")
    owner = await login(client, QA_OWNER)
    driver = await login(client, "driver@example.com")
    admin = await login(client, "admin@example.com")
    order = await _ready_order(client, sara, owner)
    await client.post("/api/drivers/me/online", json={}, headers=driver)
    job = (await client.get("/api/deliveries/me/current", headers=driver)).json()
    await client.post(f"/api/deliveries/{job['id']}/pickup", json={}, headers=driver)
    forbidden = await client.post(f"/api/deliveries/{job['id']}/cancel", json={}, headers=driver)
    assert forbidden.status_code == 403
    cancelled = await client.post(
        f"/api/deliveries/{job['id']}/cancel", json={"reason": "Scooter broke down"}, headers=admin
    )
    assert cancelled.status_code == 201 and cancelled.json()["status"] == "CANCELLED"
    final = (await client.get(f"/api/orders/{order['id']}", headers=sara)).json()
    assert final["status"] == "CANCELLED" and final["cancelledBy"] == "SYSTEM"
    assert final["cancellationReason"] == "DELIVERY_CANCELLED"
    assert final["cancellationNote"] == "Scooter broke down"
    assert final["paymentStatus"] == "REFUNDED"
    assert (await client.get("/api/drivers/me", headers=driver)).json()["status"] == "AVAILABLE"
    assert (await client.get("/api/deliveries/me/current", headers=driver)).status_code == 204
    earnings = (await client.get("/api/deliveries/me/earnings", headers=driver)).json()
    assert earnings["entries"][0]["type"] == "CANCELLATION_COMPENSATION"
    history = (
        await client.get("/api/deliveries/me/history?status=cancelled", headers=driver)
    ).json()
    assert history["items"][0]["id"] == job["id"] and history["items"][0]["cancelledAt"]


async def test_cancelling_the_order_frees_the_driver(client) -> None:
    sara = await login(client, "demo.customer@example.com")
    owner = await login(client, QA_OWNER)
    driver = await login(client, "driver@example.com")
    admin = await login(client, "admin@example.com")
    order = await _ready_order(client, sara, owner)
    await client.post("/api/drivers/me/online", json={}, headers=driver)
    from foood.orders import get_order, transition

    transition(
        client.store, get_order(client.store, order["id"]), "CANCELLED", cancelled_by="SYSTEM"
    )
    assert (await client.get("/api/drivers/me", headers=driver)).json()["status"] == "AVAILABLE"
    delivery = (await client.get(f"/api/deliveries/by-order/{order['id']}", headers=admin)).json()
    assert delivery["status"] == "CANCELLED"


async def test_customer_rates_the_driver_once(client) -> None:
    sara = await login(client, "demo.customer@example.com")
    owner = await login(client, QA_OWNER)
    driver = await login(client, "driver@example.com")
    other = await login(client, "qa.customer@delivery-plus.test")
    await _ready_order(client, sara, owner)
    await client.post("/api/drivers/me/online", json={}, headers=driver)
    job = (await client.get("/api/deliveries/me/current", headers=driver)).json()
    too_soon = (await client.get(f"/api/deliveries/{job['id']}/rating", headers=sara)).json()
    assert too_soon["rating"] is None and too_soon["canRate"] is False and too_soon["reason"]
    for action in ("pickup", "start", "complete"):
        await client.post(f"/api/deliveries/{job['id']}/{action}", json={}, headers=driver)
    status = (await client.get(f"/api/deliveries/{job['id']}/rating", headers=sara)).json()
    assert status["canRate"] is True and status["closesAt"]
    assert (
        await client.post(f"/api/deliveries/{job['id']}/rating", json={"score": 5}, headers=other)
    ).status_code == 403
    bad = await client.post(f"/api/deliveries/{job['id']}/rating", json={"score": 9}, headers=sara)
    assert bad.status_code == 400
    rated = await client.post(
        f"/api/deliveries/{job['id']}/rating",
        json={"score": 5, "comment": "Fast and kind"},
        headers=sara,
    )
    rating = assert_conforms("POST", "/api/deliveries/{id}/rating", rated)
    assert (
        rated.status_code == 201 and rating["score"] == 5 and rating["comment"] == "Fast and kind"
    )
    twice = await client.post(
        f"/api/deliveries/{job['id']}/rating", json={"score": 1}, headers=sara
    )
    assert twice.status_code == 409
    again = (await client.get(f"/api/deliveries/{job['id']}/rating", headers=sara)).json()
    assert again["rating"]["score"] == 5 and again["canRate"] is False
    summary = await client.get("/api/deliveries/me/rating-summary", headers=driver)
    body = assert_conforms("GET", "/api/deliveries/me/rating-summary", summary)
    assert body["average"] == 5.0 and body["count"] == 1
    assert body["recentComments"][0]["comment"] == "Fast and kind"


async def test_admin_creates_and_assigns_by_hand(client) -> None:
    sara = await login(client, "demo.customer@example.com")
    owner = await login(client, QA_OWNER)
    driver = await login(client, "driver@example.com")
    admin = await login(client, "admin@example.com")
    order = await _ready_order(client, sara, owner)
    delivery = (await client.get(f"/api/deliveries/by-order/{order['id']}", headers=admin)).json()
    duplicate = await client.post("/api/deliveries", json={"orderId": order["id"]}, headers=admin)
    assert duplicate.status_code == 409
    me = (await client.get("/api/drivers/me", headers=driver)).json()
    offline_driver = await client.post(
        f"/api/deliveries/{delivery['id']}/assign", json={"driverId": me["id"]}, headers=admin
    )
    assert offline_driver.status_code == 409
    await client.post("/api/drivers/me/online", json={}, headers=driver)
    # going online already dispatched it; a second manual assign is refused
    taken = await client.post(
        f"/api/deliveries/{delivery['id']}/assign", json={"driverId": me["id"]}, headers=admin
    )
    assert taken.status_code == 409 and taken.json()["error"] == "InvalidTransition"


async def test_a_pickup_before_the_kitchen_is_ready_is_refused_whole(client) -> None:
    """A courier sent ahead of the kitchen may arrive early; the refusal must leave the delivery
    exactly as it was, not picked up on paper with the food still cooking."""
    from foood.deliveries import create_delivery, dispatch_pending

    sara = await login(client, "demo.customer@example.com")
    owner = await login(client, QA_OWNER)
    driver = await login(client, "driver@example.com")
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
    await client.patch(
        f"/api/orders/{order['id']}/status", json={"status": "PREPARING"}, headers=owner
    )
    await client.post("/api/drivers/me/online", json={}, headers=driver)
    create_delivery(client.store, client.store.orders[order["id"]])
    dispatch_pending(client.store)
    delivery = (await client.get(f"/api/deliveries/by-order/{order['id']}", headers=sara)).json()
    assert delivery["status"] == "DRIVER_ASSIGNED"
    early = await client.post(f"/api/deliveries/{delivery['id']}/pickup", json={}, headers=driver)
    assert early.status_code == 409
    again = (await client.get(f"/api/deliveries/by-order/{order['id']}", headers=sara)).json()
    assert again["status"] == "DRIVER_ASSIGNED"
    assert (await client.get(f"/api/orders/{order['id']}", headers=sara)).json()[
        "status"
    ] == "PREPARING"
    await client.patch(
        f"/api/orders/{order['id']}/status", json={"status": "READY_FOR_PICKUP"}, headers=owner
    )
    assert (
        await client.post(f"/api/deliveries/{delivery['id']}/pickup", json={}, headers=driver)
    ).status_code == 201
