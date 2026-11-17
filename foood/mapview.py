"""The evening on one map (issue #34): one read-only endpoint, one page.

``GET /api/_map`` is not part of the Delivery Plus contract, like ``/api/_engine`` and
``/api/_scenario``: the kitchens, every order placed since the server started with its
door and the driver carrying it, and the drivers where their phones (or the simulated
phones) last reported. ``/map`` draws it with Leaflet over OpenStreetMap and polls.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from foood.deliveries import current_delivery, delivery_for_order
from foood.store import Store, iso, now

router = APIRouter(tags=["map"])
PAGE = Path(__file__).with_name("map.html")


def picture(store: Store, engine_name: str) -> dict[str, Any]:
    """Everything the map draws, read from the store as it is now."""
    kitchens = [
        {
            "id": r.id,
            "name": r.name,
            "latitude": r.latitude,
            "longitude": r.longitude,
            "status": r.status,
        }
        for r in store.restaurants.values()
    ]
    orders = []
    for order in sorted(store.orders.values(), key=lambda o: o.created_at):
        delivery = delivery_for_order(store, order.id)
        kitchen = store.restaurants.get(order.restaurant_id)
        orders.append(
            {
                "id": order.id,
                "status": order.status,
                "createdAt": iso(order.created_at),
                "deliveredAt": iso(order.delivered_at) if order.delivered_at else None,
                "restaurantId": order.restaurant_id,
                "restaurantName": kitchen.name if kitchen else None,
                "latitude": order.delivery_latitude,
                "longitude": order.delivery_longitude,
                "driverId": delivery.driver_id if delivery else None,
                "deliveryStatus": delivery.status if delivery else None,
                "replacedByOrderId": order.replaced_by_order_id,
            }
        )
    drivers = []
    for driver in store.drivers.values():
        location = store.locations.get(driver.user_id)
        if location is None and driver.status == "OFFLINE":
            continue  # nothing to draw: never online, never reported a position
        user = store.users.get(driver.user_id)
        job = current_delivery(store, driver)
        drivers.append(
            {
                "id": driver.id,
                "userId": driver.user_id,
                "name": user.full_name if user else None,
                "plate": driver.license_plate,
                "status": driver.status,
                "latitude": location.latitude if location else None,
                "longitude": location.longitude if location else None,
                "updatedAt": iso(location.received_at) if location else None,
                "deliveryId": job.id if job else None,
                "orderId": job.order_id if job else None,
            }
        )
    scenario = getattr(store, "engine", None)  # the scenario engine, when one is loaded
    return {
        "engine": engine_name,
        "scenario": scenario.status()["name"] if scenario is not None else None,
        "at": iso(now()),
        "kitchens": kitchens,
        "orders": orders,
        "drivers": drivers,
    }


@router.get("/api/_map")
async def map_status(request: Request) -> dict[str, Any]:
    """The kitchens, the orders and the drivers as they are now (not part of the contract)."""
    return picture(request.app.state.store, request.app.state.engine.name)


@router.get("/map", response_class=HTMLResponse, include_in_schema=False)
async def map_page() -> HTMLResponse:
    return HTMLResponse(PAGE.read_text(encoding="utf-8"))
