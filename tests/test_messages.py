"""LLM-written messages (issue #19): the model's words, the templates underneath."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest

from foood.app import create_app
from foood.messages import OpenAIPhraser, ScriptedPhraser
from tests.support import login

QA_OWNER = "qa.restaurant@delivery-plus.test"


@asynccontextmanager
async def _app(phraser) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(simulate=False, phraser=phraser)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        c.store = app.state.store  # type: ignore[attr-defined]
        yield c


async def _paid(client, headers) -> dict:
    restaurants = (await client.get("/api/restaurants?search=QA Kitchen")).json()["items"]
    menu = (await client.get(f"/api/menus/restaurants/{restaurants[0]['id']}/menu")).json()
    pasta = next(i for i in menu["items"] if i["name"] == "QA Pasta")
    await client.post(
        "/api/cart/items", json={"menuItemId": pasta["id"], "quantity": 1}, headers=headers
    )
    order = (await client.post("/api/orders", json={}, headers=headers)).json()
    payment = (
        await client.post("/api/payments", json={"orderId": order["id"]}, headers=headers)
    ).json()
    await client.post(f"/api/payments/{payment['id']}/process", json={}, headers=headers)
    return order


async def test_the_models_words_keep_the_keys(client_phraser=None) -> None:
    phraser = ScriptedPhraser(
        "Payment received, Sara: $15.50 for your QA Pasta, order {short}.",
        "QA Kitchen has confirmed order {short} and will start on it shortly.",
        "Bad news on order {short}: QA Kitchen is out of pasta tonight, so they had to cancel. "
        "Your refund is on its way.",
        "Your $15.50 refund for order {short} has been issued.",
    )
    async with _app(phraser) as client:
        sara = await login(client, "demo.customer@example.com")
        owner = await login(client, QA_OWNER)
        order = await _paid(client, sara)
        await client.patch(
            f"/api/orders/{order['id']}/status",
            json={"status": "CANCELLED", "reason": "Out of pasta tonight"},
            headers=owner,
        )
        items = (await client.get("/api/notifications", headers=sara)).json()["items"]
        by_key = {n["messageKey"]: n for n in items}
        cancelled = by_key["order.cancelled"]
        assert (
            cancelled["message"].startswith("Bad news on order")
            and "out of pasta" in cancelled["message"]
        )
        assert cancelled["params"] == {
            "orderId": order["id"],
            "cancelledBy": "RESTAURANT",
            "note": "Out of pasta tonight",
        }
        assert cancelled["title"] == "Order cancelled" and cancelled["type"] == "ORDER_CANCELLED"
        facts = next(f for f in phraser.facts if f["event"] == "order.cancelled")
        assert (
            facts["cancellationNote"] == "Out of pasta tonight"
            and facts["restaurant"] == "QA Kitchen"
        )
        assert facts["template"].startswith("The restaurant cancelled your order")
        decisions = (await client.get(f"/api/_engine?orderId={order['id']}")).json()["decisions"][
            order["id"]
        ]
        phrased = [d for d in decisions if d["decision"] == "phrase"]
        assert len(phrased) == 4 and all(d["llmTokens"] == 110 for d in phrased)


async def test_a_failing_model_leaves_the_template() -> None:
    phraser = ScriptedPhraser(RuntimeError("timeout"), "", "x" * 1000)
    async with _app(phraser) as client:
        sara = await login(client, "demo.customer@example.com")
        order = await _paid(
            client, sara
        )  # three notifications... payment, confirm; cancel adds one more
        await client.patch(
            f"/api/orders/{order['id']}/status", json={"status": "CANCELLED"}, headers=sara
        )
        items = (await client.get("/api/notifications", headers=sara)).json()["items"]
        messages = {n["messageKey"]: n["message"] for n in items}
        assert messages["payment.completed"].startswith(
            "We received your payment for order"
        )  # the error
        assert messages["order.confirmed"].startswith("Your order")  # empty text
        assert messages["order.cancelled"].startswith("You cancelled your order")  # too long


async def test_without_a_model_the_texts_are_the_templates(client) -> None:
    sara = await login(client, "demo.customer@example.com")
    order = await _paid(client, sara)
    items = (await client.get("/api/notifications", headers=sara)).json()["items"]
    assert "phrase" not in client.store.hooks
    assert [n["message"] for n in reversed(items)] == [
        f"We received your payment for order {order['id'][:8]} with Visa •••• 4242.",
        f"Your order {order['id'][:8]} has been confirmed.",
    ]


@pytest.mark.skipif(not os.environ.get("OPENAI_API_KEY"), reason="needs OPENAI_API_KEY")
async def test_live_model_mentions_the_kitchens_reason() -> None:
    async with _app(OpenAIPhraser()) as client:
        sara = await login(client, "demo.customer@example.com")
        owner = await login(client, QA_OWNER)
        order = await _paid(client, sara)
        await client.patch(
            f"/api/orders/{order['id']}/status",
            json={"status": "CANCELLED", "reason": "The oven broke down"},
            headers=owner,
        )
        items = (await client.get("/api/notifications", headers=sara)).json()["items"]
        cancelled = next(n for n in items if n["messageKey"] == "order.cancelled")
        assert order["id"][:8] in cancelled["message"] and "oven" in cancelled["message"].lower()
        assert (
            cancelled["message"]
            != f"The restaurant cancelled your order {order['id'][:8]}: The oven broke down"
        )
