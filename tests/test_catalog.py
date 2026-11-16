"""Restaurants and menus, read side plus the owner's controls (issue #4)."""

from __future__ import annotations

import pytest

from tests.support import assert_conforms

PASSWORD = "password123"


async def _token(client, email: str) -> dict[str, str]:
    response = await client.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['accessToken']}"}


async def test_restaurant_list_conforms_and_pages(client) -> None:
    response = await client.get("/api/restaurants?page=1&limit=3")
    body = assert_conforms("GET", "/api/restaurants", response)
    assert response.status_code == 200
    assert body["page"] == 1 and body["limit"] == 3 and len(body["items"]) == 3
    assert body["total"] == 7 and body["totalPages"] == 3  # six demo restaurants + QA Kitchen
    names = {r["name"] for r in (await client.get("/api/restaurants?limit=50")).json()["items"]}
    assert {"Burger Palace", "Pizza Napoli", "Cairo Kitchen", "Green Bowl", "QA Kitchen"} <= names


async def test_restaurant_detail_conforms(client) -> None:
    items = (await client.get("/api/restaurants?limit=50")).json()["items"]
    burger = next(r for r in items if r["name"] == "Burger Palace")
    response = await client.get(f"/api/restaurants/{burger['id']}")
    body = assert_conforms("GET", "/api/restaurants/{id}", response)
    assert body["status"] == "OPEN" and body["address"] == "123 Main St"
    assert body["latitude"] == pytest.approx(30.0444) and body["deliveryRadiusKm"] > 0
    assert (await client.get("/api/restaurants/nope")).status_code == 404


async def test_search_status_and_distance(client) -> None:
    found = (await client.get("/api/restaurants?search=pizza")).json()["items"]
    assert [r["name"] for r in found] == ["Pizza Napoli"]
    closed = (await client.get("/api/restaurants?status=CLOSED")).json()["items"]
    assert [r["name"] for r in closed] == ["Green Bowl"]
    near = (await client.get("/api/restaurants?lat=30.0444&lng=31.2357&limit=50")).json()["items"]
    assert near[0]["name"] == "Burger Palace" and near[0]["distanceKm"] == pytest.approx(
        0, abs=0.01
    )
    assert [r["distanceKm"] for r in near] == sorted(r["distanceKm"] for r in near)


async def test_qa_kitchen_menu_has_the_sold_out_item(client) -> None:
    items = (await client.get("/api/restaurants?search=QA")).json()["items"]
    qa = items[0]
    assert qa["description"] == "Deterministic restaurant for UI tests"
    menu = (await client.get(f"/api/menus/restaurants/{qa['id']}/menu")).json()
    assert menu["restaurantId"] == qa["id"]
    assert [c["name"] for c in menu["categories"]] == ["Mains", "Sides", "Drinks"]
    by_name = {i["name"]: i for i in menu["items"]}
    assert by_name["QA Burger"]["price"] == "10.00" and by_name["QA Burger"]["available"] is True
    assert by_name["QA Soup"]["available"] is False
    item = (await client.get(f"/api/menus/menu-items/{by_name['QA Pasta']['id']}")).json()
    assert item["price"] == "12.50" and item["restaurantId"] == qa["id"]


async def test_owner_sees_and_controls_their_restaurant(client) -> None:
    owner = await _token(client, "owner@example.com")
    mine = (await client.get("/api/restaurants/me", headers=owner)).json()
    assert [r["name"] for r in mine] == ["Burger Palace"]
    rid = mine[0]["id"]
    busy = await client.patch(
        f"/api/restaurants/{rid}/status", json={"status": "BUSY"}, headers=owner
    )
    assert busy.status_code == 200 and busy.json()["status"] == "BUSY"
    assert (await client.get(f"/api/restaurants/{rid}")).json()["status"] == "BUSY"
    customer = await _token(client, "demo.customer@example.com")
    forbidden = await client.patch(
        f"/api/restaurants/{rid}/status", json={"status": "OPEN"}, headers=customer
    )
    assert forbidden.status_code == 403
    other = await _token(client, "pizza.napoli.owner@example.com")
    assert (
        await client.patch(f"/api/restaurants/{rid}/status", json={"status": "OPEN"}, headers=other)
    ).status_code == 403


async def test_owner_toggles_availability_and_edits_the_menu(client) -> None:
    owner = await _token(client, "owner@example.com")
    rid = (await client.get("/api/restaurants/me", headers=owner)).json()[0]["id"]
    menu = (await client.get(f"/api/menus/restaurants/{rid}/menu")).json()
    fries = next(i for i in menu["items"] if i["name"] == "Fries")
    off = await client.patch(
        f"/api/menus/menu-items/{fries['id']}/availability",
        json={"available": False},
        headers=owner,
    )
    assert off.status_code == 200 and off.json()["available"] is False
    category = await client.post(
        "/api/menus/categories",
        json={"restaurantId": rid, "name": "Specials", "displayOrder": 9},
        headers=owner,
    )
    assert category.status_code == 201
    created = await client.post(
        "/api/menus/menu-items",
        json={
            "restaurantId": rid,
            "name": "Lunch Deal",
            "price": 9.5,
            "categoryId": category.json()["id"],
        },
        headers=owner,
    )
    assert created.status_code == 201 and created.json()["price"] == "9.50"
    blocked = await client.delete(f"/api/menus/categories/{category.json()['id']}", headers=owner)
    assert blocked.status_code == 409
    archived = await client.delete(f"/api/menus/menu-items/{created.json()['id']}", headers=owner)
    assert archived.status_code == 200
    menu_after = (await client.get(f"/api/menus/restaurants/{rid}/menu")).json()
    assert all(i["name"] != "Lunch Deal" for i in menu_after["items"])
    assert (
        await client.delete(f"/api/menus/categories/{category.json()['id']}", headers=owner)
    ).status_code == 204


async def test_new_restaurant_is_a_draft_until_submitted(client) -> None:
    headers = await _token(client, "demo.customer@example.com")
    register = await client.post(
        "/api/auth/register",
        json={
            "email": "chef@example.com",
            "password": "secret12",
            "fullName": "Chef",
            "role": "RESTAURANT_OWNER",
        },
    )
    headers = {"Authorization": f"Bearer {register.json()['accessToken']}"}
    draft = await client.post(
        "/api/restaurants",
        json={"name": "Chef's Table", "address": "1 Food St", "latitude": 30.0, "longitude": 31.2},
        headers=headers,
    )
    assert draft.status_code == 201 and draft.json()["approvalStatus"] == "DRAFT"
    rid = draft.json()["id"]
    assert all(
        r["id"] != rid for r in (await client.get("/api/restaurants?limit=50")).json()["items"]
    )
    submitted = await client.post(f"/api/restaurants/{rid}/submit", json={}, headers=headers)
    assert submitted.json()["approvalStatus"] == "APPROVED"
    opened = await client.patch(
        f"/api/restaurants/{rid}/status", json={"status": "OPEN"}, headers=headers
    )
    assert opened.status_code == 200
    assert any(
        r["id"] == rid for r in (await client.get("/api/restaurants?limit=50")).json()["items"]
    )
