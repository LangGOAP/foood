"""Simulated payments (issue #7): the checkout's create -> process pair and refunds."""

from __future__ import annotations

import uuid

import pytest

from tests.support import login


async def _order(client, headers: dict[str, str]) -> dict:
    restaurants = (await client.get("/api/restaurants?search=QA")).json()["items"]
    menu = (await client.get(f"/api/menus/restaurants/{restaurants[0]['id']}/menu")).json()
    burger = next(i for i in menu["items"] if i["name"] == "QA Burger")
    await client.post(
        "/api/cart/items", json={"menuItemId": burger["id"], "quantity": 1}, headers=headers
    )
    created = await client.post("/api/orders", json={}, headers=headers)
    assert created.status_code == 201, created.text
    return created.json()


async def test_checkout_pays_and_confirms_the_order(client) -> None:
    sara = await login(client, "demo.customer@example.com")
    order = await _order(client, sara)
    key = str(uuid.uuid4())
    created = await client.post(
        "/api/payments", json={"orderId": order["id"]}, headers={**sara, "Idempotency-Key": key}
    )
    assert created.status_code == 201, created.text
    payment = created.json()
    assert payment["status"] == "PENDING" and payment["amount"] == "13.00"
    assert payment["orderId"] == order["id"] and payment["customerId"] == order["customerId"]
    assert (await client.get(f"/api/orders/{order['id']}", headers=sara)).json()[
        "status"
    ] == "PAYMENT_PENDING"
    replay = await client.post(
        "/api/payments", json={"orderId": order["id"]}, headers={**sara, "Idempotency-Key": key}
    )
    assert replay.status_code == 201 and replay.json()["id"] == payment["id"]
    processed = await client.post(f"/api/payments/{payment['id']}/process", json={}, headers=sara)
    assert processed.status_code == 201 and processed.json()["status"] == "COMPLETED"
    fetched = await client.get(f"/api/payments/{payment['id']}", headers=sara)
    assert fetched.status_code == 200 and fetched.json()["status"] == "COMPLETED"
    confirmed = (await client.get(f"/api/orders/{order['id']}", headers=sara)).json()
    assert confirmed["status"] == "CONFIRMED" and confirmed["paymentStatus"] == "COMPLETED"
    # processing twice is harmless
    again = await client.post(f"/api/payments/{payment['id']}/process", json={}, headers=sara)
    assert again.status_code == 201 and again.json()["status"] == "COMPLETED"


async def test_payment_refusals(client) -> None:
    sara = await login(client, "demo.customer@example.com")
    driver = await login(client, "demo.driver@example.com")
    missing = await client.post("/api/payments", json={"orderId": "nope"}, headers=sara)
    assert missing.status_code == 404
    order = await _order(client, sara)
    foreign = await client.post("/api/payments", json={"orderId": order["id"]}, headers=driver)
    assert foreign.status_code == 403
    await client.patch(
        f"/api/orders/{order['id']}/status", json={"status": "CANCELLED"}, headers=sara
    )
    cancelled = await client.post("/api/payments", json={"orderId": order["id"]}, headers=sara)
    assert cancelled.status_code == 409 and cancelled.json()["error"] == "OrderNotPayable"
    hook = await client.post(
        "/api/payments/nope/process", json={"simulateFailure": True}, headers=sara
    )
    assert hook.status_code == 400 and "test hook" in hook.json()["message"]
    assert (await client.get("/api/payments/nope", headers=sara)).status_code == 404


async def test_declined_card_cancels_the_order(client, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PAYMENT_TEST_HOOKS", "true")
    sara = await login(client, "demo.customer@example.com")
    order = await _order(client, sara)
    payment = (
        await client.post("/api/payments", json={"orderId": order["id"]}, headers=sara)
    ).json()
    declined = await client.post(
        f"/api/payments/{payment['id']}/process", json={"simulateFailure": True}, headers=sara
    )
    assert declined.status_code == 201
    assert declined.json()["status"] == "FAILED" and declined.json()["failureReason"]
    failed = (await client.get(f"/api/orders/{order['id']}", headers=sara)).json()
    assert failed["status"] == "CANCELLED" and failed["paymentStatus"] == "FAILED"
    assert failed["cancelledBy"] == "PAYMENT" and failed["cancellationReason"] == "PAYMENT_FAILED"
    retry = await client.post(f"/api/payments/{payment['id']}/process", json={}, headers=sara)
    assert retry.status_code == 409


def test_charge_hook_decides_the_outcome() -> None:
    """The scenario engine replaces the charge hook; the default approves everything."""
    from decimal import Decimal

    from foood.payments import Payment, charge
    from foood.store import Store

    store = Store()
    payment = Payment(order_id="o", customer_id="c", amount=Decimal("10"))
    assert charge(store, payment) == ("COMPLETED", None)
    store.hooks["charge"] = lambda p: ("FAILED", "Insufficient funds")
    assert charge(store, payment) == ("FAILED", "Insufficient funds")


async def test_cancelling_a_paid_order_refunds_it(client) -> None:
    sara = await login(client, "demo.customer@example.com")
    admin = await login(client, "admin@example.com")
    order = await _order(client, sara)
    payment = (
        await client.post("/api/payments", json={"orderId": order["id"]}, headers=sara)
    ).json()
    await client.post(f"/api/payments/{payment['id']}/process", json={}, headers=sara)
    none_yet = await client.get(f"/api/payments/{payment['id']}/refund", headers=sara)
    assert none_yet.status_code == 404
    cancelled = await client.patch(
        f"/api/orders/{order['id']}/status", json={"status": "CANCELLED"}, headers=sara
    )
    assert cancelled.json()["paymentStatus"] == "REFUNDED"
    assert (await client.get(f"/api/payments/{payment['id']}", headers=sara)).json()[
        "status"
    ] == "REFUNDED"
    refund = await client.get(f"/api/payments/{payment['id']}/refund", headers=sara)
    assert refund.status_code == 200
    assert refund.json()["amount"] == "13.00" and refund.json()["status"] == "COMPLETED"
    assert refund.json()["reason"] == "ORDER_CANCELLED"
    twice = await client.post(f"/api/payments/{payment['id']}/refund", json={}, headers=admin)
    assert twice.status_code == 409
    not_admin = await client.post(f"/api/payments/{payment['id']}/refund", json={}, headers=sara)
    assert not_admin.status_code == 403


async def test_admin_refunds_a_completed_payment(client) -> None:
    sara = await login(client, "demo.customer@example.com")
    admin = await login(client, "admin@example.com")
    order = await _order(client, sara)
    payment = (
        await client.post("/api/payments", json={"orderId": order["id"]}, headers=sara)
    ).json()
    await client.post(f"/api/payments/{payment['id']}/process", json={}, headers=sara)
    refunded = await client.post(
        f"/api/payments/{payment['id']}/refund", json={"reason": "Goodwill"}, headers=admin
    )
    assert refunded.status_code == 201 and refunded.json()["reason"] == "Goodwill"
    assert (await client.get(f"/api/payments/{payment['id']}", headers=sara)).json()[
        "status"
    ] == "REFUNDED"
    assert (await client.get(f"/api/orders/{order['id']}", headers=sara)).json()[
        "paymentStatus"
    ] == "REFUNDED"
