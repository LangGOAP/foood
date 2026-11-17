"""The plan panel (issue #28): the current route, where it stands, and why it changed."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx

from foood.app import create_app
from foood.goap_engine import GoapEngine
from foood.graph_engine import LangGraphEngine
from foood.recovery import Recovery, ScriptedAdvisor
from foood.support import plan_panel
from tests.support import login

QA_OWNER = "qa.restaurant@delivery-plus.test"


@asynccontextmanager
async def _app(engine=None) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(simulate=False, engine=engine)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
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


def test_panel_is_built_from_decisions_only() -> None:
    assert plan_panel([], None) is None
    assert plan_panel([{"decision": "confirm"}], None) is None
    decisions = [
        {
            "at": "t0",
            "decision": "plan",
            "steps": ["await_payment", "await_kitchen", "dispatch"],
            "planCost": 3.0,
            "considered": [],
        },
        {
            "at": "t1",
            "decision": "replan",
            "because": "state_deviation",
            "steps": ["replace_after_kitchen_failure", "await_kitchen", "dispatch"],
            "planCost": 9.5,
            "considered": [{"kitchen": "QA Bistro", "difference": "-0.50", "needsApproval": False}],
        },
        {"at": "t2", "decision": "llm: wait", "because": "the kitchen is close"},
    ]
    panel = plan_panel(decisions, "await_kitchen")
    assert panel["steps"][0] == "replace_after_kitchen_failure" and panel["position"] == 1
    assert (
        panel["replans"] == 1 and panel["lastReplan"] == "state_deviation" and panel["cost"] == 9.5
    )
    assert (
        panel["considered"][0]["kitchen"] == "QA Bistro"
        and panel["lastReasoning"] == "the kitchen is close"
    )


async def test_the_planner_engine_shows_its_route_and_the_kitchens_it_weighed() -> None:
    async with _app(GoapEngine()) as client:
        sara = await login(client, "demo.customer@example.com")
        owner = await login(client, QA_OWNER)
        admin = await login(client, "admin@example.com")
        order = await _paid(client, sara)
        row = next(
            o
            for o in (await client.get("/api/support/overview", headers=admin)).json()["orders"]
            if o["id"] == order["id"]
        )
        assert row["waitingAt"] == "await_kitchen" and row["plan"]["position"] == 1
        assert row["plan"]["steps"] == [
            "await_payment",
            "await_kitchen",
            "dispatch",
            "await_driver",
            "await_pickup",
            "await_dropoff",
        ]
        await client.patch(
            f"/api/orders/{order['id']}/status",
            json={"status": "CANCELLED", "reason": "No pasta"},
            headers=owner,
        )
        rows = (await client.get("/api/support/overview", headers=admin)).json()["orders"]
        original = next(o for o in rows if o["id"] == order["id"])
        assert original["plan"]["steps"][0] == "replace_after_kitchen_failure"
        assert (
            original["plan"]["considered"][0]["kitchen"] == "QA Bistro"
            and original["plan"]["replans"] == 1
        )
        replacement = next(o for o in rows if o["replacesOrderId"] == order["id"])
        assert (
            replacement["waitingAt"] == "await_kitchen" and replacement["plan"]["position"] == 1
        )  # the same thread


async def test_the_graph_engine_shows_its_wait_and_the_models_reasoning() -> None:
    advisor = ScriptedAdvisor(
        Recovery(tool="replace_order", reasoning="QA Bistro is open and cheaper")
    )
    async with _app(LangGraphEngine(advisor=advisor)) as client:
        sara = await login(client, "demo.customer@example.com")
        owner = await login(client, QA_OWNER)
        admin = await login(client, "admin@example.com")
        order = await _paid(client, sara)
        row = next(
            o
            for o in (await client.get("/api/support/overview", headers=admin)).json()["orders"]
            if o["id"] == order["id"]
        )
        assert (
            row["waitingAt"] == "wait_kitchen" and row["plan"] is None
        )  # a graph has no plan, only a place
        await client.patch(
            f"/api/orders/{order['id']}/status",
            json={"status": "CANCELLED", "reason": "No pasta"},
            headers=owner,
        )
        row = next(
            o
            for o in (await client.get("/api/support/overview", headers=admin)).json()["orders"]
            if o["id"] == order["id"]
        )
        reasoning = [
            d["because"] for d in row["decisions"] if d["decision"] == "llm: replace_order"
        ]
        assert reasoning == ["QA Bistro is open and cheaper"] and row["waitingAt"] is None


async def test_hand_wired_has_no_plan(client) -> None:
    sara = await login(client, "demo.customer@example.com")
    admin = await login(client, "admin@example.com")
    order = await _paid(client, sara)
    row = next(
        o
        for o in (await client.get("/api/support/overview", headers=admin)).json()["orders"]
        if o["id"] == order["id"]
    )
    assert row["plan"] is None and row["waitingAt"] is None
    page = (await client.get("/support")).text
    assert "renderPlan" in page and "considered" in page
