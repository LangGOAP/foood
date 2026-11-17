"""The order promise (issue #21): a delivery-by time, a fact every engine can judge against."""

from __future__ import annotations

from datetime import datetime, timedelta

from foood.orders import DEFAULT_RIDE_MINUTES, PROMISE_MARGIN_MINUTES, promise_minutes
from foood.store import CLOCK, now
from tests.support import login

QA_OWNER = "qa.restaurant@delivery-plus.test"


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


async def _cart(client, headers, item: str = "QA Pasta") -> dict:
    restaurants = (await client.get("/api/restaurants?search=QA Kitchen")).json()["items"]
    menu = (await client.get(f"/api/menus/restaurants/{restaurants[0]['id']}/menu")).json()
    chosen = next(i for i in menu["items"] if i["name"] == item)
    await client.post(
        "/api/cart/items", json={"menuItemId": chosen["id"], "quantity": 1}, headers=headers
    )
    return restaurants[0]


async def _pay(client, headers, order_id: str) -> None:
    payment = (
        await client.post("/api/payments", json={"orderId": order_id}, headers=headers)
    ).json()
    await client.post(f"/api/payments/{payment['id']}/process", json={}, headers=headers)


def test_promise_formula() -> None:
    from foood.models import Restaurant

    kitchen = Restaurant("K", "o", "1 St", 30.03, 31.24, prep_minutes=12)
    unknown = promise_minutes(kitchen, None, None)
    assert unknown == 12 + DEFAULT_RIDE_MINUTES + PROMISE_MARGIN_MINUTES
    next_door = promise_minutes(kitchen, 30.03, 31.24)
    assert next_door == 12 + PROMISE_MARGIN_MINUTES  # no ride at all
    far = promise_minutes(kitchen, 30.12, 31.24)  # about 10 km north
    assert far > unknown


async def test_quote_and_order_carry_the_promise(client) -> None:
    sara = await login(client, "demo.customer@example.com")
    assert "promisedAt" not in (await client.get("/api/orders/quote", headers=sara)).json()
    restaurant = await _cart(client, sara)
    quote = (await client.get("/api/orders/quote", headers=sara)).json()
    assert quote["promiseMinutes"] == 10 + DEFAULT_RIDE_MINUTES + PROMISE_MARGIN_MINUTES
    assert _parse(quote["promisedAt"]) - now() < timedelta(minutes=quote["promiseMinutes"] + 1)
    order = (
        await client.post(
            "/api/orders",
            json={
                "deliveryAddress": "Next door",
                "deliveryLatitude": 30.03,
                "deliveryLongitude": 31.24,
            },
            headers=sara,
        )
    ).json()
    assert order["promiseMinutes"] == 10 + PROMISE_MARGIN_MINUTES  # the ride is zero next door
    assert _parse(order["promisedAt"]) - _parse(order["createdAt"]) == timedelta(
        minutes=order["promiseMinutes"]
    )
    assert order["promiseBreached"] is False and order["deliveredAt"] is None
    assert restaurant["id"] == order["restaurantId"]


async def test_breach_while_active_and_late_delivery(client) -> None:
    sara = await login(client, "demo.customer@example.com")
    owner = await login(client, QA_OWNER)
    driver = await login(client, "driver@example.com")
    await _cart(client, sara)
    order = (await client.post("/api/orders", json={}, headers=sara)).json()
    await _pay(client, sara, order["id"])
    await client.patch(
        f"/api/orders/{order['id']}/status", json={"status": "PREPARING"}, headers=owner
    )
    CLOCK.advance(order["promiseMinutes"] * 60 + 1)
    breached = (await client.get(f"/api/orders/{order['id']}", headers=sara)).json()
    assert breached["status"] == "PREPARING" and breached["promiseBreached"] is True
    await client.patch(
        f"/api/orders/{order['id']}/status", json={"status": "READY_FOR_PICKUP"}, headers=owner
    )
    await client.post("/api/drivers/me/online", json={}, headers=driver)
    job = (await client.get("/api/deliveries/me/current", headers=driver)).json()
    for action in ("pickup", "start", "complete"):
        await client.post(f"/api/deliveries/{job['id']}/{action}", json={}, headers=driver)
    late = (await client.get(f"/api/orders/{order['id']}", headers=sara)).json()
    assert late["status"] == "DELIVERED" and late["deliveredAt"]
    assert late["promiseBreached"] is True  # delivered, but after the promise
    assert _parse(late["deliveredAt"]) > _parse(late["promisedAt"])


async def test_on_time_delivery_and_cancellation_are_not_breaches(client) -> None:
    sara = await login(client, "demo.customer@example.com")
    owner = await login(client, QA_OWNER)
    driver = await login(client, "driver@example.com")
    await _cart(client, sara)
    order = (await client.post("/api/orders", json={}, headers=sara)).json()
    await _pay(client, sara, order["id"])
    for status in ("PREPARING", "READY_FOR_PICKUP"):
        await client.patch(
            f"/api/orders/{order['id']}/status", json={"status": status}, headers=owner
        )
    await client.post("/api/drivers/me/online", json={}, headers=driver)
    job = (await client.get("/api/deliveries/me/current", headers=driver)).json()
    for action in ("pickup", "start", "complete"):
        await client.post(f"/api/deliveries/{job['id']}/{action}", json={}, headers=driver)
    on_time = (await client.get(f"/api/orders/{order['id']}", headers=sara)).json()
    assert on_time["promiseBreached"] is False and on_time["deliveredAt"]
    CLOCK.advance(3600)
    assert (await client.get(f"/api/orders/{order['id']}", headers=sara)).json()[
        "promiseBreached"
    ] is False
    await _cart(client, sara, item="QA Burger")
    other = (await client.post("/api/orders", json={}, headers=sara)).json()
    await client.patch(
        f"/api/orders/{other['id']}/status", json={"status": "CANCELLED"}, headers=sara
    )
    CLOCK.advance(7200)
    assert (await client.get(f"/api/orders/{other['id']}", headers=sara)).json()[
        "promiseBreached"
    ] is False


async def test_a_revised_promise_is_announced(client) -> None:
    from foood.orders import get_order, revise_promise

    sara = await login(client, "demo.customer@example.com")
    await _cart(client, sara)
    order = (await client.post("/api/orders", json={}, headers=sara)).json()
    await _pay(client, sara, order["id"])
    later = now() + timedelta(minutes=50)
    revise_promise(
        client.store, get_order(client.store, order["id"]), later, "The kitchen is slower today"
    )
    revised = (await client.get(f"/api/orders/{order['id']}", headers=sara)).json()
    assert _parse(revised["promisedAt"]) == later.replace(microsecond=0)
    assert revised["promiseRevisions"] == [
        {
            "from": order["promisedAt"],
            "to": revised["promisedAt"],
            "reason": "The kitchen is slower today",
        }
    ]
    newest = (await client.get("/api/notifications", headers=sara)).json()["items"][0]
    assert newest["messageKey"] == "order.promise_moved"
    assert newest["params"]["reason"] == "The kitchen is slower today"
    assert newest["title"] == "New delivery time" and "slower today" in newest["message"]
