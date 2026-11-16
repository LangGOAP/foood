"""Notifications (issue #12): one per customer-visible stage, in order."""

from __future__ import annotations

from tests.support import login

QA_OWNER = "qa.restaurant@delivery-plus.test"


async def _place_and_pay(client, customer, item: str = "QA Pasta") -> dict:
    restaurants = (await client.get("/api/restaurants?search=QA Kitchen")).json()["items"]
    menu = (await client.get(f"/api/menus/restaurants/{restaurants[0]['id']}/menu")).json()
    chosen = next(i for i in menu["items"] if i["name"] == item)
    await client.post(
        "/api/cart/items", json={"menuItemId": chosen["id"], "quantity": 1}, headers=customer
    )
    order = (await client.post("/api/orders", json={}, headers=customer)).json()
    payment = (
        await client.post("/api/payments", json={"orderId": order["id"]}, headers=customer)
    ).json()
    await client.post(f"/api/payments/{payment['id']}/process", json={}, headers=customer)
    return order


async def _keys(client, headers) -> list[str]:
    page = (await client.get("/api/notifications?limit=50", headers=headers)).json()
    return [n["messageKey"] for n in reversed(page["items"])]  # oldest first


async def test_a_completed_order_tells_its_story_in_order(client) -> None:
    sara = await login(client, "demo.customer@example.com")
    owner = await login(client, QA_OWNER)
    driver = await login(client, "driver@example.com")
    assert (await client.get("/api/notifications", headers=sara)).json() == {
        "items": [],
        "page": 1,
        "limit": 20,
        "total": 0,
        "totalPages": 0,
    }
    order = await _place_and_pay(client, sara)
    for status in ("PREPARING", "READY_FOR_PICKUP"):
        await client.patch(
            f"/api/orders/{order['id']}/status", json={"status": status}, headers=owner
        )
    await client.post("/api/drivers/me/online", json={}, headers=driver)
    delivery = (await client.get(f"/api/deliveries/by-order/{order['id']}", headers=sara)).json()
    for action in ("pickup", "start", "complete"):
        await client.post(f"/api/deliveries/{delivery['id']}/{action}", json={}, headers=driver)
    assert await _keys(client, sara) == [
        "payment.completed",
        "order.confirmed",
        "delivery.driver_assigned",
        "delivery.picked_up",
        "delivery.delivered",
    ]
    page = (await client.get("/api/notifications", headers=sara)).json()
    newest = page["items"][0]
    assert newest["type"] == "DELIVERED" and newest["isRead"] is False
    assert newest["params"] == {"orderId": order["id"]}
    assert newest["title"] == "Order delivered" and order["id"][:8] in newest["message"]
    assert newest["userId"] == order["customerId"] and newest["createdAt"]
    assert page["total"] == 5 and page["totalPages"] == 1
    # nobody else hears about Sara's order
    assert (await client.get("/api/notifications", headers=driver)).json()["total"] == 0
    assert (await client.get("/api/notifications", headers=owner)).json()["total"] == 0


async def test_read_marks(client) -> None:
    sara = await login(client, "demo.customer@example.com")
    other = await login(client, "qa.customer@delivery-plus.test")
    await _place_and_pay(client, sara)
    items = (await client.get("/api/notifications", headers=sara)).json()["items"]
    first, second = items[0], items[1]
    marked = await client.patch(f"/api/notifications/{first['id']}/read", json={}, headers=sara)
    assert marked.status_code == 200 and marked.json() == {"success": True}
    after = {
        n["id"]: n["isRead"]
        for n in (await client.get("/api/notifications", headers=sara)).json()["items"]
    }
    assert after[first["id"]] is True and after[second["id"]] is False
    assert (
        await client.patch(f"/api/notifications/{second['id']}/read", json={}, headers=other)
    ).status_code == 404
    assert (
        await client.patch("/api/notifications/nope/read", json={}, headers=sara)
    ).status_code == 404
    everything = await client.patch("/api/notifications/read-all", json={}, headers=sara)
    assert everything.status_code == 200 and everything.json() == {"success": True}
    assert all(
        n["isRead"] for n in (await client.get("/api/notifications", headers=sara)).json()["items"]
    )
    assert (await client.get("/api/notifications")).status_code == 401


async def test_cancellations_and_declines_carry_the_reason(client) -> None:
    sara = await login(client, "demo.customer@example.com")
    owner = await login(client, QA_OWNER)
    order = await _place_and_pay(client, sara)
    await client.patch(
        f"/api/orders/{order['id']}/status",
        json={"status": "CANCELLED", "reason": "Out of pasta"},
        headers=owner,
    )
    page = (await client.get("/api/notifications", headers=sara)).json()
    by_key = {n["messageKey"]: n for n in page["items"]}
    assert by_key["order.cancelled"]["params"] == {
        "orderId": order["id"],
        "cancelledBy": "RESTAURANT",
        "note": "Out of pasta",
    }
    assert "Out of pasta" in by_key["order.cancelled"]["message"]
    assert by_key["payment.refunded"]["params"] == {"orderId": order["id"], "amount": "15.50"}
    assert await _keys(client, sara) == [
        "payment.completed",
        "order.confirmed",
        "order.cancelled",
        "payment.refunded",
    ]


async def test_a_declined_card_is_reported(client, monkeypatch) -> None:
    monkeypatch.setenv("PAYMENT_TEST_HOOKS", "true")
    sara = await login(client, "demo.customer@example.com")
    restaurants = (await client.get("/api/restaurants?search=QA Kitchen")).json()["items"]
    menu = (await client.get(f"/api/menus/restaurants/{restaurants[0]['id']}/menu")).json()
    burger = next(i for i in menu["items"] if i["name"] == "QA Burger")
    await client.post(
        "/api/cart/items", json={"menuItemId": burger["id"], "quantity": 1}, headers=sara
    )
    order = (await client.post("/api/orders", json={}, headers=sara)).json()
    payment = (
        await client.post("/api/payments", json={"orderId": order["id"]}, headers=sara)
    ).json()
    await client.post(
        f"/api/payments/{payment['id']}/process", json={"simulateFailure": True}, headers=sara
    )
    assert await _keys(client, sara) == ["payment.failed"]


async def test_pagination(client) -> None:
    sara = await login(client, "demo.customer@example.com")
    for _ in range(3):
        await _place_and_pay(client, sara)  # two notifications each
    page = (await client.get("/api/notifications?page=2&limit=4", headers=sara)).json()
    assert page["total"] == 6 and page["totalPages"] == 2 and len(page["items"]) == 2
    assert page["page"] == 2 and page["limit"] == 4
