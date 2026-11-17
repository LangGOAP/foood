"""Support (issue #26): engines ask, a person answers, deadlines take the default."""

from __future__ import annotations

from foood.store import CLOCK
from foood.support import ask, expire_due
from tests.support import login


async def _order(client, headers) -> dict:
    restaurants = (await client.get("/api/restaurants?search=QA Kitchen")).json()["items"]
    menu = (await client.get(f"/api/menus/restaurants/{restaurants[0]['id']}/menu")).json()
    pasta = next(i for i in menu["items"] if i["name"] == "QA Pasta")
    await client.post(
        "/api/cart/items", json={"menuItemId": pasta["id"], "quantity": 1}, headers=headers
    )
    return (await client.post("/api/orders", json={}, headers=headers)).json()


async def test_a_question_is_asked_and_answered(client) -> None:
    sara = await login(client, "demo.customer@example.com")
    admin = await login(client, "admin@example.com")
    order = await _order(client, sara)
    answered: list = []
    client.store.subscribe("support.answered", lambda question: answered.append(question))
    question = ask(
        client.store,
        order["id"],
        "substitution",
        "Pizza Napoli can make this for 1.50 more. Go ahead?",
        options=[
            {"value": "yes", "label": "Yes, substitute"},
            {"value": "no", "label": "No, refund"},
        ],
        default="no",
        deadline_seconds=120,
        restaurant="Pizza Napoli",
    )
    listed = await client.get("/api/support/questions", headers=admin)
    assert listed.status_code == 200 and listed.json()["total"] == 1
    shown = listed.json()["items"][0]
    assert (
        shown["id"] == question.id
        and shown["status"] == "OPEN"
        and shown["context"] == {"restaurant": "Pizza Napoli"}
    )
    assert (await client.get("/api/support/questions", headers=sara)).status_code == 403
    bad = await client.post(
        f"/api/support/questions/{question.id}/answer", json={"value": "maybe"}, headers=admin
    )
    assert bad.status_code == 400
    ok = await client.post(
        f"/api/support/questions/{question.id}/answer", json={"value": "yes"}, headers=admin
    )
    assert (
        ok.status_code == 200 and ok.json()["status"] == "ANSWERED" and ok.json()["answer"] == "yes"
    )
    assert len(answered) == 1 and answered[0].answered_by != "deadline"
    twice = await client.post(
        f"/api/support/questions/{question.id}/answer", json={"value": "no"}, headers=admin
    )
    assert twice.status_code == 409
    assert (await client.get("/api/support/questions", headers=admin)).json()["total"] == 0
    assert (await client.get("/api/support/questions?status=all", headers=admin)).json()[
        "total"
    ] == 1
    assert (
        await client.post(
            "/api/support/questions/nope/answer", json={"value": "yes"}, headers=admin
        )
    ).status_code == 404


async def test_typed_answers_are_validated(client) -> None:
    sara = await login(client, "demo.customer@example.com")
    admin = await login(client, "admin@example.com")
    order = await _order(client, sara)
    form = {
        "type": "object",
        "required": ["amount"],
        "properties": {"amount": {"type": "number", "minimum": 0}, "note": {"type": "string"}},
    }
    question = ask(
        client.store,
        order["id"],
        "refund",
        "How much do we refund?",
        form=form,
        default={"amount": 0},
    )
    missing = await client.post(
        f"/api/support/questions/{question.id}/answer", json={"value": {"note": "x"}}, headers=admin
    )
    assert missing.status_code == 400 and "amount" in missing.json()["message"]
    negative = await client.post(
        f"/api/support/questions/{question.id}/answer",
        json={"value": {"amount": -1}},
        headers=admin,
    )
    assert negative.status_code == 400
    fine = await client.post(
        f"/api/support/questions/{question.id}/answer",
        json={"value": {"amount": 5, "note": "goodwill"}},
        headers=admin,
    )
    assert fine.status_code == 200 and fine.json()["answer"] == {"amount": 5, "note": "goodwill"}


async def test_the_deadline_takes_the_default(client) -> None:
    sara = await login(client, "demo.customer@example.com")
    order = await _order(client, sara)
    answered: list = []
    client.store.subscribe("support.answered", lambda question: answered.append(question))
    question = ask(
        client.store,
        order["id"],
        "wait",
        "Keep waiting for a driver?",
        options=[{"value": "wait", "label": "Wait"}, {"value": "cancel", "label": "Cancel"}],
        default="cancel",
        deadline_seconds=60,
    )
    assert expire_due(client.store) == [] and question.status == "OPEN"
    CLOCK.advance(61)
    assert expire_due(client.store) == [question]
    assert (
        question.status == "EXPIRED"
        and question.answer == "cancel"
        and question.answered_by == "deadline"
    )
    assert answered == [question]
    assert client.store.tickers, "the app installs the expiry ticker"
    client.store.tickers[-1]()  # running it again changes nothing


def test_a_question_needs_options_or_a_form() -> None:
    import pytest

    from foood.store import Store

    with pytest.raises(ValueError):
        ask(Store(), "o", "k", "prompt?")


async def test_overview_and_page(client) -> None:
    sara = await login(client, "demo.customer@example.com")
    admin = await login(client, "admin@example.com")
    order = await _order(client, sara)
    payment = (
        await client.post("/api/payments", json={"orderId": order["id"]}, headers=sara)
    ).json()
    await client.post(f"/api/payments/{payment['id']}/process", json={}, headers=sara)
    ask(
        client.store,
        order["id"],
        "check",
        "All good?",
        options=[{"value": "y", "label": "Yes"}],
        default="y",
    )
    overview = await client.get("/api/support/overview", headers=admin)
    assert overview.status_code == 200
    body = overview.json()
    assert body["engine"] == "hand-wired" and body["scenario"] is None
    assert [q["kind"] for q in body["openQuestions"]] == ["check"]
    assert (
        body["orders"][0]["id"] == order["id"]
        and body["orders"][0]["restaurantName"] == "QA Kitchen"
    )
    assert [d["decision"] for d in body["orders"][0]["decisions"]] == ["confirm"]
    assert (await client.get("/api/support/overview", headers=sara)).status_code == 403
    page = await client.get("/support")
    assert page.status_code == 200 and "foood support" in page.text
    assert "/api/support/overview" in page.text
