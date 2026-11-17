"""The model at the forks (issue #17, step two), corralled: allow-list, budget, fallback."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

from foood.app import create_app
from foood.bench import run
from foood.graph_engine import LangGraphEngine
from foood.recovery import (
    ALLOWED,
    TOOLS,
    OpenAIAdvisor,
    Recovery,
    ScriptedAdvisor,
    advisor_from_env,
)
from foood.scenario import Injection, Scenario
from foood.store import CLOCK
from tests.support import login

QA_OWNER = "qa.restaurant@delivery-plus.test"


@asynccontextmanager
async def _app(advisor, scenario=None, max_recoveries: int = 3) -> AsyncIterator[httpx.AsyncClient]:
    engine = LangGraphEngine(advisor=advisor, max_recoveries=max_recoveries)
    app = create_app(simulate=False, engine=engine, scenario=scenario)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        c.store = app.state.store  # type: ignore[attr-defined]
        c.engine = engine  # type: ignore[attr-defined]
        yield c


async def _paid(client, headers, item: str = "QA Pasta", **body) -> dict:
    restaurants = (await client.get("/api/restaurants?search=QA Kitchen")).json()["items"]
    menu = (await client.get(f"/api/menus/restaurants/{restaurants[0]['id']}/menu")).json()
    chosen = next(i for i in menu["items"] if i["name"] == item)
    await client.post(
        "/api/cart/items", json={"menuItemId": chosen["id"], "quantity": 1}, headers=headers
    )
    order = (await client.post("/api/orders", json=body, headers=headers)).json()
    payment = (
        await client.post("/api/payments", json={"orderId": order["id"]}, headers=headers)
    ).json()
    await client.post(f"/api/payments/{payment['id']}/process", json={}, headers=headers)
    return order


async def _decisions(client, order_id: str) -> list[str]:
    body = (await client.get(f"/api/_engine?orderId={order_id}")).json()
    return [d["decision"] for d in body["decisions"][order_id]]


def test_allow_lists_only_name_real_tools() -> None:
    assert all(set(tools) <= set(TOOLS) for tools in ALLOWED.values())
    assert (
        "ask_support" not in ALLOWED["card_declined"]
    )  # a decline is answered by the platform, fast


async def test_kitchen_rejection_becomes_a_replacement() -> None:
    advisor = ScriptedAdvisor(
        Recovery(tool="replace_order", reasoning="QA Bistro is open and cheaper")
    )
    async with _app(advisor) as client:
        sara = await login(client, "demo.customer@example.com")
        owner = await login(client, QA_OWNER)
        order = await _paid(client, sara)
        await client.patch(
            f"/api/orders/{order['id']}/status",
            json={"status": "CANCELLED", "reason": "No pasta"},
            headers=owner,
        )
        assert await _decisions(client, order["id"]) == [
            "confirm",
            "llm: replace_order",
            "replace order",
        ]
        original = (await client.get(f"/api/orders/{order['id']}", headers=sara)).json()
        assert original["status"] == "CANCELLED" and original["replacedByOrderId"]
        replacement = (
            await client.get(f"/api/orders/{original['replacedByOrderId']}", headers=sara)
        ).json()
        assert replacement["status"] == "CONFIRMED" and replacement["paymentStatus"] == "COMPLETED"
        assert client.engine.waiting_at(replacement["id"]) == "wait_kitchen"
        assert client.engine.waiting_at(order["id"]) is None
        context = advisor.contexts[0]
        assert (
            context.failure == "kitchen_rejected"
            and context.alternatives[0]["restaurantName"] == "QA Bistro"
        )
        recorded = (await client.get(f"/api/_engine?orderId={order['id']}")).json()["decisions"][
            order["id"]
        ][1]
        assert (
            recorded["llmCalls"] == 1
            and recorded["llmTokens"] == 100
            and recorded["model"] == "scripted"
        )


async def test_a_declined_card_is_retried_on_the_backup() -> None:
    advisor = ScriptedAdvisor(
        Recovery(tool="retry_backup_card", reasoning="a second card is on file")
    )
    scenario = Scenario("t", injections=(Injection("payment_declines", 1),))
    async with _app(advisor, scenario) as client:
        sara = await login(client, "demo.customer@example.com")
        order = await _paid(client, sara)
        final = (await client.get(f"/api/orders/{order['id']}", headers=sara)).json()
        assert final["status"] == "CONFIRMED" and final["paymentStatus"] == "COMPLETED"
        assert await _decisions(client, order["id"]) == ["llm: retry_backup_card", "confirm"]


async def test_no_driver_widens_the_search() -> None:
    advisor = ScriptedAdvisor(
        Recovery(tool="widen_dispatch", reasoning="a busy driver finishes nearby")
    )
    async with _app(advisor) as client:
        sara = await login(client, "demo.customer@example.com")
        owner = await login(client, QA_OWNER)
        order = await _paid(client, sara)
        for status in ("PREPARING", "READY_FOR_PICKUP"):
            await client.patch(
                f"/api/orders/{order['id']}/status", json={"status": status}, headers=owner
            )
        CLOCK.advance(order["promiseMinutes"] * 60 + 1)
        client.engine.tick()
        assert "dispatch" in client.store.hooks
        assert await _decisions(client, order["id"]) == [
            "confirm",
            "create delivery",
            "llm: widen_dispatch",
            "widen dispatch",
        ]
        assert client.engine.waiting_at(order["id"]) == "wait_driver"


async def test_a_drop_after_pickup_gets_a_credit_and_a_clean_cancellation() -> None:
    advisor = ScriptedAdvisor(Recovery(tool="issue_credit", amount=8, reasoning="sorry money"))
    async with _app(advisor) as client:
        sara = await login(client, "demo.customer@example.com")
        owner = await login(client, QA_OWNER)
        driver = await login(client, "driver@example.com")
        order = await _paid(client, sara)
        for status in ("PREPARING", "READY_FOR_PICKUP"):
            await client.patch(
                f"/api/orders/{order['id']}/status", json={"status": status}, headers=owner
            )
        await client.post("/api/drivers/me/online", json={}, headers=driver)
        job = (await client.get("/api/deliveries/me/current", headers=driver)).json()
        await client.post(f"/api/deliveries/{job['id']}/pickup", json={}, headers=driver)
        from foood.deliveries import drop

        drop(client.store, client.store.deliveries[job["id"]], "Flat tyre")
        final = (await client.get(f"/api/orders/{order['id']}", headers=sara)).json()
        assert final["status"] == "CANCELLED" and final["paymentStatus"] == "REFUNDED"
        assert (await client.get("/api/credits/me", headers=sara)).json()["balance"] == "8.00"
        last_three = (await _decisions(client, order["id"]))[-3:]
        assert last_three == ["llm: issue_credit", "cancel", "refund"]


async def test_support_picks_the_tool_when_asked() -> None:
    advisor = ScriptedAdvisor(
        Recovery(
            tool="ask_support",
            question="Kitchen is slow. Wait or move it?",
            options=["wait", "replace_order"],
            reasoning="a judgement call",
        )
    )
    async with _app(advisor) as client:
        sara = await login(client, "demo.customer@example.com")
        owner = await login(client, QA_OWNER)
        admin = await login(client, "admin@example.com")
        order = await _paid(client, sara)
        await client.patch(
            f"/api/orders/{order['id']}/status", json={"status": "PREPARING"}, headers=owner
        )
        CLOCK.advance(order["promiseMinutes"] * 60 + 1)
        client.engine.tick()
        assert client.engine.waiting_at(order["id"]) == "wait_support"
        questions = (await client.get("/api/support/questions", headers=admin)).json()["items"]
        assert len(questions) == 1 and [o["value"] for o in questions[0]["options"]] == [
            "wait",
            "replace_order",
        ]
        assert questions[0]["default"] == "wait" and questions[0]["prompt"].startswith(
            "Kitchen is slow"
        )
        answered = await client.post(
            f"/api/support/questions/{questions[0]['id']}/answer",
            json={"value": "replace_order"},
            headers=admin,
        )
        assert answered.status_code == 200
        original = (await client.get(f"/api/orders/{order['id']}", headers=sara)).json()
        assert original["status"] == "CANCELLED" and original["cancellationReason"] == "REPLACED"
        assert (
            original["replacedByOrderId"]
            and client.engine.waiting_at(original["replacedByOrderId"]) == "wait_kitchen"
        )
        assert await _decisions(client, order["id"]) == [
            "confirm",
            "llm: ask_support",
            "ask support",
            "support: replace_order",
            "replace order",
        ]
        assert len(advisor.contexts) == 1  # the person's answer costs no model call


async def test_an_unanswered_question_takes_the_default() -> None:
    advisor = ScriptedAdvisor(Recovery(tool="ask_support", reasoning="let a person decide"))
    async with _app(advisor) as client:
        sara = await login(client, "demo.customer@example.com")
        owner = await login(client, QA_OWNER)
        order = await _paid(client, sara)
        await client.patch(
            f"/api/orders/{order['id']}/status",
            json={"status": "CANCELLED", "reason": "Closed early"},
            headers=owner,
        )
        assert client.engine.waiting_at(order["id"]) == "wait_support"
        CLOCK.advance(121)
        for ticker in client.store.tickers:  # the deadline sweep, as the simulator loop runs it
            ticker()
        final = (await client.get(f"/api/orders/{order['id']}", headers=sara)).json()
        assert (
            final["paymentStatus"] == "REFUNDED" and client.engine.waiting_at(order["id"]) is None
        )
        assert (await _decisions(client, order["id"]))[-2:] == [
            "support: refund_and_cancel",
            "refund",
        ]


async def test_a_substitution_outside_the_policy_asks_first() -> None:
    advisor = ScriptedAdvisor(Recovery(tool="replace_order", reasoning="move it"))
    async with _app(advisor) as client:
        qa = await login(client, "qa.customer@delivery-plus.test")  # allows no substitutions
        owner = await login(client, QA_OWNER)
        admin = await login(client, "admin@example.com")
        order = await _paid(client, qa)
        await client.patch(
            f"/api/orders/{order['id']}/status",
            json={"status": "CANCELLED", "reason": "Out"},
            headers=owner,
        )
        question = (await client.get("/api/support/questions", headers=admin)).json()["items"][0]
        assert question["kind"] == "substitution" and "QA Bistro" in question["prompt"]
        await client.post(
            f"/api/support/questions/{question['id']}/answer",
            json={"value": "replace_order"},
            headers=admin,
        )
        original = (await client.get(f"/api/orders/{order['id']}", headers=qa)).json()
        assert original["replacedByOrderId"]


async def test_controls_disallowed_tool_model_error_and_budget() -> None:
    advisor = ScriptedAdvisor(
        Recovery(
            tool="issue_credit", reasoning="not allowed for a decline"
        ),  # not in the allow-list
        RuntimeError("timeout"),
    )
    scenario = Scenario(
        "t", injections=(Injection("payment_declines", 1), Injection("payment_declines", 2))
    )
    async with _app(advisor, scenario, max_recoveries=1) as client:
        sara = await login(client, "demo.customer@example.com")
        first = await _paid(client, sara)
        decisions = (await client.get(f"/api/_engine?orderId={first['id']}")).json()["decisions"][
            first["id"]
        ]
        assert [d["decision"] for d in decisions] == ["fallback", "cancel"]
        assert "not allowed" in decisions[0]["because"] and decisions[0]["llmCalls"] == 1
        second = await _paid(client, sara)
        decisions = (await client.get(f"/api/_engine?orderId={second['id']}")).json()["decisions"][
            second["id"]
        ]
        assert [d["decision"] for d in decisions] == ["fallback", "cancel"]
        assert decisions[0]["because"].startswith("model error")
        assert (await client.get(f"/api/orders/{second['id']}", headers=sara)).json()[
            "status"
        ] == "CANCELLED"
    # the budget: a second recovery on the same order falls back without asking the model
    advisor = ScriptedAdvisor(Recovery(tool="wait", reasoning="give it a minute"))
    async with _app(advisor, max_recoveries=1) as client:
        sara = await login(client, "demo.customer@example.com")
        owner = await login(client, QA_OWNER)
        driver = await login(client, "driver@example.com")
        order = await _paid(client, sara)
        for status in ("PREPARING", "READY_FOR_PICKUP"):
            await client.patch(
                f"/api/orders/{order['id']}/status", json={"status": status}, headers=owner
            )
        CLOCK.advance(order["promiseMinutes"] * 60 + 1)
        client.engine.tick()  # no driver: the model says wait (attempt 1)
        await client.post("/api/drivers/me/online", json={}, headers=driver)
        from foood.deliveries import drop

        job = (await client.get("/api/deliveries/me/current", headers=driver)).json()
        drop(client.store, client.store.deliveries[job["id"]])  # attempt 2 exceeds the budget
        decisions = await _decisions(client, order["id"])
        assert "fallback" in decisions and len(advisor.contexts) == 1


def test_without_a_key_or_switched_off_there_is_no_advisor(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert advisor_from_env() is None
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("FOOOD_LLM", "off")
    assert advisor_from_env() is None
    monkeypatch.delenv("FOOOD_LLM")
    assert isinstance(advisor_from_env(), OpenAIAdvisor)


def test_bench_without_a_model_still_matches_hand_wired(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("FOOOD_LLM", "off")
    wired = run("payment-trouble", "hand-wired", 7, tmp_path / "w")
    graphed = run("payment-trouble", "langgraph", 7, tmp_path / "g")
    assert graphed["summary"] == wired["summary"]


@pytest.mark.skipif(not os.environ.get("OPENAI_API_KEY"), reason="needs OPENAI_API_KEY")
async def test_live_model_picks_an_allowed_tool() -> None:
    """One real call, when a key is in the environment: the answer must be inside the allow-list."""
    advisor = OpenAIAdvisor()
    async with _app(advisor) as client:
        sara = await login(client, "demo.customer@example.com")
        owner = await login(client, QA_OWNER)
        order = await _paid(client, sara)
        await client.patch(
            f"/api/orders/{order['id']}/status",
            json={"status": "CANCELLED", "reason": "Oven broke"},
            headers=owner,
        )
        decisions = (await client.get(f"/api/_engine?orderId={order['id']}")).json()["decisions"][
            order["id"]
        ]
        llm = next(
            d for d in decisions if d["decision"].startswith("llm: ") or d["decision"] == "fallback"
        )
        assert llm["decision"] != "fallback", llm
        assert llm["decision"].removeprefix("llm: ") in ALLOWED["kitchen_rejected"]
        assert llm["llmTokens"] > 0 and llm["model"]


async def test_a_gateway_outage_waits_or_cancels_as_the_model_says() -> None:
    outage = Scenario("t", injections=(Injection("gateway_down", 1, for_seconds=60),))
    # wait: the engine charges the pending attempt itself once the gateway is back
    advisor = ScriptedAdvisor(Recovery(tool="wait", reasoning="the gateway will be back"))
    async with _app(advisor, scenario=outage) as client:
        sara = await login(client, "demo.customer@example.com")
        order = await _paid(client, sara)  # the process call is refused with a 503
        assert client.engine.waiting_at(order["id"]) == "wait_payment"
        assert await _decisions(client, order["id"]) == ["llm: wait", "keep waiting"]
        assert "nothing has been charged" in advisor.contexts[0].prompt()
        pending = (
            await client.post("/api/payments", json={"orderId": order["id"]}, headers=sara)
        ).json()
        for _ in range(3):  # the customer taps pay again while the gateway is down
            refused = await client.post(
                f"/api/payments/{pending['id']}/process", json={}, headers=sara
            )
            assert refused.status_code == 503
        assert await _decisions(client, order["id"]) == ["llm: wait", "keep waiting"]
        assert len(advisor.contexts) == 1  # one decision per outage, not one per tap
        CLOCK.advance(61)
        client.store.engine.tick()  # the gateway comes back
        assert await _decisions(client, order["id"]) == ["llm: wait", "keep waiting", "confirm"]
        assert client.engine.waiting_at(order["id"]) == "wait_kitchen"
    # refund and cancel: nothing was charged, so the order is simply cancelled
    advisor = ScriptedAdvisor(Recovery(tool="refund_and_cancel", reasoning="do not hold them"))
    async with _app(advisor, scenario=outage) as client:
        sara = await login(client, "demo.customer@example.com")
        order = await _paid(client, sara)
        got = (await client.get(f"/api/orders/{order['id']}", headers=sara)).json()
        assert got["status"] == "CANCELLED"
        assert got["cancellationReason"] == "PAYMENT_GATEWAY_UNAVAILABLE"
        assert await _decisions(client, order["id"]) == ["llm: refund_and_cancel", "cancel"]
    # no model: the hand-wired reaction, which is to cancel
    async with _app(None, scenario=outage) as client:
        sara = await login(client, "demo.customer@example.com")
        order = await _paid(client, sara)
        got = (await client.get(f"/api/orders/{order['id']}", headers=sara)).json()
        assert got["status"] == "CANCELLED" and await _decisions(client, order["id"]) == ["cancel"]
