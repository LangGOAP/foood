"""Suggest a substitute (issue #50): a small model classifies the kitchen's note, a mid model
proposes dishes from the pool, the platform grounds and prices them, the customer decides."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx

from foood.app import create_app
from foood.graph_engine import CUSTOMER_DEADLINE, LangGraphEngine, mermaid
from foood.recovery import Recovery, ScriptedAdvisor
from foood.store import CLOCK
from foood.suggest import Reason, ScriptedClassifier, ScriptedSuggester, Suggestions
from tests.support import login
from tests.test_graph_engine import QA_OWNER
from tests.test_lookup import _order_at_the_door


@asynccontextmanager
async def _app(engine: LangGraphEngine) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(simulate=False, engine=engine)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        c.store = app.state.store  # type: ignore[attr-defined]
        yield c


async def _runs_out_mid_prep(client, order: dict) -> None:
    """The kitchen starts, the pasta runs out, the kitchen cancels with the note."""
    owner = await login(client, QA_OWNER)
    started = await client.patch(
        f"/api/orders/{order['id']}/status", json={"status": "PREPARING"}, headers=owner
    )
    assert started.status_code == 200, started.text
    kitchen_id = client.store.orders[order["id"]].restaurant_id
    pasta = next(
        i
        for i in client.store.menu_items.values()
        if i.restaurant_id == kitchen_id and i.name == "QA Pasta"
    )
    pasta.available = False
    cancelled = await client.patch(
        f"/api/orders/{order['id']}/status",
        json={"status": "CANCELLED", "reason": "The last portion just went out"},
        headers=owner,
    )
    assert cancelled.status_code == 200, cancelled.text


def _names(client, order_id: str) -> list[str]:
    return [d["decision"] for d in client.store.decisions[order_id]]


async def test_an_item_that_ran_out_becomes_an_offer_the_customer_accepts() -> None:
    advisor = ScriptedAdvisor()
    classifier = ScriptedClassifier(Reason(kind="item_out", item="QA Pasta"))
    suggester = ScriptedSuggester(Suggestions(candidates=["QA Burger", "Pad Thai"], note="closest"))
    engine = LangGraphEngine(advisor=advisor, classifier=classifier, suggester=suggester)
    async with _app(engine) as c:
        sara, order = await _order_at_the_door(c)
        await _runs_out_mid_prep(c, order)
        assert classifier.seen[0][0] == "The last portion just went out"
        assert suggester.seen[0]["missing"] == ["QA Pasta"]
        assert any(p["item"] == "QA Burger" for p in suggester.seen[0]["pool"])
        offered = (await c.get(f"/api/orders/{order['id']}/substitution", headers=sara)).json()
        assert offered["status"] == "OPEN"
        kitchens = [o["restaurantName"] for o in offered["options"]]
        assert "QA Kitchen" in kitchens and all(
            i["name"] != "Pad Thai" for o in offered["options"] for i in o["items"]
        )
        names = _names(c, order["id"])
        assert "classify: item_out" in names and "suggest: QA Burger, Pad Thai" in names
        assert "offer substitutes" in names and advisor.contexts == []
        kinds = {
            d["decision"]: (d.get("llmRole"), d.get("node"))
            for d in c.store.decisions[order["id"]]
            if d.get("llmCalls")
        }
        assert kinds["classify: item_out"] == ("perception", "classify")
        assert kinds["suggest: QA Burger, Pad Thai"] == ("perception", "suggest")
        assert engine.waiting_at(order["id"]) == "wait_customer"
        chosen = kitchens.index("QA Kitchen")
        accepted = await c.post(
            f"/api/orders/{order['id']}/substitution", json={"accept": chosen}, headers=sara
        )
        assert accepted.status_code == 200, accepted.text
        original = (await c.get(f"/api/orders/{order['id']}", headers=sara)).json()
        assert original["replacedByOrderId"]
        replacement = (
            await c.get(f"/api/orders/{original['replacedByOrderId']}", headers=sara)
        ).json()
        assert [i["name"] for i in replacement["items"]] == ["QA Burger"]
        assert "replace order" in _names(c, order["id"]) and engine.waiting_at(order["id"]) is None
        assert engine.waiting_at(original["replacedByOrderId"]) == "wait_kitchen"


async def test_a_declined_offer_goes_back_to_the_fork_with_that_fact() -> None:
    advisor = ScriptedAdvisor(Recovery(tool="replace_order", reasoning="QA Bistro makes it"))
    engine = LangGraphEngine(
        advisor=advisor,
        classifier=ScriptedClassifier(Reason(kind="item_out", item="QA Pasta")),
        suggester=ScriptedSuggester(Suggestions(candidates=["QA Burger"])),
    )
    async with _app(engine) as c:
        sara, order = await _order_at_the_door(c)
        await _runs_out_mid_prep(c, order)
        declined = await c.post(
            f"/api/orders/{order['id']}/substitution", json={"decline": True}, headers=sara
        )
        assert declined.status_code == 200
        assert advisor.contexts and advisor.contexts[0].world["substitutesDeclined"] is True
        assert "declined" in advisor.contexts[0].prompt().lower()
        original = (await c.get(f"/api/orders/{order['id']}", headers=sara)).json()
        assert original["replacedByOrderId"]  # the model chose another kitchen


async def test_a_kitchen_that_is_down_skips_the_offer() -> None:
    advisor = ScriptedAdvisor(Recovery(tool="replace_order", reasoning="elsewhere"))
    suggester = ScriptedSuggester(Suggestions(candidates=["QA Burger"]))
    engine = LangGraphEngine(
        advisor=advisor,
        classifier=ScriptedClassifier(Reason(kind="kitchen_down")),
        suggester=suggester,
    )
    async with _app(engine) as c:
        sara, order = await _order_at_the_door(c)
        await _runs_out_mid_prep(c, order)
        assert (
            suggester.seen == [] and advisor.contexts[0].world["reason"]["kind"] == "kitchen_down"
        )
        assert "offer substitutes" not in _names(c, order["id"])


async def test_without_the_two_nodes_the_fork_is_as_before() -> None:
    advisor = ScriptedAdvisor(Recovery(tool="replace_order", reasoning="elsewhere"))
    async with _app(LangGraphEngine(advisor=advisor)) as c:
        sara, order = await _order_at_the_door(c)
        await _runs_out_mid_prep(c, order)
        names = _names(c, order["id"])
        assert "replace order" in names and not any(
            n.startswith(("classify", "suggest")) for n in names
        )
        assert "reason" not in advisor.contexts[0].world


async def test_a_silent_customer_counts_as_a_decline_after_the_deadline() -> None:
    advisor = ScriptedAdvisor(Recovery(tool="refund_and_cancel", reasoning="nothing else"))
    engine = LangGraphEngine(
        advisor=advisor,
        classifier=ScriptedClassifier(Reason(kind="item_out", item="QA Pasta")),
        suggester=ScriptedSuggester(Suggestions(candidates=["QA Burger"])),
    )
    async with _app(engine) as c:
        CLOCK.freeze()
        sara, order = await _order_at_the_door(c)
        await _runs_out_mid_prep(c, order)
        assert engine.waiting_at(order["id"]) == "wait_customer"
        CLOCK.advance(CUSTOMER_DEADLINE + 1)
        engine.tick()
        assert advisor.contexts and advisor.contexts[0].world["substitutesDeclined"] is True
        shown = (await c.get(f"/api/orders/{order['id']}/substitution", headers=sara)).json()
        assert shown["status"] == "DECLINED"
        assert (await c.get(f"/api/orders/{order['id']}", headers=sara)).json()[
            "paymentStatus"
        ] == "REFUNDED"


async def test_a_classifier_that_errs_is_a_note_not_a_fallback() -> None:
    advisor = ScriptedAdvisor(Recovery(tool="replace_order", reasoning="elsewhere"))
    engine = LangGraphEngine(advisor=advisor, classifier=ScriptedClassifier(TimeoutError("slow")))
    async with _app(engine) as c:
        sara, order = await _order_at_the_door(c)
        await _runs_out_mid_prep(c, order)
        names = _names(c, order["id"])
        assert "classify error" in names and "replace order" in names and "fallback" not in names


def test_the_drawing_has_the_new_nodes() -> None:
    drawn = mermaid()
    for node in ("classify", "suggest", "wait_customer", "apply_substitution"):
        assert node in drawn, node
