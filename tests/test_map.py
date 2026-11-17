"""The evening on one map (issue #34): one read-only endpoint beside /api/_engine, one page."""

from __future__ import annotations

from foood.launch import APPS, banner
from tests.test_tracking import _assigned


async def test_the_map_starts_with_the_kitchens_and_nothing_moving(client) -> None:
    body = (await client.get("/api/_map")).json()
    assert body["engine"] == "hand-wired" and body["scenario"] is None
    names = {k["name"] for k in body["kitchens"]}
    assert {"QA Kitchen", "Burger Palace", "Cairo Kitchen"} <= names
    assert all(37.70 <= k["latitude"] <= 37.82 for k in body["kitchens"])
    assert body["orders"] == [] and body["drivers"] == []


async def test_an_order_its_kitchen_its_door_and_its_driver_are_on_the_map(client) -> None:
    sara, driver, order, delivery = await _assigned(client)
    await client.post(
        "/api/tracking/location",
        json={"latitude": 37.7655, "longitude": -122.4255},
        headers=driver,
    )
    body = (await client.get("/api/_map")).json()
    [drawn] = [o for o in body["orders"] if o["id"] == order["id"]]
    assert drawn["status"] == "DRIVER_ASSIGNED" and drawn["restaurantName"] == "QA Kitchen"
    assert (drawn["latitude"], drawn["longitude"]) == (37.787, -122.3985)
    assert drawn["driverId"] == delivery["driverId"]
    assert drawn["deliveryStatus"] == "DRIVER_ASSIGNED"
    [moving] = [d for d in body["drivers"] if d["id"] == delivery["driverId"]]
    assert (moving["latitude"], moving["longitude"]) == (37.7655, -122.4255)
    assert moving["orderId"] == order["id"] and moving["plate"]


async def test_the_page_is_served_without_a_sign_in(client) -> None:
    page = await client.get("/map")
    assert page.status_code == 200
    assert "leaflet" in page.text and "/api/_map" in page.text


def test_the_banner_points_at_the_map() -> None:
    assert "http://localhost:3000/map" in banner("http://localhost:3000", APPS)
