"""Sensors read the world; costs move with it (issue #51): the planner sees rain, traffic and a
promise at risk as facts, prices its actions against them, and re-plans when the world changes."""

from __future__ import annotations

from foood.goap_engine import GoapEngine
from foood.store import CLOCK
from foood.world import close_road, set_traffic, set_weather
from tests.support import login
from tests.test_goap_engine import _app, _decisions, _paid

DOOR = {
    "deliveryAddress": "742 Evergreen Terrace",
    "deliveryLatitude": 37.787,
    "deliveryLongitude": -122.3985,
}


async def _door_order(client, headers) -> dict:
    restaurants = (await client.get("/api/restaurants?search=QA Kitchen")).json()["items"]
    menu = (await client.get(f"/api/menus/restaurants/{restaurants[0]['id']}/menu")).json()
    pasta = next(i for i in menu["items"] if i["name"] == "QA Pasta")
    await client.post(
        "/api/cart/items", json={"menuItemId": pasta["id"], "quantity": 1}, headers=headers
    )
    order = (await client.post("/api/orders", json=DOOR, headers=headers)).json()
    payment = (
        await client.post("/api/payments", json={"orderId": order["id"]}, headers=headers)
    ).json()
    await client.post(f"/api/payments/{payment['id']}/process", json={}, headers=headers)
    return order


def _spec(engine: GoapEngine, name: str):
    return next(a for a in engine.actions() if a.name == name)


def _cost(engine: GoapEngine, name: str, ws: dict) -> float:
    cost = _spec(engine, name).cost
    return float(cost(ws)) if callable(cost) else float(cost)


async def test_the_sensor_reads_the_world_as_facts() -> None:
    async with _app() as c:
        sara = await login(c, "demo.customer@example.com")
        order = await _door_order(c, sara)
        ws = {"order_id": order["id"], "accepted_delay": False, "accepted_wait": False}
        facts = c.engine.sense(ws)
        assert facts["zone"] == "civic" and facts["door_zone"] == "downtown"
        assert facts["rain"] is False and facts["route_delay_high"] is False
        assert facts["corridor_closed"] is False and facts["promise_at_risk"] is False
        assert facts["ride_minutes"] > 0 and isinstance(facts["drivers_thin"], bool)
        set_weather(c.store, "civic", "storm")
        facts = c.engine.sense(ws)
        assert facts["rain"] is True
        close_road(c.store, "market-street")
        set_traffic(c.store, "downtown", 1.5)
        facts = c.engine.sense(ws)
        assert facts["corridor_closed"] is True and facts["route_delay_high"] is True
        assert facts["promise_at_risk"] is True  # even leaving now, the courier would be late
        assert c.store.world.calls_by_order[order["id"]] > 0  # the planner consulted the world


async def test_costs_move_with_the_world() -> None:
    async with _app() as c:
        engine = c.engine
        clear = {
            "rain": False,
            "drivers_thin": False,
            "promise_at_risk": False,
            "route_delay_high": False,
        }
        rain = {**clear, "rain": True}
        at_risk = {**clear, "promise_at_risk": True}
        assert _cost(engine, "widen_dispatch", rain) < _cost(engine, "widen_dispatch", clear)
        assert _cost(engine, "keep_searching", at_risk) > _cost(engine, "keep_searching", clear)
        assert _cost(engine, "accept_kitchen_delay", at_risk) > _cost(
            engine, "accept_kitchen_delay", clear
        )
        assert _cost(engine, "issue_credit", rain) < _cost(engine, "issue_credit", clear)
        # the pool is thin: a wider search is the cheapest thing to try
        assert _cost(engine, "widen_dispatch", {**clear, "drivers_thin": True}) < _cost(
            engine, "keep_searching", clear
        )


async def test_a_replacement_behind_a_jam_costs_more() -> None:
    async with _app() as c:
        sara = await login(c, "demo.customer@example.com")
        order = await _door_order(c, sara)
        ws = {"order_id": order["id"]}
        before = _cost(c.engine, "replace_after_kitchen_failure", ws)
        set_traffic(c.store, "civic", 2.0)  # every QA kitchen is in civic
        assert _cost(c.engine, "replace_after_kitchen_failure", ws) > before


async def test_a_world_change_wakes_the_plan() -> None:
    async with _app() as c:
        CLOCK.freeze()
        sara = await login(c, "demo.customer@example.com")
        order = await _door_order(c, sara)
        assert c.engine.waiting_at(order["id"]) == "await_kitchen"
        before = len(await _decisions(c, order["id"]))
        close_road(c.store, "market-street")  # publishes world.changed
        set_traffic(c.store, "downtown", 1.5)
        names = [d["decision"] for d in (await _decisions(c, order["id"]))][before:]
        assert any(n in {"replan", "plan skips ahead"} for n in names), names
        last = next(
            d
            for d in reversed(await _decisions(c, order["id"]))
            if d["decision"] in {"replan", "plan skips ahead"}
        )
        assert "promise_at_risk" in str(last.get("because")) or "state_deviation" in str(
            last.get("because")
        )
        # the planner sent a courier ahead: a promise at risk is a fact the goal cares about
        assert "act: pre_dispatch" in [d["decision"] for d in await _decisions(c, order["id"])]
        assert c.engine.waiting_at(order["id"]) in {"await_driver", "await_pickup"}


async def test_without_weather_the_facts_are_quiet_and_waits_are_undisturbed() -> None:
    async with _app() as c:
        sara = await login(c, "demo.customer@example.com")
        order = await _paid(c, sara)
        count = len(await _decisions(c, order["id"]))
        set_weather(c.store, "sunset", "rain")  # far from the kitchens and the door
        assert len(await _decisions(c, order["id"])) == count
