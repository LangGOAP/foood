"""Credits (issue #24): compensation the customer sees, applied at the next checkout."""

from __future__ import annotations

from decimal import Decimal

from foood.credits import balance, issue_credit
from tests.support import login


async def _cart(client, headers, item: str = "QA Pasta", quantity: int = 1) -> None:
    restaurants = (await client.get("/api/restaurants?search=QA Kitchen")).json()["items"]
    menu = (await client.get(f"/api/menus/restaurants/{restaurants[0]['id']}/menu")).json()
    chosen = next(i for i in menu["items"] if i["name"] == item)
    await client.post(
        "/api/cart/items", json={"menuItemId": chosen["id"], "quantity": quantity}, headers=headers
    )


async def test_support_issues_a_credit_and_the_customer_sees_it(client) -> None:
    sara = await login(client, "demo.customer@example.com")
    admin = await login(client, "admin@example.com")
    me = (await client.get("/api/auth/me", headers=sara)).json()
    assert (await client.get("/api/credits/me", headers=sara)).json() == {
        "currency": "EGP",
        "balance": "0.00",
        "entries": [],
    }
    issued = await client.post(
        "/api/credits",
        json={"customerId": me["sub"], "amount": 5, "reason": "Sorry for the wait"},
        headers=admin,
    )
    assert issued.status_code == 201 and issued.json()["amount"] == "5.00"
    mine = (await client.get("/api/credits/me", headers=sara)).json()
    assert mine["balance"] == "5.00" and mine["entries"][0]["reason"] == "Sorry for the wait"
    assert (await client.get(f"/api/credits/{me['sub']}", headers=admin)).json()[
        "balance"
    ] == "5.00"
    newest = (await client.get("/api/notifications", headers=sara)).json()["items"][0]
    assert (
        newest["messageKey"] == "credit.issued"
        and "5.00" in newest["message"]
        and "wait" in newest["message"]
    )
    assert (
        await client.post(
            "/api/credits", json={"customerId": me["sub"], "amount": 1, "reason": "x"}, headers=sara
        )
    ).status_code == 403
    assert (
        await client.post(
            "/api/credits", json={"customerId": "nobody", "amount": 1, "reason": "x"}, headers=admin
        )
    ).status_code == 404
    assert (
        await client.post(
            "/api/credits",
            json={"customerId": me["sub"], "amount": 0, "reason": "x"},
            headers=admin,
        )
    ).status_code == 400
    other = await login(client, "qa.customer@delivery-plus.test")
    assert (await client.get(f"/api/credits/{me['sub']}", headers=other)).status_code == 403


async def test_the_next_checkout_applies_the_balance(client) -> None:
    sara = await login(client, "demo.customer@example.com")
    me = (await client.get("/api/auth/me", headers=sara)).json()
    issue_credit(client.store, me["sub"], Decimal("20"), "Late delivery")
    await _cart(client, sara)  # 12.50 + 3.00 fee = 15.50 gross
    quote = (await client.get("/api/orders/quote", headers=sara)).json()
    assert quote["subtotalAmount"] == "12.50" and quote["deliveryFee"] == "3.00"
    assert quote["creditApplied"] == "15.50" and quote["totalAmount"] == "0.00"
    order = (await client.post("/api/orders", json={}, headers=sara)).json()
    assert (
        order["creditApplied"] == "15.50"
        and order["totalAmount"] == "0.00"
        and order["grossAmount"] == "15.50"
    )
    assert balance(client.store, me["sub"]) == Decimal("4.50")
    payment = (
        await client.post("/api/payments", json={"orderId": order["id"]}, headers=sara)
    ).json()
    assert payment["amount"] == "0.00"
    processed = (
        await client.post(f"/api/payments/{payment['id']}/process", json={}, headers=sara)
    ).json()
    assert processed["status"] == "COMPLETED"
    assert (await client.get(f"/api/orders/{order['id']}", headers=sara)).json()[
        "status"
    ] == "CONFIRMED"
    # the remainder covers part of the next one
    await _cart(client, sara, item="QA Burger")  # 10.00 + 3.00
    second = (await client.post("/api/orders", json={}, headers=sara)).json()
    assert second["creditApplied"] == "4.50" and second["totalAmount"] == "8.50"
    assert balance(client.store, me["sub"]) == Decimal("0")
    ledger = (await client.get("/api/credits/me", headers=sara)).json()
    assert [e["reason"] for e in ledger["entries"]] == ["APPLIED", "APPLIED", "Late delivery"]
    assert ledger["entries"][0]["amount"] == "-4.50"


async def test_a_cancelled_order_gives_the_credit_back(client) -> None:
    sara = await login(client, "demo.customer@example.com")
    me = (await client.get("/api/auth/me", headers=sara)).json()
    issue_credit(client.store, me["sub"], Decimal("10"), "Goodwill")
    await _cart(client, sara, item="QA Burger")  # 13.00 gross
    order = (await client.post("/api/orders", json={}, headers=sara)).json()
    assert order["creditApplied"] == "10.00" and balance(client.store, me["sub"]) == Decimal("0")
    await client.patch(
        f"/api/orders/{order['id']}/status", json={"status": "CANCELLED"}, headers=sara
    )
    assert balance(client.store, me["sub"]) == Decimal("10")
    ledger = (await client.get("/api/credits/me", headers=sara)).json()
    assert (
        ledger["entries"][0]["reason"] == "RETURNED"
        and ledger["entries"][0]["orderId"] == order["id"]
    )
    # the kitchen summary and the restaurant revenue still count the full subtotal
    await _cart(client, sara, item="QA Burger")
    again = (await client.post("/api/orders", json={}, headers=sara)).json()
    assert again["subtotalAmount"] == "10.00" and again["creditApplied"] == "10.00"


async def test_one_compensation_per_order(client) -> None:
    sara = await login(client, "demo.customer@example.com")
    admin = await login(client, "admin@example.com")
    await _cart(client, sara)
    order = (await client.post("/api/orders", json={}, headers=sara)).json()
    first = await client.post(
        "/api/credits", json={"orderId": order["id"], "amount": 3, "reason": "Late"}, headers=admin
    )
    assert first.status_code == 201 and first.json()["orderId"] == order["id"]
    twice = await client.post(
        "/api/credits",
        json={"orderId": order["id"], "amount": 3, "reason": "Late again"},
        headers=admin,
    )
    assert twice.status_code == 409 and twice.json()["error"] == "AlreadyCompensated"
    assert (
        await client.post(
            "/api/credits", json={"orderId": "nope", "amount": 3, "reason": "x"}, headers=admin
        )
    ).status_code == 404
    assert (await client.get("/api/credits/me", headers=sara)).json()["balance"] == "3.00"
