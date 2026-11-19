"""Planning ahead, and the model as a sensor (issue #52): actions the planner can choose before
anything fails, and the one place a model belongs inside the planner."""

from __future__ import annotations

from foood.goap_engine import CUSTOMER_DEADLINE, GoapEngine
from foood.store import CLOCK
from foood.suggest import Reason, ScriptedClassifier, ScriptedSuggester, Suggestions
from foood.world import close_road, set_traffic
from tests.support import login
from tests.test_goap_engine import QA_OWNER, _app, _decisions
from tests.test_goap_world import _door_order


def _names(decisions: list[dict]) -> list[str]:
    return [d["decision"] for d in decisions]


async def test_the_planner_pre_dispatches_when_the_promise_is_at_risk() -> None:
    async with _app() as c:
        CLOCK.freeze()
        sara = await login(c, "demo.customer@example.com")
        driver = await login(c, "driver@example.com")
        await c.post("/api/drivers/me/online", json={}, headers=driver)
        close_road(c.store, "market-street")  # the way to the door is closed, the streets jammed
        set_traffic(c.store, "downtown", 1.5)
        order = await _door_order(c, sara)
        names = _names(await _decisions(c, order["id"]))
        assert "act: pre_dispatch" in names, names
        delivery = (await c.get(f"/api/deliveries/by-order/{order['id']}", headers=sara)).json()
        assert delivery["status"] in {"CREATED", "DRIVER_ASSIGNED"}
        shown = (await c.get(f"/api/orders/{order['id']}", headers=sara)).json()
        assert (
            shown["status"] == "CONFIRMED"
        )  # the kitchen has not even started; the courier is on the way
        pickup = await c.post(f"/api/deliveries/{delivery['id']}/pickup", json={}, headers=driver)
        assert pickup.status_code == 409  # nothing to collect before the kitchen is ready


async def test_a_closed_corridor_on_the_ride_is_rerouted() -> None:
    async with _app() as c:
        CLOCK.freeze()
        sara = await login(c, "demo.customer@example.com")
        owner = await login(c, QA_OWNER)
        driver = await login(c, "driver@example.com")
        await c.post("/api/drivers/me/online", json={}, headers=driver)
        order = await _door_order(c, sara)
        await c.patch(
            f"/api/orders/{order['id']}/status", json={"status": "PREPARING"}, headers=owner
        )
        await c.patch(
            f"/api/orders/{order['id']}/status", json={"status": "READY_FOR_PICKUP"}, headers=owner
        )
        delivery = (await c.get(f"/api/deliveries/by-order/{order['id']}", headers=sara)).json()
        assert delivery["status"] == "DRIVER_ASSIGNED"
        set_traffic(c.store, "downtown", 1.5)
        close_road(c.store, "market-street")  # closed with the courier en route, streets jammed
        names = _names(await _decisions(c, order["id"]))
        assert "act: reroute" in names, names
        assert delivery["id"] in c.store.world.rerouted
        assert c.store.world.log[-1]["event"] == "reroute"


async def test_a_faster_kitchen_takes_over_when_the_promise_cannot_be_kept() -> None:
    async with _app() as c:
        CLOCK.freeze()
        sara = await login(c, "demo.customer@example.com")
        # QA Bistro next door is unaffected only if the jam is on this kitchen's own block; the
        # zones are coarse, so give the bistro a shorter prep instead and jam nothing
        bistro = next(r for r in c.store.restaurants.values() if r.name == "QA Bistro")
        kitchen = next(r for r in c.store.restaurants.values() if r.name == "QA Kitchen")
        kitchen.prep_minutes = 40  # a slow kitchen tonight, promised on its usual pace
        bistro.prep_minutes = 5
        order = await _door_order(c, sara)
        c.store.orders[order["id"]].promise_minutes = 12
        from datetime import timedelta

        from foood.store import now

        c.store.orders[order["id"]].promised_at = now() + timedelta(minutes=12)
        set_traffic(c.store, "downtown", 2.2)  # the ride alone now breaches a 12-minute promise
        names = _names(await _decisions(c, order["id"]))
        assert "act: switch_kitchen_early" in names, names
        shown = (await c.get(f"/api/orders/{order['id']}", headers=sara)).json()
        assert shown["replacedByOrderId"]
        replacement = c.store.orders[shown["replacedByOrderId"]]
        assert c.store.restaurants[replacement.restaurant_id].name == "QA Bistro"


async def test_the_model_reads_the_note_and_substitutes_are_the_planners_route() -> None:
    classifier = ScriptedClassifier(Reason(kind="item_out", item="QA Soup"))
    suggester = ScriptedSuggester(
        Suggestions(candidates=["QA Fries", "Fries"], note="a side for a side")
    )
    async with _app(GoapEngine(classifier=classifier, suggester=suggester)) as c:
        CLOCK.freeze()
        sara = await login(c, "demo.customer@example.com")
        owner = await login(c, QA_OWNER)
        order = await _door_order(c, sara)
        # the soup is this kitchen's alone: no other kitchen makes it, so a plain replacement is
        # impossible and the planner's only route to the door is a substitute the customer accepts
        store = c.store
        soup = next(
            i
            for i in store.menu_items.values()
            if i.name == "QA Soup" and i.restaurant_id == store.orders[order["id"]].restaurant_id
        )
        for line in store.orders[order["id"]].items:
            line.name, line.menu_item_id = "QA Soup", soup.id
        for item in store.menu_items.values():
            if item.name == "QA Soup" and item.restaurant_id != soup.restaurant_id:
                item.available = False
        soup.available = False
        cancelled = await c.patch(
            f"/api/orders/{order['id']}/status",
            json={"status": "CANCELLED", "reason": "The last soup just went out"},
            headers=owner,
        )
        assert cancelled.status_code == 200, cancelled.text
        decisions = await _decisions(c, order["id"])
        names = _names(decisions)
        assert "sense: reason item_out" in names and "sense: substitutes" in names
        perception = [d for d in decisions if d.get("llmRole") == "perception"]
        assert {d["node"] for d in perception} == {"classify", "suggest"}
        assert all(d.get("llmCalls") == 1 and d.get("model") == "scripted" for d in perception)
        assert not any(d.get("llmRole") == "decision" for d in decisions)
        offered = (await c.get(f"/api/orders/{order['id']}/substitution", headers=sara)).json()
        assert offered["status"] == "OPEN" and any(
            i["name"] == "QA Fries" for o in offered["options"] for i in o["items"]
        )
        assert c.engine.waiting_at(order["id"]) == "await_customer"
        accepted = await c.post(
            f"/api/orders/{order['id']}/substitution", json={"accept": 0}, headers=sara
        )
        assert accepted.status_code == 200
        names = _names(await _decisions(c, order["id"]))
        assert "act: offer_substitutes" in names and "act: replace_with_substitute" in names
        shown = (await c.get(f"/api/orders/{order['id']}", headers=sara)).json()
        assert shown["replacedByOrderId"]


async def test_a_declined_substitute_leaves_only_the_refund() -> None:
    engine = GoapEngine(
        classifier=ScriptedClassifier(Reason(kind="item_out", item="QA Soup")),
        suggester=ScriptedSuggester(Suggestions(candidates=["QA Fries"])),
    )
    async with _app(engine) as c:
        CLOCK.freeze()
        sara = await login(c, "demo.customer@example.com")
        owner = await login(c, QA_OWNER)
        order = await _door_order(c, sara)
        store = c.store
        soup = next(
            i
            for i in store.menu_items.values()
            if i.name == "QA Soup" and i.restaurant_id == store.orders[order["id"]].restaurant_id
        )
        for line in store.orders[order["id"]].items:
            line.name, line.menu_item_id = "QA Soup", soup.id
        for item in store.menu_items.values():
            if item.name == "QA Soup":
                item.available = False
        await c.patch(
            f"/api/orders/{order['id']}/status",
            json={"status": "CANCELLED", "reason": "The last soup just went out"},
            headers=owner,
        )
        assert c.engine.waiting_at(order["id"]) == "await_customer"
        CLOCK.advance(CUSTOMER_DEADLINE + 1)
        c.engine.tick()  # silence is a decline
        shown = (await c.get(f"/api/orders/{order['id']}", headers=sara)).json()
        assert shown["status"] == "CANCELLED" and shown["paymentStatus"] == "REFUNDED"
        names = _names(await _decisions(c, order["id"]))
        assert "refund" in names and "act: replace_with_substitute" not in names


async def test_without_the_models_the_planner_is_as_before() -> None:
    async with _app() as c:
        sara = await login(c, "demo.customer@example.com")
        owner = await login(c, QA_OWNER)
        order = await _door_order(c, sara)
        await c.patch(
            f"/api/orders/{order['id']}/status",
            json={"status": "CANCELLED", "reason": "The last portion just went out"},
            headers=owner,
        )
        names = _names(await _decisions(c, order["id"]))
        assert "act: replace_after_kitchen_failure" in names
        assert not any(n.startswith("sense:") for n in names)
