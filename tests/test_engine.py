"""The engine seam (issue #16): decisions live in one place, and only there."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest

from foood.app import create_app
from foood.engine import ENGINES, HandWiredEngine, NullEngine, make_engine
from tests.support import login

QA_OWNER = "qa.restaurant@delivery-plus.test"


@asynccontextmanager
async def _app(engine) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(simulate=False, engine=engine)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        c.store = app.state.store  # type: ignore[attr-defined]
        yield c


async def _paid_order(client, customer) -> dict:
    restaurants = (await client.get("/api/restaurants?search=QA Kitchen")).json()["items"]
    menu = (await client.get(f"/api/menus/restaurants/{restaurants[0]['id']}/menu")).json()
    pasta = next(i for i in menu["items"] if i["name"] == "QA Pasta")
    await client.post(
        "/api/cart/items", json={"menuItemId": pasta["id"], "quantity": 1}, headers=customer
    )
    order = (await client.post("/api/orders", json={}, headers=customer)).json()
    payment = (
        await client.post("/api/payments", json={"orderId": order["id"]}, headers=customer)
    ).json()
    await client.post(f"/api/payments/{payment['id']}/process", json={}, headers=customer)
    return order


def test_registry() -> None:
    assert set(ENGINES) == {"hand-wired", "null"}
    assert isinstance(make_engine("hand-wired"), HandWiredEngine)
    assert isinstance(make_engine("null"), NullEngine)
    with pytest.raises(ValueError, match="unknown engine"):
        make_engine("magic")


async def test_the_default_engine_is_hand_wired(client) -> None:
    status = await client.get("/api/_engine")
    assert status.status_code == 200 and status.json() == {"name": "hand-wired", "decisions": {}}


async def test_without_an_engine_the_platform_records_facts_but_decides_nothing() -> None:
    async with _app(NullEngine()) as client:
        sara = await login(client, "demo.customer@example.com")
        owner = await login(client, QA_OWNER)
        driver = await login(client, "driver@example.com")
        order = await _paid_order(client, sara)
        paid = (await client.get(f"/api/orders/{order['id']}", headers=sara)).json()
        assert paid["status"] == "PAYMENT_PENDING" and paid["paymentStatus"] == "COMPLETED"
        # nobody confirmed it, so the kitchen cannot start and no delivery exists
        blocked = await client.patch(
            f"/api/orders/{order['id']}/status", json={"status": "PREPARING"}, headers=owner
        )
        assert blocked.status_code == 409
        await client.post("/api/drivers/me/online", json={}, headers=driver)
        assert (
            await client.get(f"/api/deliveries/by-order/{order['id']}", headers=sara)
        ).status_code == 404
        # a cancellation leaves the payment un-refunded: that too was the engine's job
        from foood.orders import get_order, transition

        transition(
            client.store, get_order(client.store, order["id"]), "CANCELLED", cancelled_by="SYSTEM"
        )
        assert (await client.get(f"/api/orders/{order['id']}", headers=sara)).json()[
            "paymentStatus"
        ] == "COMPLETED"
        assert (await client.get("/api/_engine")).json() == {"name": "null", "decisions": {}}


async def test_hand_wired_decisions_are_recorded_in_order(client) -> None:
    sara = await login(client, "demo.customer@example.com")
    owner = await login(client, QA_OWNER)
    driver = await login(client, "driver@example.com")
    order = await _paid_order(client, sara)
    for status in ("PREPARING", "READY_FOR_PICKUP"):
        await client.patch(
            f"/api/orders/{order['id']}/status", json={"status": status}, headers=owner
        )
    await client.post("/api/drivers/me/online", json={}, headers=driver)
    job = (await client.get("/api/deliveries/me/current", headers=driver)).json()
    await client.post(f"/api/deliveries/{job['id']}/pickup", json={}, headers=driver)
    from foood.deliveries import drop

    drop(client.store, client.store.deliveries[job["id"]], "Flat tyre")
    decisions = (await client.get(f"/api/_engine?orderId={order['id']}")).json()["decisions"][
        order["id"]
    ]
    assert [d["decision"] for d in decisions] == [
        "confirm",
        "create delivery",
        "assign driver",
        "cancel",
        "refund",
    ]
    assert decisions[2]["licensePlate"] == "DMO-2030"
    assert decisions[2]["because"] == "a driver came online"
    final = (await client.get(f"/api/orders/{order['id']}", headers=sara)).json()
    assert final["status"] == "CANCELLED" and final["paymentStatus"] == "REFUNDED"
    assert final["cancellationNote"] == "Flat tyre"


async def test_a_drop_before_pickup_is_dispatched_again_by_the_engine(client) -> None:
    sara = await login(client, "demo.customer@example.com")
    owner = await login(client, QA_OWNER)
    first = await login(client, "driver@example.com")
    second = await login(client, "qa.driver@delivery-plus.test")
    order = await _paid_order(client, sara)
    for status in ("PREPARING", "READY_FOR_PICKUP"):
        await client.patch(
            f"/api/orders/{order['id']}/status", json={"status": status}, headers=owner
        )
    await client.post("/api/drivers/me/online", json={}, headers=first)
    await client.post("/api/drivers/me/online", json={}, headers=second)
    job = (await client.get("/api/deliveries/me/current", headers=first)).json()
    from foood.deliveries import drop

    drop(client.store, client.store.deliveries[job["id"]])
    again = (await client.get(f"/api/deliveries/by-order/{order['id']}", headers=sara)).json()
    assert again["status"] == "DRIVER_ASSIGNED" and again["driver"]["licensePlate"] == "QA-0001"
    decisions = (await client.get(f"/api/_engine?orderId={order['id']}")).json()["decisions"][
        order["id"]
    ]
    assert [d["decision"] for d in decisions][-2:] == ["dispatch again", "assign driver"]
    assert decisions[-1]["because"] == "a driver dropped the job before pickup"
