"""One frontier model runs the evening (issue #47): the agent loop, its tools, its guards."""

from __future__ import annotations

import pytest

from foood.agent_engine import AgentEngine, mcp_payload
from foood.app import create_app
from foood.llm import ScriptedModel
from foood.store import CLOCK
from tests.support import login
from tests.test_scenario import QA_OWNER

PASTA_DOOR = {
    "deliveryAddress": "742 Evergreen Terrace",
    "deliveryLatitude": 37.787,
    "deliveryLongitude": -122.3985,
}


@pytest.fixture
def agent_client():
    """A client around the llm engine with a scripted model: ``async with agent_client(m) as c``."""
    from collections.abc import AsyncIterator
    from contextlib import asynccontextmanager

    import httpx

    @asynccontextmanager
    async def build(model: ScriptedModel | None) -> AsyncIterator[httpx.AsyncClient]:
        engine = AgentEngine(model=model)
        app = create_app(simulate=False, engine=engine)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as c:
            c.store = app.state.store  # type: ignore[attr-defined]
            c.engine = engine  # type: ignore[attr-defined]
            yield c

    return build


async def _pay(client, item: str = "QA Pasta", door: dict | None = None) -> tuple[dict, dict]:
    sara = await login(client, "demo.customer@example.com")
    restaurants = (await client.get("/api/restaurants?search=QA Kitchen")).json()["items"]
    menu = (await client.get(f"/api/menus/restaurants/{restaurants[0]['id']}/menu")).json()
    chosen = next(i for i in menu["items"] if i["name"] == item)
    await client.post(
        "/api/cart/items", json={"menuItemId": chosen["id"], "quantity": 1}, headers=sara
    )
    order = (await client.post("/api/orders", json=door or PASTA_DOOR, headers=sara)).json()
    payment = (
        await client.post("/api/payments", json={"orderId": order["id"]}, headers=sara)
    ).json()
    await client.post(f"/api/payments/{payment['id']}/process", json={}, headers=sara)
    return sara, order


def _decisions(client, order_id: str) -> list[dict]:
    return client.store.decisions.get(order_id, [])


async def test_without_a_model_it_is_the_hand_wired_engine(agent_client) -> None:
    async with agent_client(None) as c:
        assert c.engine.model is None and c.engine.name == "llm"
        sara, order = await _pay(c)
        assert (await c.get(f"/api/orders/{order['id']}", headers=sara)).json()[
            "status"
        ] == "CONFIRMED"
        names = [d["decision"] for d in _decisions(c, order["id"])]
        assert names == ["confirm"] and not any(
            d.get("llmCalls") for d in _decisions(c, order["id"])
        )


async def test_the_model_hears_the_payment_and_confirms_with_a_tool(agent_client) -> None:
    model = ScriptedModel(("I will confirm it.", [("confirm_payment", {})]), ("Confirmed.", []))
    async with agent_client(model) as c:
        sara, order = await _pay(c)
        shown = (await c.get(f"/api/orders/{order['id']}", headers=sara)).json()
        assert shown["status"] == "CONFIRMED"
        names = [d["decision"] for d in _decisions(c, order["id"])]
        assert names == ["llm: confirm_payment", "confirm", "llm: done"]
        first = _decisions(c, order["id"])[0]
        assert first["llmRole"] == "decision" and first["model"] == "scripted"
        assert first["llmCalls"] == 1 and first["llmTokens"] == 100 and first["latencyMs"] == 50.0
        assert "What happened: The payment of $15.50" in model.seen[0]["input"][0]["content"]
        assert "order_facts" in model.seen[0]["tools"] and "get_weather" in model.seen[0]["tools"]
        assert "set_weather" not in model.seen[0]["tools"]  # never the world's controls
        assert model.seen[1]["input"][0]["type"] == "function_call_output"
        assert '"ok": true' in model.seen[1]["input"][0]["output"]


async def test_a_rejected_order_is_replaced_after_a_lookup(agent_client) -> None:
    model = ScriptedModel(
        ("Paying.", [("confirm_payment", {})]),
        ("", []),
        ("Who else makes it?", [("alternatives", {}), ("get_weather", {"zone": "civic"})]),
        (
            "QA Bistro is closest.",
            [("replace_order", {"restaurant_id": None, "reason": "the grill is down"})],
        ),
        ("Replaced.", []),
    )
    async with agent_client(model) as c:
        sara, order = await _pay(c)
        owner = await login(c, QA_OWNER)
        rejected = await c.patch(
            f"/api/orders/{order['id']}/status",
            json={"status": "CANCELLED", "note": "The grill is down"},
            headers=owner,
        )
        assert rejected.status_code == 200, rejected.text
        decisions = _decisions(c, order["id"])
        names = [d["decision"] for d in decisions]
        assert "llm: alternatives, get_weather" in names and "replace order" in names
        original = (await c.get(f"/api/orders/{order['id']}", headers=sara)).json()
        assert original["replacedByOrderId"]
        replacement = (
            await c.get(f"/api/orders/{original['replacedByOrderId']}", headers=sara)
        ).json()
        assert c.store.restaurants[replacement["restaurantId"]].name in {
            "QA Bistro",
            "QA Night Cafe",
        }
        assert c.store.world.calls_by_order[order["id"]] == 1  # the weather lookup, counted
        weather_output = model.seen[3]["input"][1]["output"]
        assert '"kind": "clear"' in weather_output
        assert sum(d.get("llmCalls", 0) for d in decisions if d.get("llmRole") == "decision") == 5


async def test_the_model_errs_and_the_hand_wired_reaction_applies(agent_client) -> None:
    model = ScriptedModel(TimeoutError("the model took too long"))
    async with agent_client(model) as c:
        sara, order = await _pay(c)
        shown = (await c.get(f"/api/orders/{order['id']}", headers=sara)).json()
        assert shown["status"] == "CONFIRMED"  # the fallback confirmed it
        names = [d["decision"] for d in _decisions(c, order["id"])]
        assert names == ["fallback", "confirm"]
        assert _decisions(c, order["id"])[0]["because"].startswith("model error: TimeoutError")
        assert c.engine.episodes[-1].failed


async def test_a_model_that_only_looks_spends_its_budget_and_falls_back(agent_client) -> None:
    model = ScriptedModel(*[("Hmm.", [("order_facts", {})])] * 8)
    async with agent_client(model) as c:
        sara, order = await _pay(c)
        shown = (await c.get(f"/api/orders/{order['id']}", headers=sara)).json()
        assert shown["status"] == "CONFIRMED"
        decisions = _decisions(c, order["id"])
        assert (
            sum(1 for d in decisions if d["decision"].startswith("llm:")) == 6
        )  # the round budget
        assert [d["decision"] for d in decisions][-2:] == ["fallback", "confirm"]
        assert c.engine.episodes[-1].rounds == 6


async def test_substitutes_go_to_the_customer_and_come_back(agent_client) -> None:
    model = ScriptedModel(
        ("Paying.", [("confirm_payment", {})]),
        ("", []),
        ("What can they make?", [("candidate_pool", {})]),
        (
            "Offer a burger.",
            [
                (
                    "offer_substitutes",
                    {
                        "candidates": ["QA Burger", "Spaghetti Carbonara"],
                        "reason": "QA Pasta ran out",
                    },
                )
            ],
        ),
        ("Offered.", []),
        ("Apply it.", [("apply_substitution", {})]),
        ("Done.", []),
    )
    async with agent_client(model) as c:
        sara, order = await _pay(c)
        owner = await login(c, QA_OWNER)
        # the kitchen starts, the item runs out, the kitchen cancels mid-prep
        await c.patch(
            f"/api/orders/{order['id']}/status", json={"status": "PREPARING"}, headers=owner
        )
        pasta = next(
            i
            for i in c.store.menu_items.values()
            if i.name == "QA Pasta" and i.restaurant_id == c.store.orders[order["id"]].restaurant_id
        )
        pasta.available = False
        await c.patch(
            f"/api/orders/{order['id']}/status",
            json={"status": "CANCELLED", "note": "The last portion just went out"},
            headers=owner,
        )
        offered = (await c.get(f"/api/orders/{order['id']}/substitution", headers=sara)).json()
        assert offered["status"] == "OPEN"
        kitchens = [o["restaurantName"] for o in offered["options"]]
        assert "QA Kitchen" in kitchens
        assert "Sold out at this kitchen now: QA Pasta" in model.seen[2]["input"][0]["content"]
        choice = kitchens.index("QA Kitchen")
        accepted = await c.post(
            f"/api/orders/{order['id']}/substitution", json={"accept": choice}, headers=sara
        )
        assert accepted.status_code == 200
        original = (await c.get(f"/api/orders/{order['id']}", headers=sara)).json()
        assert original["replacedByOrderId"]
        replacement = (
            await c.get(f"/api/orders/{original['replacedByOrderId']}", headers=sara)
        ).json()
        assert [i["name"] for i in replacement["items"]] == ["QA Burger"]
        assert "The customer accepted a substitute" in model.seen[5]["input"][0]["content"]


async def test_timers_raise_the_forks_a_graph_would_draw(agent_client) -> None:
    model = ScriptedModel(
        ("Paying.", [("confirm_payment", {})]),
        ("", []),
        ("Send it.", [("create_delivery", {})]),
        ("", []),
        ("Nobody is coming; widen.", [("widen_dispatch", {})]),
        ("", []),
    )
    async with agent_client(model) as c:
        CLOCK.freeze()
        sara, order = await _pay(c)
        owner = await login(c, QA_OWNER)
        await c.patch(
            f"/api/orders/{order['id']}/status", json={"status": "PREPARING"}, headers=owner
        )
        await c.patch(
            f"/api/orders/{order['id']}/status", json={"status": "READY_FOR_PICKUP"}, headers=owner
        )
        assert [d["decision"] for d in _decisions(c, order["id"])][-3:] == [
            "llm: create_delivery",
            "create delivery",
            "llm: done",
        ]
        CLOCK.advance(100)
        c.engine.tick()
        decisions = _decisions(c, order["id"])
        assert "widen dispatch" in [d["decision"] for d in decisions]
        assert (
            "No driver has taken the delivery for 100 seconds"
            in model.seen[4]["input"][0]["content"]
        )
        c.engine.tick()  # raised once only
        assert sum(1 for d in _decisions(c, order["id"]) if d["decision"] == "widen dispatch") == 1


async def test_a_world_change_reaches_every_order_in_flight(agent_client) -> None:
    from foood.world import set_weather

    model = ScriptedModel(
        ("Paying.", [("confirm_payment", {})]),
        ("", []),
        ("Rain; check the ride.", [("ride_eta", {})]),
        ("Still fine.", [("wait", {"reason": "the ride still fits the promise"})]),
        ("", []),
    )
    async with agent_client(model) as c:
        sara, order = await _pay(c)
        set_weather(c.store, "civic", "rain")
        names = [d["decision"] for d in _decisions(c, order["id"])]
        assert "llm: ride_eta" in names and "keep waiting" in names
        assert (
            '"weather": {"zone": "civic", "kind": "rain"}' in model.seen[2]["input"][0]["content"]
        )
        assert "rideMinutesNow" in model.seen[3]["input"][0]["output"]
        assert c.store.world.calls_by_order[order["id"]] == 1


async def test_intake_places_an_order_through_the_cart_tools(agent_client) -> None:
    def first_turn(input_items):
        return ("Looking.", [("search_menu", {"query": "QA Burger"})])

    model = ScriptedModel(
        first_turn,
        lambda items: (
            "Adding.",
            [
                (
                    "add_to_cart",
                    {
                        "menu_item_id": __import__("json").loads(items[0]["output"])[0][
                            "menuItemId"
                        ],
                        "quantity": 2,
                    },
                )
            ],
        ),
        (
            "Placing.",
            [
                (
                    "place_order",
                    {"delivery_address": "742 Evergreen Terrace", "delivery_notes": None},
                )
            ],
        ),
    )
    async with agent_client(model) as c:
        sara = await login(c, "demo.customer@example.com")
        placed = await c.post(
            "/api/orders/intake",
            json={"text": "two QA burgers to 742 Evergreen Terrace"},
            headers=sara,
        )
        assert placed.status_code == 200, placed.text
        body = placed.json()
        assert body["status"] == "placed" and body["usage"]["calls"] == 3
        assert [i["quantity"] for i in body["order"]["items"]] == [2]
        decisions = _decisions(c, body["order"]["id"])
        intake = next(d for d in decisions if d["decision"] == "intake")
        assert (
            intake["llmRole"] == "language"
            and intake["llmCalls"] == 3
            and intake["model"] == "scripted"
        )


async def test_intake_asks_back_instead_of_guessing(agent_client) -> None:
    model = ScriptedModel(
        ("Which?", [("ask_customer", {"question": "Pizza Napoli or Luigi's for the Margherita?"})])
    )
    async with agent_client(model) as c:
        sara = await login(c, "demo.customer@example.com")
        reply = (
            await c.post("/api/orders/intake", json={"text": "a margherita"}, headers=sara)
        ).json()
        assert reply["status"] == "question" and "Margherita" in reply["question"]


async def test_intake_needs_the_llm_engine() -> None:
    import httpx

    app = create_app(simulate=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        sara = await login(c, "demo.customer@example.com")
        assert (
            await c.post("/api/orders/intake", json={"text": "anything at all"}, headers=sara)
        ).status_code == 503


def test_mcp_payloads_become_data() -> None:
    class Text:
        def __init__(self, text: str) -> None:
            self.text = text

    class Result:
        def __init__(self, content, structured=None) -> None:
            self.content = content
            self.structuredContent = structured

    assert mcp_payload(Result([Text('{"kind": "rain"}')])) == {"kind": "rain"}
    assert mcp_payload(Result([Text("plain")])) == "plain"
    assert mcp_payload(Result([], {"zone": "civic"})) == {"zone": "civic"}
    assert mcp_payload(([Text("x")], {"a": 1})) == {"a": 1}
    assert mcp_payload({"already": "data"}) == {"already": "data"}


def test_the_engine_is_registered() -> None:
    from foood.engine import make_engine

    engine = make_engine("llm")
    assert isinstance(engine, AgentEngine)
    with pytest.raises(ValueError):
        make_engine("nope")
