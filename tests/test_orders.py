"""Orders (issue #6): created from the cart, priced, cancelled, and worked by the kitchen."""

from __future__ import annotations

import uuid

from tests.support import assert_conforms, login

QA_OWNER = "qa.restaurant@delivery-plus.test"


async def _menu(client, name: str) -> dict:
    restaurants = (await client.get(f"/api/restaurants?search={name}")).json()["items"]
    menu = (await client.get(f"/api/menus/restaurants/{restaurants[0]['id']}/menu")).json()
    return {"restaurantId": restaurants[0]["id"], **{i["name"]: i for i in menu["items"]}}


async def _fill_cart(client, headers: dict[str, str], restaurant: str = "QA") -> dict:
    menu = await _menu(client, restaurant)
    first, second = ("QA Burger", "QA Fries") if restaurant == "QA" else ("Margherita", "Tiramisu")
    await client.post(
        "/api/cart/items", json={"menuItemId": menu[first]["id"], "quantity": 2}, headers=headers
    )
    await client.post(
        "/api/cart/items", json={"menuItemId": menu[second]["id"], "quantity": 1}, headers=headers
    )
    return menu


async def _place(client, headers: dict[str, str], **body) -> dict:
    response = await client.post(
        "/api/orders", json=body, headers={**headers, "Idempotency-Key": str(uuid.uuid4())}
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_quote_then_order_from_the_cart(client) -> None:
    sara = await login(client, "demo.customer@example.com")
    empty = await client.get("/api/orders/quote", headers=sara)
    assert empty.status_code == 200 and empty.json()["totalAmount"] == "0.00"
    menu = await _fill_cart(client, sara)
    quote = (await client.get("/api/orders/quote", headers=sara)).json()
    assert quote == {
        "subtotalAmount": "24.00",
        "deliveryFee": "3.00",
        "totalAmount": "27.00",
        "currency": "EGP",
    }
    key = str(uuid.uuid4())
    created = await client.post(
        "/api/orders",
        json={"deliveryNotes": "Ring twice"},
        headers={**sara, "Idempotency-Key": key},
    )
    order = assert_conforms("POST", "/api/orders", created)
    assert created.status_code == 201
    assert order["status"] == "CREATED" and order["paymentStatus"] == "PENDING"
    assert order["restaurantId"] == menu["restaurantId"]
    assert order["subtotalAmount"] == "24.00" and order["deliveryFee"] == "3.00"
    assert order["totalAmount"] == "27.00"
    assert order["deliveryAddress"] == "742 Evergreen Terrace, Apt 3B"  # the profile address
    assert order["deliveryNotes"] == "Ring twice"
    assert [(i["name"], i["quantity"], i["price"]) for i in order["items"]] == [
        ("QA Burger", 2, "10.00"),
        ("QA Fries", 1, "4.00"),
    ]
    assert all("id" in i for i in order["items"])
    # the cart is consumed, the same key returns the same order
    assert (await client.get("/api/cart", headers=sara)).json()["items"] == []
    replay = await client.post("/api/orders", json={}, headers={**sara, "Idempotency-Key": key})
    assert replay.status_code == 201 and replay.json()["id"] == order["id"]
    fetched = await client.get(f"/api/orders/{order['id']}", headers=sara)
    assert fetched.status_code == 200 and fetched.json()["id"] == order["id"]
    listed = (await client.get("/api/orders?status=active", headers=sara)).json()
    assert [o["id"] for o in listed["items"]] == [order["id"]] and listed["total"] == 1
    assert (await client.get("/api/orders?status=completed", headers=sara)).json()["items"] == []


async def test_order_refusals(client) -> None:
    sara = await login(client, "demo.customer@example.com")
    empty = await client.post("/api/orders", json={}, headers=sara)
    assert empty.status_code == 400
    menu = await _fill_cart(client, sara)
    far = await client.post(
        "/api/orders",
        json={"deliveryAddress": "Alexandria", "deliveryLatitude": 31.2, "deliveryLongitude": 29.9},
        headers=sara,
    )
    assert far.status_code == 409 and far.json()["error"] == "OutOfDeliveryArea"
    owner = await login(client, QA_OWNER)
    await client.patch(
        f"/api/restaurants/{menu['restaurantId']}/status", json={"status": "BUSY"}, headers=owner
    )
    busy = await client.post("/api/orders", json={}, headers=sara)
    assert busy.status_code == 409 and busy.json()["error"] == "RestaurantBusy"
    await client.patch(
        f"/api/restaurants/{menu['restaurantId']}/status", json={"status": "CLOSED"}, headers=owner
    )
    closed = await client.post("/api/orders", json={}, headers=sara)
    assert closed.status_code == 409 and closed.json()["error"] == "RestaurantClosed"
    await client.patch(
        f"/api/restaurants/{menu['restaurantId']}/status", json={"status": "OPEN"}, headers=owner
    )
    await client.patch(
        f"/api/menus/menu-items/{menu['QA Fries']['id']}", json={"price": 4.5}, headers=owner
    )
    drifted = await client.post("/api/orders", json={}, headers=sara)
    assert drifted.status_code == 409 and drifted.json()["error"] == "CartChanged"
    assert drifted.json()["details"]["changes"][0]["change"] == "PRICE_CHANGED"
    assert (await client.post("/api/orders", json={}, headers=sara)).status_code == 201


async def test_unverified_phone_and_missing_address_are_refused(client) -> None:
    registered = await client.post(
        "/api/auth/register",
        json={"email": "new.customer@example.com", "password": "secret12", "fullName": "New One"},
    )
    new = {"Authorization": f"Bearer {registered.json()['accessToken']}"}
    await _fill_cart(client, new)
    refused = await client.post("/api/orders", json={}, headers=new)
    assert refused.status_code == 403 and refused.json()["error"] == "PhoneNotVerified"
    await client.post(
        "/api/users/me/phone/verification", json={"phone": "+20 100 000 0000"}, headers=new
    )
    await client.post("/api/users/me/phone/verify", json={"code": "123456"}, headers=new)
    no_address = await client.post("/api/orders", json={}, headers=new)
    assert no_address.status_code == 400
    with_address = await client.post(
        "/api/orders", json={"deliveryAddress": "1 Tahrir Square"}, headers=new
    )
    assert with_address.status_code == 201


async def test_customer_cancels_while_allowed(client) -> None:
    sara = await login(client, "demo.customer@example.com")
    await _fill_cart(client, sara)
    order = await _place(client, sara)
    cancelled = await client.patch(
        f"/api/orders/{order['id']}/status", json={"status": "CANCELLED"}, headers=sara
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "CANCELLED"
    assert cancelled.json()["cancelledBy"] == "CUSTOMER"
    listed = (await client.get("/api/orders?status=cancelled", headers=sara)).json()
    assert [o["id"] for o in listed["items"]] == [order["id"]]
    again = await client.patch(
        f"/api/orders/{order['id']}/status", json={"status": "CANCELLED"}, headers=sara
    )
    assert again.status_code == 409 and again.json()["error"] == "InvalidTransition"
    other = await client.patch(
        f"/api/orders/{order['id']}/status", json={"status": "PREPARING"}, headers=sara
    )
    assert other.status_code == 403


async def test_kitchen_board_transitions_and_summary(client) -> None:
    sara = await login(client, "demo.customer@example.com")
    owner = await login(client, QA_OWNER)
    menu = await _fill_cart(client, sara)
    order = await _place(client, sara)
    rid = menu["restaurantId"]
    board = await client.get(f"/api/orders/restaurant/{rid}", headers=owner)
    assert board.status_code == 200
    row = board.json()["items"][0]
    assert row["id"] == order["id"] and row["customerFirstName"] == "Sara"
    assert row["deliveryAddress"] == "742 Evergreen Terrace, Apt 3B"
    assert (await client.get(f"/api/orders/restaurant/{rid}", headers=sara)).status_code == 403
    # the kitchen cannot start before the order is paid
    early = await client.patch(
        f"/api/orders/{order['id']}/status", json={"status": "PREPARING"}, headers=owner
    )
    assert early.status_code == 409 and early.json()["error"] == "InvalidTransition"
    # payments are issue #7; confirm through the domain function the payment service will call
    from foood.orders import confirm_paid

    confirm_paid(client.store, order["id"])
    preparing = await client.patch(
        f"/api/orders/{order['id']}/status", json={"status": "PREPARING"}, headers=owner
    )
    assert preparing.status_code == 200 and preparing.json()["status"] == "PREPARING"
    ready = await client.patch(
        f"/api/orders/{order['id']}/status", json={"status": "READY_FOR_PICKUP"}, headers=owner
    )
    assert ready.json()["status"] == "READY_FOR_PICKUP"
    late = await client.patch(
        f"/api/orders/{order['id']}/status", json={"status": "CANCELLED"}, headers=sara
    )
    assert late.status_code == 409  # past the customer's window
    summary = (await client.get(f"/api/orders/restaurant/{rid}/summary", headers=owner)).json()
    assert summary == {
        "orders": 1,
        "active": 1,
        "delivered": 0,
        "cancelled": 0,
        "revenue": "24.00",
        "currency": "EGP",
    }


async def test_kitchen_rejects_with_a_reason(client) -> None:
    sara = await login(client, "demo.customer@example.com")
    owner = await login(client, QA_OWNER)
    await _fill_cart(client, sara)
    order = await _place(client, sara)
    from foood.orders import confirm_paid

    confirm_paid(client.store, order["id"])
    rejected = await client.patch(
        f"/api/orders/{order['id']}/status",
        json={"status": "CANCELLED", "reason": "Out of buns"},
        headers=owner,
    )
    assert rejected.status_code == 200
    body = rejected.json()
    assert body["status"] == "CANCELLED" and body["cancelledBy"] == "RESTAURANT"
    assert body["cancellationReason"] == "RESTAURANT_REJECTED"
    assert body["cancellationNote"] == "Out of buns"
    assert body["paymentStatus"] == "REFUNDED"
