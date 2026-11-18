"""LLM intake (issue #18): a sentence becomes an order, or a question."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest

from foood.app import create_app
from foood.intake import IntakeRequest, OpenAIIntake, ScriptedIntake, WantedItem, catalogue_text
from tests.support import login


@asynccontextmanager
async def _app(intake) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(simulate=False, intake=intake)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        c.store = app.state.store  # type: ignore[attr-defined]
        yield c


async def test_a_recorded_reading_places_the_expected_order() -> None:
    reading = IntakeRequest(
        restaurant="QA Kitchen",
        items=[WantedItem(name="QA Burger", quantity=2), WantedItem(name="qa fries")],
        delivery_notes="Ring twice",
    )
    intake = ScriptedIntake(reading)
    async with _app(intake) as client:
        sara = await login(client, "demo.customer@example.com")
        reply = await client.post(
            "/api/orders/intake",
            json={"text": "two QA burgers and fries to the office, ring twice"},
            headers=sara,
        )
        assert reply.status_code == 200, reply.text
        body = reply.json()
        assert body["status"] == "placed"
        assert body["usage"] == {"model": "scripted", "tokens": 100, "cost": 0, "latencyMs": 30.0}
        order = body["order"]
        assert [(i["name"], i["quantity"]) for i in order["items"]] == [
            ("QA Burger", 2),
            ("QA Fries", 1),
        ]
        assert order["deliveryNotes"] == "Ring twice" and order["deliveryAddress"].startswith(
            "742 Evergreen"
        )
        assert order["status"] == "CREATED" and order["totalAmount"] == "27.00"
        text, catalogue = intake.seen[0]
        assert "ring twice" in text and "QA Kitchen: QA Burger (10.00)" in catalogue
        assert "QA Night Cafe" not in catalogue  # closed kitchens are not offered
        mine = (await client.get("/api/orders", headers=sara)).json()
        assert [o["id"] for o in mine["items"]] == [order["id"]]  # it is an ordinary order
        decisions = (await client.get(f"/api/_engine?orderId={order['id']}")).json()["decisions"][
            order["id"]
        ]
        assert decisions[0]["decision"] == "intake" and decisions[0]["llmTokens"] == 100


async def test_unclear_requests_come_back_as_questions() -> None:
    intake = ScriptedIntake(
        IntakeRequest(
            items=[WantedItem(name="Margherita")]
        ),  # Pizza Napoli and Luigi's both have it
        IntakeRequest(restaurant="QA Kitchen", items=[WantedItem(name="QA Soup")]),  # sold out
        IntakeRequest(items=[WantedItem(name="Beef Wellington")]),  # nobody has it
        IntakeRequest(question="Did you mean the QA Kitchen or the QA Bistro?"),  # the model asks
        IntakeRequest(items=[]),
    )
    async with _app(intake) as client:
        sara = await login(client, "demo.customer@example.com")
        answers = []
        for _ in range(5):
            reply = await client.post(
                "/api/orders/intake", json={"text": "something"}, headers=sara
            )
            assert reply.status_code == 200 and reply.json()["status"] == "question"
            answers.append(reply.json()["question"])
        assert "Several kitchens" in answers[0] and "Luigi's Pizzeria" in answers[0]
        assert "QA Kitchen does not have: QA Soup" in answers[1]
        assert "No open kitchen has all of: Beef Wellington" in answers[2]
        assert answers[3] == "Did you mean the QA Kitchen or the QA Bistro?"
        assert "What would you like" in answers[4]
        assert (await client.get("/api/orders", headers=sara)).json()["total"] == 0


async def test_the_platforms_guards_still_apply() -> None:
    intake = ScriptedIntake(
        IntakeRequest(restaurant="QA Kitchen", items=[WantedItem(name="QA Pasta")])
    )
    async with _app(intake) as client:
        registered = await client.post(
            "/api/auth/register",
            json={"email": "newbie@example.com", "password": "secret12", "fullName": "New Bie"},
        )
        new = {"Authorization": f"Bearer {registered.json()['accessToken']}"}
        refused = await client.post(
            "/api/orders/intake", json={"text": "pasta please"}, headers=new
        )
        assert refused.status_code == 403 and refused.json()["error"] == "PhoneNotVerified"


async def test_without_a_model_intake_says_so() -> None:
    async with _app(None) as client:
        sara = await login(client, "demo.customer@example.com")
        reply = await client.post("/api/orders/intake", json={"text": "a burger"}, headers=sara)
        assert reply.status_code == 503 and reply.json()["error"] == "NoModel"


def test_catalogue_lists_open_kitchens_only() -> None:
    from foood.store import Store, seed_demo

    text = catalogue_text(seed_demo(Store()))
    assert (
        "- QA Kitchen: QA Burger (10.00), QA Pasta (12.50), QA Fries (4.00)" in text
    )  # no sold-out soup
    assert "QA Night Cafe" not in text and "Green Bowl" not in text  # closed kitchens


@pytest.mark.skipif(not os.environ.get("OPENAI_API_KEY"), reason="needs OPENAI_API_KEY")
async def test_live_model_reads_a_real_sentence() -> None:
    async with _app(OpenAIIntake()) as client:
        sara = await login(client, "demo.customer@example.com")
        reply = await client.post(
            "/api/orders/intake",
            json={"text": "Two QA burgers and one QA fries from the QA Kitchen, please ring twice"},
            headers=sara,
        )
        assert reply.status_code == 200, reply.text
        body = reply.json()
        assert body["status"] == "placed", body
        assert sorted((i["name"], i["quantity"]) for i in body["order"]["items"]) == [
            ("QA Burger", 2),
            ("QA Fries", 1),
        ]
        assert body["usage"]["tokens"] > 0
