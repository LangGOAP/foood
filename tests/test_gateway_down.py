"""A service outage (issue #36): the payment gateway goes down for a while.

Every payment processed in the window is refused with a 503 and stays pending; the
platform says when the gateway goes down and when it is back. The hand-wired engine
treats the refusal as a payment failure and cancels, as most shops do; an engine that
can wait retries the pending attempt once the gateway is back.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx

from foood.app import create_app
from foood.bench import run
from foood.engine import make_engine
from foood.payments import retry_pending_payment
from foood.scenario import FAILURES, Injection, Scenario
from foood.store import CLOCK
from tests.support import login

OUTAGE = Scenario("t", injections=(Injection("gateway_down", 1, for_seconds=60),))


@asynccontextmanager
async def _app(engine: str = "hand-wired") -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(simulate=False, engine=make_engine(engine), scenario=OUTAGE)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        c.store = app.state.store  # type: ignore[attr-defined]
        yield c


async def _placed(client, customer) -> tuple[dict, dict]:
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
    return order, payment


def _advance(store, seconds: float) -> None:
    CLOCK.advance(seconds)
    store.engine.tick()


def test_the_failure_is_documented() -> None:
    assert "gateway_down" in FAILURES


async def test_the_gateway_refuses_payments_and_comes_back() -> None:
    async with _app("null") as client:
        heard: list[str] = []
        for event in ("payment.gateway_down", "payment.unavailable", "payment.gateway_up"):
            client.store.subscribe(event, lambda *a, _e=event, **k: heard.append(_e))
        sara = await login(client, "demo.customer@example.com")
        order, payment = await _placed(client, sara)
        refused = await client.post(f"/api/payments/{payment['id']}/process", json={}, headers=sara)
        assert refused.status_code == 503
        assert refused.json()["error"] == "PaymentGatewayUnavailable"
        still = (await client.get(f"/api/payments/{payment['id']}", headers=sara)).json()
        assert still["status"] == "PENDING"
        assert heard == ["payment.gateway_down", "payment.unavailable"]
        log = client.store.engine.status()["log"]
        assert any(e["event"] == "fired" and e.get("failure") == "gateway_down" for e in log)
        _advance(client.store, 61)
        assert heard[-1] == "payment.gateway_up"
        log = client.store.engine.status()["log"]  # the status is a copy; read it again
        assert any(e["event"] == "cleared" and e.get("failure") == "gateway_down" for e in log)
        paid = await client.post(f"/api/payments/{payment['id']}/process", json={}, headers=sara)
        assert paid.status_code == 201 and paid.json()["status"] == "COMPLETED"


async def test_an_engine_that_waits_retries_when_the_gateway_is_back() -> None:
    async with _app("null") as client:
        sara = await login(client, "demo.customer@example.com")
        order, payment = await _placed(client, sara)
        await client.post(f"/api/payments/{payment['id']}/process", json={}, headers=sara)
        assert retry_pending_payment(client.store, order["id"]) is None  # still down
        _advance(client.store, 61)
        retried = retry_pending_payment(client.store, order["id"])
        assert retried is not None and retried.status == "COMPLETED"
        assert retry_pending_payment(client.store, order["id"]) is None  # nothing pending


async def test_the_hand_wired_engine_cancels_the_order() -> None:
    async with _app("hand-wired") as client:
        sara = await login(client, "demo.customer@example.com")
        order, payment = await _placed(client, sara)
        refused = await client.post(f"/api/payments/{payment['id']}/process", json={}, headers=sara)
        assert refused.status_code == 503
        cancelled = (await client.get(f"/api/orders/{order['id']}", headers=sara)).json()
        assert cancelled["status"] == "CANCELLED"
        assert cancelled["cancellationReason"] == "PAYMENT_GATEWAY_UNAVAILABLE"
        attempt = (await client.get(f"/api/payments/{payment['id']}", headers=sara)).json()
        assert attempt["status"] == "FAILED"
        decisions = client.store.decisions[order["id"]]
        assert ("cancel", "payment gateway unavailable") in [
            (d["decision"], d["because"]) for d in decisions
        ]
        # the customer taps pay again once the gateway is back: a cancelled order is not payable
        _advance(client.store, 61)
        again = await client.post(f"/api/payments/{payment['id']}/process", json={}, headers=sara)
        assert again.status_code == 409


def test_payment_trouble_meets_the_outage(tmp_path) -> None:
    result = run("payment-trouble", "hand-wired", 7, tmp_path)
    summary = result["summary"]
    assert summary["orders"] == 3 and "gateway_down" in summary["failuresFired"]
    assert "gateway_down" in summary["unrecovered"]
    third = next(o for o in result["orders"] if o["number"] == 3)
    assert third["status"] == "CANCELLED" and third["consistent"]
