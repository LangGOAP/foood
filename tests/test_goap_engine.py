"""The LangGOAP engine (issue #27): the planner at the forks, no model after intake."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx

from foood.app import create_app
from foood.bench import run
from foood.engine import make_engine
from foood.goap_engine import GoapEngine, mermaid
from foood.scenario import Injection, Scenario
from foood.store import CLOCK
from tests.support import login

QA_OWNER = "qa.restaurant@delivery-plus.test"


@asynccontextmanager
async def _app(engine: GoapEngine | None = None, scenario=None) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(simulate=False, engine=engine or GoapEngine(), scenario=scenario)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        c.store = app.state.store  # type: ignore[attr-defined]
        c.engine = app.state.engine  # type: ignore[attr-defined]
        yield c


async def _paid(client, headers, item: str = "QA Pasta") -> dict:
    restaurants = (await client.get("/api/restaurants?search=QA Kitchen")).json()["items"]
    menu = (await client.get(f"/api/menus/restaurants/{restaurants[0]['id']}/menu")).json()
    chosen = next(i for i in menu["items"] if i["name"] == item)
    await client.post(
        "/api/cart/items", json={"menuItemId": chosen["id"], "quantity": 1}, headers=headers
    )
    order = (await client.post("/api/orders", json={}, headers=headers)).json()
    payment = (
        await client.post("/api/payments", json={"orderId": order["id"]}, headers=headers)
    ).json()
    await client.post(f"/api/payments/{payment['id']}/process", json={}, headers=headers)
    return order


async def _decisions(client, order_id: str) -> list[dict]:
    body = (await client.get(f"/api/_engine?orderId={order_id}")).json()
    return body["decisions"][order_id]


async def _status(client, headers, order_id: str) -> dict:
    return (await client.get(f"/api/orders/{order_id}", headers=headers)).json()


def test_engine_is_selectable_by_name() -> None:
    assert isinstance(make_engine("langgoap"), GoapEngine)


def test_the_tracer_learns_the_quiet_waits_from_the_actions() -> None:
    """A wait that only waits ending is the world moving, not an act, so the tracer records
    no decision for it; the tracer reads which actions those are from the declarations,
    not from a list kept by hand."""
    engine = GoapEngine()
    engine.install(create_app(simulate=False).state.store)
    quiet = {a.name for a in engine.actions() if a.is_wait and a.execute is None}
    assert engine.tracer.waits == quiet
    assert {"await_payment", "await_kitchen", "await_dropoff"} <= quiet
    assert "offer_substitutes" not in quiet and "dispatch" not in quiet


def test_the_drawing_is_committed_and_current() -> None:
    committed = (Path(__file__).resolve().parents[1] / "foood" / "langgoap_engine.mmd").read_text()
    assert mermaid() == committed, "run scripts/goap_mermaid.py"


async def test_a_full_order_is_one_plan() -> None:
    async with _app() as client:
        sara = await login(client, "demo.customer@example.com")
        owner = await login(client, QA_OWNER)
        driver = await login(client, "driver@example.com")
        assert (await client.get("/api/_engine")).json()["name"] == "langgoap"
        order = await _paid(client, sara)
        engine = client.engine
        plan = (await _decisions(client, order["id"]))[0]
        assert plan["decision"] == "plan"
        assert plan["steps"] == [
            "await_payment",
            "await_kitchen",
            "dispatch",
            "await_driver",
            "await_pickup",
            "await_dropoff",
        ]
        assert engine.waiting_at(order["id"]) == "await_kitchen"
        for status in ("PREPARING", "READY_FOR_PICKUP"):
            await client.patch(
                f"/api/orders/{order['id']}/status", json={"status": status}, headers=owner
            )
        assert engine.waiting_at(order["id"]) == "await_driver"
        await client.post("/api/drivers/me/online", json={}, headers=driver)
        assert engine.waiting_at(order["id"]) == "await_pickup"
        job = (await client.get("/api/deliveries/me/current", headers=driver)).json()
        for action in ("pickup", "start", "complete"):
            await client.post(f"/api/deliveries/{job['id']}/{action}", json={}, headers=driver)
        assert engine.waiting_at(order["id"]) is None
        assert (await _status(client, sara, order["id"]))["status"] == "DELIVERED"
        names = [d["decision"] for d in await _decisions(client, order["id"])]
        assert names == ["plan", "confirm", "act: dispatch", "assign driver"]  # no replan, no model


async def test_a_rejection_is_replanned_into_a_replacement() -> None:
    async with _app() as client:
        sara = await login(client, "demo.customer@example.com")
        owner = await login(client, QA_OWNER)
        order = await _paid(client, sara)
        await client.patch(
            f"/api/orders/{order['id']}/status",
            json={"status": "CANCELLED", "reason": "No pasta"},
            headers=owner,
        )
        original = await _status(client, sara, order["id"])
        assert original["status"] == "CANCELLED" and original["replacedByOrderId"]
        replacement = await _status(client, sara, original["replacedByOrderId"])
        assert replacement["status"] == "CONFIRMED" and replacement["paymentStatus"] == "COMPLETED"
        assert client.engine.waiting_at(order["id"]) == "await_kitchen"
        names = [d["decision"] for d in await _decisions(client, order["id"])]
        assert names[:4] == ["plan", "confirm", "replan", "act: replace_after_kitchen_failure"]
        replan = (await _decisions(client, order["id"]))[2]
        assert (
            replan["steps"][0] == "replace_after_kitchen_failure"
            and "deviation" in replan["because"]
        )


async def test_no_alternative_means_refund_and_cancel() -> None:
    async with _app() as client:
        sara = await login(client, "demo.customer@example.com")
        restaurants = (await client.get("/api/restaurants?search=Taco Fiesta")).json()["items"]
        menu = (await client.get(f"/api/menus/restaurants/{restaurants[0]['id']}/menu")).json()
        tacos = next(i for i in menu["items"] if i["name"] == "Al Pastor Tacos")
        await client.post(
            "/api/cart/items", json={"menuItemId": tacos["id"], "quantity": 1}, headers=sara
        )
        order = (await client.post("/api/orders", json={}, headers=sara)).json()
        payment = (
            await client.post("/api/payments", json={"orderId": order["id"]}, headers=sara)
        ).json()
        await client.post(f"/api/payments/{payment['id']}/process", json={}, headers=sara)
        taco_owner = await login(client, "taco.fiesta.owner@example.com")
        await client.patch(
            f"/api/orders/{order['id']}/status",
            json={"status": "CANCELLED", "reason": "Closed"},
            headers=taco_owner,
        )
        final = await _status(client, sara, order["id"])
        assert final["status"] == "CANCELLED" and final["paymentStatus"] == "REFUNDED"
        names = [d["decision"] for d in await _decisions(client, order["id"])]
        assert "no plan" in names and names[-1] == "refund"
        assert client.engine.waiting_at(order["id"]) is None


async def test_a_substitution_outside_the_policy_asks_support() -> None:
    async with _app() as client:
        qa = await login(client, "qa.customer@delivery-plus.test")
        owner = await login(client, QA_OWNER)
        admin = await login(client, "admin@example.com")
        order = await _paid(client, qa)
        await client.patch(
            f"/api/orders/{order['id']}/status",
            json={"status": "CANCELLED", "reason": "Out"},
            headers=owner,
        )
        assert client.engine.waiting_at(order["id"]) == "goap_action_form"
        question = (await client.get("/api/support/questions", headers=admin)).json()["items"][0]
        assert question["kind"] == "substitution" and "QA Bistro" in question["prompt"]
        await client.post(
            f"/api/support/questions/{question['id']}/answer",
            json={"value": "approve"},
            headers=admin,
        )
        original = await _status(client, qa, order["id"])
        assert original["replacedByOrderId"]
        assert client.engine.waiting_at(order["id"]) == "await_kitchen"


async def test_a_declined_substitution_is_refunded() -> None:
    async with _app() as client:
        qa = await login(client, "qa.customer@delivery-plus.test")
        owner = await login(client, QA_OWNER)
        admin = await login(client, "admin@example.com")
        order = await _paid(client, qa)
        await client.patch(
            f"/api/orders/{order['id']}/status",
            json={"status": "CANCELLED", "reason": "Out"},
            headers=owner,
        )
        question = (await client.get("/api/support/questions", headers=admin)).json()["items"][0]
        await client.post(
            f"/api/support/questions/{question['id']}/answer",
            json={"value": "decline"},
            headers=admin,
        )
        final = await _status(client, qa, order["id"])
        assert final["paymentStatus"] == "REFUNDED" and final["replacedByOrderId"] is None
        assert client.engine.waiting_at(order["id"]) is None


async def test_a_declined_card_is_retried_on_the_backup() -> None:
    scenario = Scenario("t", injections=(Injection("payment_declines", 1),))
    async with _app(scenario=scenario) as client:
        sara = await login(client, "demo.customer@example.com")
        order = await _paid(client, sara)
        final = await _status(client, sara, order["id"])
        assert final["status"] == "CONFIRMED" and final["paymentStatus"] == "COMPLETED"
        names = [d["decision"] for d in await _decisions(client, order["id"])]
        assert "replan" in names and "act: retry_backup_card" in names


async def test_no_driver_widens_the_search_then_a_late_order_is_compensated() -> None:
    async with _app() as client:
        sara = await login(client, "demo.customer@example.com")
        owner = await login(client, QA_OWNER)
        driver = await login(client, "driver@example.com")
        order = await _paid(client, sara)
        for status in ("PREPARING", "READY_FOR_PICKUP"):
            await client.patch(
                f"/api/orders/{order['id']}/status", json={"status": status}, headers=owner
            )
        CLOCK.advance(order["promiseMinutes"] * 60 + 1)
        client.engine.tick()
        decisions = await _decisions(client, order["id"])
        names = [d["decision"] for d in decisions]
        assert "act: widen_dispatch" in names
        replan = next(d for d in decisions if d["decision"] == "replan")
        assert "widen_dispatch" in replan["steps"] and "issue_credit" in replan["steps"]
        assert client.engine.waiting_at(order["id"]) == "await_driver"
        await client.post("/api/drivers/me/online", json={}, headers=driver)
        assert client.engine.waiting_at(order["id"]) == "await_pickup"
        job = (await client.get("/api/deliveries/me/current", headers=driver)).json()
        for action in ("pickup", "start", "complete"):
            await client.post(f"/api/deliveries/{job['id']}/{action}", json={}, headers=driver)
        assert client.engine.waiting_at(order["id"]) is None
        assert (await _status(client, sara, order["id"]))["status"] == "DELIVERED"
        assert (await client.get("/api/credits/me", headers=sara)).json()["balance"] == "5.00"


async def test_a_drop_after_pickup_is_recooked_elsewhere() -> None:
    async with _app() as client:
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
        original = await _status(client, sara, order["id"])
        assert original["replacedByOrderId"], original
        names = [d["decision"] for d in await _decisions(client, order["id"])]
        assert "act: replace_lost_order" in names
        assert client.engine.waiting_at(order["id"]) == "await_kitchen"


def test_bench_recovers_every_bundled_failure_without_a_model(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("FOOOD_LLM", "off")
    for name in ("happy-path", "kitchen-trouble", "delivery-trouble", "payment-trouble"):
        result = run(name, "langgoap", 7, tmp_path)
        summary = result["summary"]
        assert summary["llmCalls"] == 0, name
        assert summary["consistent"] == summary["orders"], (
            name,
            [o["problems"] for o in result["orders"]],
        )
        assert summary["unrecovered"] == [], (name, result["orders"])


async def test_a_new_engine_resumes_from_the_checkpoint(tmp_path: Path) -> None:
    path = tmp_path / "goap.sqlite"
    async with _app(GoapEngine(path)) as client:
        sara = await login(client, "demo.customer@example.com")
        owner = await login(client, QA_OWNER)
        order = await _paid(client, sara)
        first = client.engine
        assert first.waiting_at(order["id"]) == "await_kitchen"
        first.uninstall()
        second = GoapEngine(path)
        second.install(client.store)
        second.threads = dict(
            first.threads
        )  # the order-to-thread map is the platform's, not the process's
        assert second.waiting_at(order["id"]) == "await_kitchen"
        for status in ("PREPARING", "READY_FOR_PICKUP"):
            await client.patch(
                f"/api/orders/{order['id']}/status", json={"status": status}, headers=owner
            )
        assert second.waiting_at(order["id"]) == "await_driver"
        second.uninstall()


async def test_a_gateway_outage_is_waited_out() -> None:
    scenario = Scenario("t", injections=(Injection("gateway_down", 1, for_seconds=60),))
    async with _app(scenario=scenario) as client:
        sara = await login(client, "demo.customer@example.com")
        order = await _paid(client, sara)  # refused with a 503; the planner routes around it
        assert client.engine.waiting_at(order["id"]) == "await_gateway"
        names = [d["decision"] for d in await _decisions(client, order["id"])]
        assert "replan" in names
        CLOCK.advance(61)
        client.store.engine.tick()  # the gateway is back: the engine charges the attempt itself
        final = await _status(client, sara, order["id"])
        assert final["status"] == "CONFIRMED" and final["paymentStatus"] == "COMPLETED"
        assert client.engine.waiting_at(order["id"]) == "await_kitchen"
