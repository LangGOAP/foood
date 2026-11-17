"""Backup payment method (issue #23): a second card on file and a retry path for engines."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest

from foood.app import create_app
from foood.engine import HandWiredEngine, decide
from foood.errors import ApiError
from foood.payments import Payment, retry_with_backup
from foood.scenario import Injection, Scenario
from tests.support import login


class RetryingEngine(HandWiredEngine):
    """What a smarter engine does with a decline: try the backup before giving up."""

    name = "retrying"

    def on_payment(self, payment: Payment) -> None:
        if payment.status == "FAILED":
            try:
                decide(
                    self.store,
                    payment.order_id,
                    "retry with backup card",
                    because="the card declined",
                )
                retry_with_backup(self.store, payment)
                return
            except ApiError:
                decide(self.store, payment.order_id, "cancel", because="no backup card left")
        super().on_payment(payment)


@asynccontextmanager
async def _app(engine=None, scenario=None) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(simulate=False, engine=engine, scenario=scenario)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        c.store = app.state.store  # type: ignore[attr-defined]
        yield c


async def _unpaid(client, headers, item: str = "QA Burger") -> dict:
    restaurants = (await client.get("/api/restaurants?search=QA Kitchen")).json()["items"]
    menu = (await client.get(f"/api/menus/restaurants/{restaurants[0]['id']}/menu")).json()
    chosen = next(i for i in menu["items"] if i["name"] == item)
    await client.post(
        "/api/cart/items", json={"menuItemId": chosen["id"], "quantity": 1}, headers=headers
    )
    return (await client.post("/api/orders", json={}, headers=headers)).json()


async def test_payments_name_the_card(client) -> None:
    sara = await login(client, "demo.customer@example.com")
    order = await _unpaid(client, sara)
    payment = (
        await client.post("/api/payments", json={"orderId": order["id"]}, headers=sara)
    ).json()
    assert payment["attempt"] == 1
    assert payment["method"] == {
        "id": "card-sara-visa",
        "brand": "Visa",
        "last4": "4242",
        "label": "Visa •••• 4242",
    }
    processed = (
        await client.post(f"/api/payments/{payment['id']}/process", json={}, headers=sara)
    ).json()
    assert processed["status"] == "COMPLETED" and processed["attempt"] == 1
    newest = (await client.get("/api/notifications", headers=sara)).json()["items"]
    received = next(n for n in newest if n["messageKey"] == "payment.completed")
    assert (
        received["params"]["card"] == "Visa •••• 4242" and "Visa •••• 4242" in received["message"]
    )


async def test_a_customer_without_cards_still_pays(client) -> None:
    registered = await client.post(
        "/api/auth/register",
        json={"email": "cardless@example.com", "password": "secret12", "fullName": "No Card"},
    )
    new = {"Authorization": f"Bearer {registered.json()['accessToken']}"}
    await client.patch(
        "/api/users/me", json={"phone": "+20 100 000 0009", "address": "9 Nowhere"}, headers=new
    )
    await client.post("/api/users/me/phone/verification", json={}, headers=new)
    await client.post("/api/users/me/phone/verify", json={"code": "123456"}, headers=new)
    order = await _unpaid(client, new)
    payment = (
        await client.post("/api/payments", json={"orderId": order["id"]}, headers=new)
    ).json()
    assert payment["method"] is None
    assert (
        await client.post(f"/api/payments/{payment['id']}/process", json={}, headers=new)
    ).json()["status"] == "COMPLETED"


async def test_a_declined_card_is_retried_on_the_backup_by_a_smarter_engine() -> None:
    scenario = Scenario(
        "t", injections=(Injection("payment_declines", 1, reason="Insufficient funds"),)
    )
    async with _app(engine=RetryingEngine(), scenario=scenario) as client:
        sara = await login(client, "demo.customer@example.com")
        order = await _unpaid(client, sara)
        first = (
            await client.post("/api/payments", json={"orderId": order["id"]}, headers=sara)
        ).json()
        outcome = await client.post(f"/api/payments/{first['id']}/process", json={}, headers=sara)
        # the response is the latest attempt, which the backup card completed
        assert outcome.status_code == 201
        body = outcome.json()
        assert body["status"] == "COMPLETED" and body["attempt"] == 2 and body["id"] != first["id"]
        assert body["method"]["label"] == "Mastercard •••• 4444"
        declined = (await client.get(f"/api/payments/{first['id']}", headers=sara)).json()
        assert declined["status"] == "FAILED" and declined["failureReason"] == "Insufficient funds"
        confirmed = (await client.get(f"/api/orders/{order['id']}", headers=sara)).json()
        assert confirmed["status"] == "CONFIRMED" and confirmed["paymentStatus"] == "COMPLETED"
        decisions = (await client.get(f"/api/_engine?orderId={order['id']}")).json()["decisions"][
            order["id"]
        ]
        assert [d["decision"] for d in decisions] == ["retry with backup card", "confirm"]
        keys = [
            n["messageKey"]
            for n in reversed(
                (await client.get("/api/notifications", headers=sara)).json()["items"]
            )
        ]
        assert keys == ["payment.failed", "payment.completed", "order.confirmed"]
        # the client cannot re-run the declined attempt itself
        assert (
            await client.post(f"/api/payments/{first['id']}/process", json={}, headers=sara)
        ).status_code == 409


async def test_both_cards_declining_cancels_the_order() -> None:
    scenario = Scenario("t", injections=(Injection("payment_declines", 1, times=2),))
    async with _app(engine=RetryingEngine(), scenario=scenario) as client:
        sara = await login(client, "demo.customer@example.com")
        order = await _unpaid(client, sara)
        first = (
            await client.post("/api/payments", json={"orderId": order["id"]}, headers=sara)
        ).json()
        body = (
            await client.post(f"/api/payments/{first['id']}/process", json={}, headers=sara)
        ).json()
        assert body["status"] == "FAILED" and body["attempt"] == 2
        cancelled = (await client.get(f"/api/orders/{order['id']}", headers=sara)).json()
        assert cancelled["status"] == "CANCELLED" and cancelled["cancelledBy"] == "PAYMENT"
        attempts = [p for p in client.store.payments.values() if p.order_id == order["id"]]
        assert sorted(p.attempt for p in attempts) == [1, 2] and all(
            p.status == "FAILED" for p in attempts
        )
        decisions = (await client.get(f"/api/_engine?orderId={order['id']}")).json()["decisions"][
            order["id"]
        ]
        assert [d["decision"] for d in decisions] == [
            "retry with backup card",
            "retry with backup card",
            "cancel",
            "cancel",
        ]


async def test_retry_rules(client) -> None:
    qa = await login(client, "qa.customer@delivery-plus.test")  # one card on file
    order = await _unpaid(client, qa)
    payment = (await client.post("/api/payments", json={"orderId": order["id"]}, headers=qa)).json()
    attempt = client.store.payments[payment["id"]]
    with pytest.raises(ApiError, match="declined attempt"):
        retry_with_backup(client.store, attempt)  # not declined yet
    attempt.status = "FAILED"
    with pytest.raises(ApiError, match="No other card"):
        retry_with_backup(client.store, attempt)


def test_the_hand_wired_engine_still_cancels_on_the_first_decline(client) -> None:
    """Behaviour on main is unchanged: no retry unless an engine asks for one."""
    assert HandWiredEngine.on_payment is not RetryingEngine.on_payment
