"""Look up, then decide (issue #49): the world reaches the fork through its own node, the
decision node never sees a tool, and the allow-list reads the conditions."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx

from foood.app import create_app
from foood.graph_engine import LangGraphEngine, mermaid
from foood.llm import ScriptedModel
from foood.recovery import ALLOWED, Recovery, ScriptedAdvisor, allowed_for
from foood.world import close_road, set_weather
from tests.support import login
from tests.test_graph_engine import QA_OWNER

DOOR = {
    "deliveryAddress": "742 Evergreen Terrace",
    "deliveryLatitude": 37.787,
    "deliveryLongitude": -122.3985,
}


@asynccontextmanager
async def _app(engine: LangGraphEngine) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(simulate=False, engine=engine)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        c.store = app.state.store  # type: ignore[attr-defined]
        yield c


async def _order_at_the_door(client) -> tuple[dict, dict]:
    sara = await login(client, "demo.customer@example.com")
    restaurants = (await client.get("/api/restaurants?search=QA Kitchen")).json()["items"]
    menu = (await client.get(f"/api/menus/restaurants/{restaurants[0]['id']}/menu")).json()
    pasta = next(i for i in menu["items"] if i["name"] == "QA Pasta")
    await client.post(
        "/api/cart/items", json={"menuItemId": pasta["id"], "quantity": 1}, headers=sara
    )
    order = (await client.post("/api/orders", json=DOOR, headers=sara)).json()
    payment = (
        await client.post("/api/payments", json={"orderId": order["id"]}, headers=sara)
    ).json()
    await client.post(f"/api/payments/{payment['id']}/process", json={}, headers=sara)
    return sara, order


async def _reject(client, order: dict, reason: str = "The grill is down") -> None:
    owner = await login(client, QA_OWNER)
    done = await client.patch(
        f"/api/orders/{order['id']}/status",
        json={"status": "CANCELLED", "reason": reason},
        headers=owner,
    )
    assert done.status_code == 200, done.text


async def test_the_fork_sees_the_world_without_a_model_lookup() -> None:
    advisor = ScriptedAdvisor(Recovery(tool="replace_order", reasoning="QA Bistro can"))
    async with _app(LangGraphEngine(advisor=advisor)) as c:
        set_weather(c.store, "civic", "storm")
        sara, order = await _order_at_the_door(c)
        await _reject(c, order)
        context = advisor.contexts[0]
        assert context.world["weather"] == "storm" and context.world["zone"] == "civic"
        assert context.world["rideMinutes"] > context.world["clearRideMinutes"]
        assert context.world["closures"] == [] and context.world["forecast"] == []
        assert "World:" in context.prompt() and "storm" in context.prompt()
        assert context.world["lookups"] == []
        decisions = c.store.decisions[order["id"]]
        assert not any(d.get("llmRole") == "perception" for d in decisions)
        assert c.store.world.calls_by_order[order["id"]] >= 3  # weather, eta, forecast, in code


async def test_a_small_model_may_look_further_through_the_mcp_tools() -> None:
    looker = ScriptedModel(
        (
            "Check the traffic downtown.",
            [
                (
                    "get_traffic",
                    {
                        "from_latitude": 37.7731,
                        "from_longitude": -122.4167,
                        "to_latitude": 37.787,
                        "to_longitude": -122.3985,
                    },
                )
            ],
        ),
        ("Nothing more.", []),
    )
    advisor = ScriptedAdvisor(Recovery(tool="replace_order", reasoning="fine"))
    async with _app(LangGraphEngine(advisor=advisor, looker=looker)) as c:
        sara, order = await _order_at_the_door(c)
        await _reject(c, order)
        context = advisor.contexts[0]
        assert context.world["lookups"][0]["tool"] == "get_traffic"
        assert context.world["lookups"][0]["result"]["delayFactor"] == 1.0
        decisions = c.store.decisions[order["id"]]
        perception = [d for d in decisions if d.get("llmRole") == "perception"]
        assert len(perception) == 2 and all(d["node"] == "lookup" for d in perception)
        assert (
            perception[0]["decision"] == "lookup: get_traffic"
            and perception[1]["decision"] == "lookup: done"
        )
        assert looker.seen[0]["tools"] == [
            "zones",
            "get_weather",
            "get_traffic",
            "eta",
            "forecast",
        ] or set(looker.seen[0]["tools"]) == {
            "zones",
            "get_weather",
            "get_traffic",
            "eta",
            "forecast",
        }
        assert "kitchen_rejected" in looker.seen[0]["input"][0]["content"]


async def test_a_lookup_model_that_errs_costs_nothing_but_a_note() -> None:
    advisor = ScriptedAdvisor(Recovery(tool="replace_order", reasoning="fine"))
    async with _app(
        LangGraphEngine(advisor=advisor, looker=ScriptedModel(TimeoutError("slow")))
    ) as c:
        sara, order = await _order_at_the_door(c)
        await _reject(c, order)
        assert advisor.contexts[0].world["weather"] == "clear"  # the fixed lookups still ran
        assert advisor.contexts[0].world["lookups"] == []
        original = (await c.get(f"/api/orders/{order['id']}", headers=sara)).json()
        assert original["replacedByOrderId"]


async def test_a_kitchen_behind_a_closed_corridor_is_not_offered() -> None:
    advisor = ScriptedAdvisor(Recovery(tool="replace_order", reasoning="try"))
    async with _app(LangGraphEngine(advisor=advisor)) as c:
        close_road(c.store, "market-street")  # every QA kitchen is in civic, the door downtown
        sara, order = await _order_at_the_door(c)
        await _reject(c, order)
        context = advisor.contexts[0]
        assert context.alternatives == [] and "replace_order" not in context.allowed
        assert context.world["closures"] == ["market-street"]
        names = [d["decision"] for d in c.store.decisions[order["id"]]]
        assert "fallback" in names  # replace_order was not allowed; the baseline applied


def test_the_allow_list_reads_the_conditions() -> None:
    assert (
        allowed_for("kitchen_rejected", {"alternativesReachable": 2}) == ALLOWED["kitchen_rejected"]
    )
    without = allowed_for("kitchen_rejected", {"alternativesReachable": 0})
    assert "replace_order" not in without and "refund_and_cancel" in without
    stormy = allowed_for("no_driver", {"weather": "storm", "alternativesReachable": 1})
    assert stormy.index("widen_dispatch") == 0  # the first thing to try when the pool is thin
    assert allowed_for("card_declined", {"weather": "storm"}) == ALLOWED["card_declined"]


def test_the_drawing_has_the_lookup_node() -> None:
    drawn = mermaid()
    assert "lookup" in drawn and "failure_no_driver --> lookup" in drawn.replace("\t", " ")
