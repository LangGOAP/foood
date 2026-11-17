"""The LangGraph engine (issue #17, step one): the lifecycle as a checkpointed graph."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx

from foood.app import create_app
from foood.bench import run
from foood.engine import make_engine
from foood.graph_engine import LangGraphEngine, mermaid
from foood.store import CLOCK
from tests.support import login

QA_OWNER = "qa.restaurant@delivery-plus.test"
SCENARIOS = ["happy-path", "kitchen-trouble", "delivery-trouble", "payment-trouble"]


@asynccontextmanager
async def _app(
    engine: LangGraphEngine | None = None, scenario=None
) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(simulate=False, engine=engine or LangGraphEngine(), scenario=scenario)
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


def test_engine_is_selectable_by_name(monkeypatch) -> None:
    assert isinstance(make_engine("langgraph"), LangGraphEngine)
    monkeypatch.setenv("FOOOD_CHECKPOINTS", "/tmp/x.sqlite")
    assert make_engine("langgraph").checkpoint_path == Path("/tmp/x.sqlite")


def test_the_drawing_is_committed_and_current() -> None:
    drawn = mermaid()
    committed = (Path(__file__).resolve().parents[1] / "foood" / "langgraph_engine.mmd").read_text()
    assert drawn == committed, "run scripts/graph_mermaid.py"
    for node in (
        "wait_payment",
        "wait_kitchen",
        "dispatch",
        "wait_driver",
        "wait_pickup",
        "wait_dropoff",
        "recover",
    ):
        assert node in drawn
    assert "failure_driver_dropped_after_pickup" in drawn


async def test_a_full_order_walks_the_graph() -> None:
    async with _app() as client:
        sara = await login(client, "demo.customer@example.com")
        owner = await login(client, QA_OWNER)
        driver = await login(client, "driver@example.com")
        assert (await client.get("/api/_engine")).json()["name"] == "langgraph"
        order = await _paid(client, sara)
        engine = client.engine
        assert engine.waiting_at(order["id"]) == "wait_kitchen"
        assert (await client.get(f"/api/orders/{order['id']}", headers=sara)).json()[
            "status"
        ] == "CONFIRMED"
        for status in ("PREPARING", "READY_FOR_PICKUP"):
            await client.patch(
                f"/api/orders/{order['id']}/status", json={"status": status}, headers=owner
            )
        assert engine.waiting_at(order["id"]) == "wait_driver"
        await client.post("/api/drivers/me/online", json={}, headers=driver)
        assert engine.waiting_at(order["id"]) == "wait_pickup"
        job = (await client.get("/api/deliveries/me/current", headers=driver)).json()
        await client.post(f"/api/deliveries/{job['id']}/pickup", json={}, headers=driver)
        assert engine.waiting_at(order["id"]) == "wait_dropoff"
        await client.post(f"/api/deliveries/{job['id']}/start", json={}, headers=driver)
        assert engine.waiting_at(order["id"]) == "wait_dropoff"
        await client.post(f"/api/deliveries/{job['id']}/complete", json={}, headers=driver)
        assert engine.waiting_at(order["id"]) is None
        final = (await client.get(f"/api/orders/{order['id']}", headers=sara)).json()
        assert final["status"] == "DELIVERED"
        decisions = (await client.get(f"/api/_engine?orderId={order['id']}")).json()["decisions"][
            order["id"]
        ]
        assert [d["decision"] for d in decisions] == ["confirm", "create delivery", "assign driver"]


async def test_a_customer_cancellation_is_tidied_up() -> None:
    async with _app() as client:
        sara = await login(client, "demo.customer@example.com")
        order = await _paid(client, sara)
        await client.patch(
            f"/api/orders/{order['id']}/status", json={"status": "CANCELLED"}, headers=sara
        )
        final = (await client.get(f"/api/orders/{order['id']}", headers=sara)).json()
        assert final["status"] == "CANCELLED" and final["paymentStatus"] == "REFUNDED"
        assert client.engine.waiting_at(order["id"]) is None


def _outcome(result: dict) -> dict:
    """The summary minus the decision count: the graph logs 'keep waiting', the other does not."""
    return {k: v for k, v in result["summary"].items() if k != "recoveryDecisions"}


def test_bench_parity_with_the_hand_wired_engine(tmp_path: Path, monkeypatch) -> None:
    """With the model off, the graph ends every order exactly where the hand-wired engine does."""
    monkeypatch.setenv("FOOOD_LLM", "off")
    for name in SCENARIOS:
        wired = run(name, "hand-wired", 7, tmp_path / "wired")
        graphed = run(name, "langgraph", 7, tmp_path / "graph")
        assert _outcome(graphed) == _outcome(wired), name
        assert [(o["status"], o["minutes"], o["withinPromise"]) for o in graphed["orders"]] == [
            (o["status"], o["minutes"], o["withinPromise"]) for o in wired["orders"]
        ], name


async def test_a_new_engine_resumes_from_the_checkpoint(tmp_path: Path) -> None:
    path = tmp_path / "checkpoints.sqlite"
    async with _app(LangGraphEngine(path)) as client:
        sara = await login(client, "demo.customer@example.com")
        owner = await login(client, QA_OWNER)
        driver = await login(client, "driver@example.com")
        order = await _paid(client, sara)
        first = client.engine
        assert first.waiting_at(order["id"]) == "wait_kitchen"
        # the process dies: its listeners are gone, the checkpoint file is not
        first.uninstall()
        second = LangGraphEngine(path)
        second.install(client.store)
        assert second.waiting_at(order["id"]) == "wait_kitchen"
        for status in ("PREPARING", "READY_FOR_PICKUP"):
            await client.patch(
                f"/api/orders/{order['id']}/status", json={"status": status}, headers=owner
            )
        assert second.waiting_at(order["id"]) == "wait_driver"
        await client.post("/api/drivers/me/online", json={}, headers=driver)
        assert second.waiting_at(order["id"]) == "wait_pickup"
        assert (await client.get(f"/api/orders/{order['id']}", headers=sara)).json()[
            "status"
        ] == "DRIVER_ASSIGNED"
        second.uninstall()


async def test_time_based_failures_fire_once() -> None:
    async with _app() as client:
        sara = await login(client, "demo.customer@example.com")
        owner = await login(client, QA_OWNER)
        order = await _paid(client, sara)
        await client.patch(
            f"/api/orders/{order['id']}/status", json={"status": "PREPARING"}, headers=owner
        )
        CLOCK.advance(order["promiseMinutes"] * 60 + 1)
        client.engine.tick()
        client.engine.tick()
        decisions = (await client.get(f"/api/_engine?orderId={order['id']}")).json()["decisions"][
            order["id"]
        ]
        assert [d["decision"] for d in decisions] == ["confirm", "keep waiting"]
        assert client.engine.waiting_at(order["id"]) == "wait_kitchen"
