"""Tracking (issue #10): location reports, the snapshot, the SSE stream, the simulated phone."""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta

import pytest

from foood.store import now
from tests.support import login

QA_OWNER = "qa.restaurant@delivery-plus.test"
QA_KITCHEN = (37.7731, -122.4167)  # QA Kitchen's seeded position
DROP_OFF = (37.787, -122.3985)


def _km(location: dict, target: tuple[float, float]) -> float:
    from foood.catalog import distance_km

    return distance_km(location["latitude"], location["longitude"], *target)


async def _ready_order(client, customer, owner, **extra) -> dict:
    restaurants = (await client.get("/api/restaurants?search=QA Kitchen")).json()["items"]
    menu = (await client.get(f"/api/menus/restaurants/{restaurants[0]['id']}/menu")).json()
    pasta = next(i for i in menu["items"] if i["name"] == "QA Pasta")
    await client.post(
        "/api/cart/items", json={"menuItemId": pasta["id"], "quantity": 1}, headers=customer
    )
    order = (await client.post("/api/orders", json=extra, headers=customer)).json()
    payment = (
        await client.post("/api/payments", json={"orderId": order["id"]}, headers=customer)
    ).json()
    await client.post(f"/api/payments/{payment['id']}/process", json={}, headers=customer)
    for status in ("PREPARING", "READY_FOR_PICKUP"):
        await client.patch(
            f"/api/orders/{order['id']}/status", json={"status": status}, headers=owner
        )
    return order


async def _assigned(client) -> tuple[dict, dict, dict, dict]:
    sara = await login(client, "demo.customer@example.com")
    owner = await login(client, QA_OWNER)
    driver = await login(client, "driver@example.com")
    order = await _ready_order(
        client,
        sara,
        owner,
        deliveryAddress="742 Evergreen Terrace",
        deliveryLatitude=37.787,
        deliveryLongitude=-122.3985,
    )
    await client.post("/api/drivers/me/online", json={}, headers=driver)
    delivery = (await client.get(f"/api/deliveries/by-order/{order['id']}", headers=sara)).json()
    return sara, driver, order, delivery


async def test_snapshot_follows_the_delivery(client) -> None:
    sara = await login(client, "demo.customer@example.com")
    owner = await login(client, QA_OWNER)
    driver = await login(client, "driver@example.com")
    order = await _ready_order(client, sara, owner)
    delivery = (await client.get(f"/api/deliveries/by-order/{order['id']}", headers=sara)).json()
    snapshot = await client.get(f"/api/tracking/delivery/{delivery['id']}", headers=sara)
    assert snapshot.status_code == 200
    assert snapshot.json()["tracking"] == "NO_DRIVER" and snapshot.json()["location"] is None
    await client.post("/api/drivers/me/online", json={}, headers=driver)
    awaiting = (await client.get(f"/api/tracking/delivery/{delivery['id']}", headers=sara)).json()
    assert awaiting["tracking"] == "AWAITING_LOCATION" and awaiting["assignment"]["driverId"]
    assert awaiting["eta"]["status"] == "UNAVAILABLE"
    reported = await client.post(
        "/api/tracking/location", json={"latitude": 37.765, "longitude": -122.426}, headers=driver
    )
    assert reported.status_code == 201, reported.text
    live = (await client.get(f"/api/tracking/delivery/{delivery['id']}", headers=sara)).json()
    assert live["tracking"] == "LIVE" and live["locationAgeSeconds"] == 0
    assert live["location"]["latitude"] == 37.765 and live["location"]["updatedAt"]
    assert live["status"] == "DRIVER_ASSIGNED" and live["deliveryId"] == delivery["id"]
    assert live["eta"]["status"] == "ESTIMATED" and live["eta"]["seconds"] > 0
    assert live["eta"]["driverId"] == awaiting["assignment"]["driverId"]
    for action in ("pickup", "start", "complete"):
        await client.post(f"/api/deliveries/{delivery['id']}/{action}", json={}, headers=driver)
    ended = (await client.get(f"/api/tracking/delivery/{delivery['id']}", headers=sara)).json()
    assert ended["tracking"] == "ENDED" and ended["status"] == "DELIVERED"


async def test_old_locations_go_stale(client) -> None:
    sara, driver, order, delivery = await _assigned(client)
    await client.post(
        "/api/tracking/location", json={"latitude": 37.765, "longitude": -122.426}, headers=driver
    )
    from foood.tracking import STALE_AFTER

    location = next(iter(client.store.locations.values()))
    location.received_at = now() - STALE_AFTER - timedelta(seconds=5)
    stale = (await client.get(f"/api/tracking/delivery/{delivery['id']}", headers=sara)).json()
    assert (
        stale["tracking"] == "STALE" and stale["locationAgeSeconds"] >= STALE_AFTER.total_seconds()
    )


async def test_location_reports_are_checked(client) -> None:
    sara = await login(client, "demo.customer@example.com")
    driver = await login(client, "driver@example.com")
    admin = await login(client, "admin@example.com")
    bad = await client.post(
        "/api/tracking/location", json={"latitude": 91, "longitude": 0}, headers=driver
    )
    assert bad.status_code == 400
    future = (now() + timedelta(minutes=5)).isoformat()
    ahead = await client.post(
        "/api/tracking/location",
        json={"latitude": 37.7, "longitude": -122.5, "recordedAt": future},
        headers=driver,
    )
    assert ahead.status_code == 400
    assert (
        await client.post(
            "/api/tracking/location", json={"latitude": 30, "longitude": 31}, headers=sara
        )
    ).status_code == 403
    ok = await client.post(
        "/api/tracking/location", json={"latitude": 37.7, "longitude": -122.5}, headers=driver
    )
    assert ok.status_code == 201
    me = (await client.get("/api/auth/me", headers=driver)).json()
    mine = await client.get(f"/api/tracking/driver/{me['sub']}", headers=driver)
    assert mine.status_code == 200 and mine.json()["latitude"] == 37.7
    assert (await client.get(f"/api/tracking/driver/{me['sub']}", headers=admin)).status_code == 200
    assert (await client.get(f"/api/tracking/driver/{me['sub']}", headers=sara)).status_code == 403
    assert (await client.get("/api/tracking/driver/nobody", headers=admin)).status_code == 404


async def test_only_the_parties_see_the_snapshot(client) -> None:
    sara, driver, order, delivery = await _assigned(client)
    other = await login(client, "qa.customer@delivery-plus.test")
    assert (
        await client.get(f"/api/tracking/delivery/{delivery['id']}", headers=other)
    ).status_code == 403
    assert (
        await client.get(f"/api/tracking/delivery/{delivery['id']}", headers=driver)
    ).status_code == 200
    assert (await client.get("/api/tracking/delivery/nope", headers=sara)).status_code == 404


async def _read_event(lines) -> dict:
    event = None
    async for line in lines:
        if line.startswith("event:"):
            event = line.split(":", 1)[1].strip()
        elif line.startswith("data:") and event == "tracking":
            return json.loads(line.split(":", 1)[1])
    raise AssertionError("stream closed without a tracking event")


async def test_stream_pushes_snapshots_and_ends(live) -> None:
    client = live  # httpx's ASGI transport buffers bodies, so the stream needs a real server
    sara, driver, order, delivery = await _assigned(client)
    async with client.stream(
        "GET", f"/api/tracking/delivery/{delivery['id']}/stream", headers=sara
    ) as stream:
        assert stream.status_code == 200
        assert stream.headers["content-type"].startswith("text/event-stream")
        lines = stream.aiter_lines()
        first = await asyncio.wait_for(_read_event(lines), 5)
        assert first["tracking"] == "AWAITING_LOCATION"
        await client.post(
            "/api/tracking/location",
            json={"latitude": 37.7655, "longitude": -122.4255},
            headers=driver,
        )
        moved = await asyncio.wait_for(_read_event(lines), 5)
        assert moved["tracking"] == "LIVE" and moved["location"]["latitude"] == 37.7655
        for action in ("pickup", "start", "complete"):
            await client.post(f"/api/deliveries/{delivery['id']}/{action}", json={}, headers=driver)
        seen = []
        while True:
            event = await asyncio.wait_for(_read_event(lines), 5)
            seen.append(event["tracking"])
            if event["tracking"] == "ENDED":
                break
        assert seen[-1] == "ENDED" and len(seen) >= 2
        async for line in lines:  # the server closes the stream after ENDED
            if line.strip():
                pytest.fail(f"unexpected data after ENDED: {line!r}")


async def test_stream_rejects_strangers(live) -> None:
    client = live
    sara, driver, order, delivery = await _assigned(client)
    other = await login(client, "qa.customer@delivery-plus.test")
    forbidden = await client.get(f"/api/tracking/delivery/{delivery['id']}/stream", headers=other)
    assert forbidden.status_code == 403 and forbidden.json()["error"] == "Forbidden"
    assert (await client.get(f"/api/tracking/delivery/{delivery['id']}/stream")).status_code == 401


async def test_simulated_phone_moves_the_driver_when_nobody_reports(client) -> None:
    from foood.tracking import SIM_START_KM, simulate_step

    sara, driver, order, delivery = await _assigned(client)
    store = client.store
    # first tick: the phone appears a little way from the restaurant and heads for it
    simulate_step(store)
    first = (await client.get(f"/api/tracking/delivery/{delivery['id']}", headers=sara)).json()
    assert first["tracking"] == "LIVE"
    start = _km(first["location"], QA_KITCHEN)
    assert 0 < start <= SIM_START_KM
    simulate_step(store)
    second = (await client.get(f"/api/tracking/delivery/{delivery['id']}", headers=sara)).json()
    assert _km(second["location"], QA_KITCHEN) < start
    # after pickup the phone heads for the drop-off instead
    await client.post(f"/api/deliveries/{delivery['id']}/pickup", json={}, headers=driver)
    before = (await client.get(f"/api/tracking/delivery/{delivery['id']}", headers=sara)).json()
    simulate_step(store)
    after = (await client.get(f"/api/tracking/delivery/{delivery['id']}", headers=sara)).json()
    assert _km(after["location"], DROP_OFF) < _km(before["location"], DROP_OFF)
    # a real phone takes precedence over the simulation
    await client.post(
        "/api/tracking/location", json={"latitude": 37.7, "longitude": -122.5}, headers=driver
    )
    simulate_step(store)
    real = (await client.get(f"/api/tracking/delivery/{delivery['id']}", headers=sara)).json()
    assert (real["location"]["latitude"], real["location"]["longitude"]) == (37.7, -122.5)


def test_simulated_phone_idles_without_deliveries() -> None:
    from foood.store import Store
    from foood.tracking import simulate_step

    store = Store()
    simulate_step(store)
    assert store.locations == {}


async def test_simulated_phones_can_outrank_fixed_browser_positions(client, monkeypatch) -> None:
    from foood.tracking import simulate_step

    sara, driver, order, delivery = await _assigned(client)
    await client.post(
        "/api/tracking/location", json={"latitude": 37.7, "longitude": -122.5}, headers=driver
    )
    monkeypatch.setenv("FOOOD_SIMULATED_PHONES", "true")
    simulate_step(client.store)
    moved = (await client.get(f"/api/tracking/delivery/{delivery['id']}", headers=sara)).json()
    assert (moved["location"]["latitude"], moved["location"]["longitude"]) != (37.7, -122.5)
