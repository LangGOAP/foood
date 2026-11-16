"""The customer's cart (issue #5): one restaurant at a time, re-checked against the menu."""

from __future__ import annotations

from decimal import Decimal

from foood.models import MenuItem
from tests.support import assert_conforms, login


async def _menu(client, name: str) -> dict:
    restaurants = (await client.get(f"/api/restaurants?search={name}")).json()["items"]
    menu = (await client.get(f"/api/menus/restaurants/{restaurants[0]['id']}/menu")).json()
    return {"restaurantId": restaurants[0]["id"], **{i["name"]: i for i in menu["items"]}}


async def test_empty_cart_then_add_update_remove(client) -> None:
    sara = await login(client, "demo.customer@example.com")
    empty = await client.get("/api/cart", headers=sara)
    body = assert_conforms("GET", "/api/cart", empty)
    assert body["items"] == [] and body["restaurantId"] is None and body["total"] == 0
    qa = await _menu(client, "QA")
    added = await client.post(
        "/api/cart/items", json={"menuItemId": qa["QA Burger"]["id"], "quantity": 2}, headers=sara
    )
    assert added.status_code == 201, added.text
    cart = added.json()
    assert cart["restaurantId"] == qa["restaurantId"] and cart["total"] == 20.0
    assert cart["items"][0] == {
        "menuItemId": qa["QA Burger"]["id"],
        "name": "QA Burger",
        "price": 10.0,
        "quantity": 2,
        "imageUrl": None,
    }
    again = await client.post(
        "/api/cart/items", json={"menuItemId": qa["QA Burger"]["id"], "quantity": 1}, headers=sara
    )
    assert again.json()["items"][0]["quantity"] == 3
    fries = await client.post(
        "/api/cart/items", json={"menuItemId": qa["QA Fries"]["id"], "quantity": 1}, headers=sara
    )
    assert fries.json()["total"] == 34.0
    patched = await client.patch(
        f"/api/cart/items/{qa['QA Burger']['id']}", json={"quantity": 1}, headers=sara
    )
    assert patched.status_code == 200 and patched.json()["total"] == 14.0
    removed = await client.delete(f"/api/cart/items/{qa['QA Fries']['id']}", headers=sara)
    assert removed.status_code == 200 and [i["name"] for i in removed.json()["items"]] == [
        "QA Burger"
    ]
    cleared = await client.delete("/api/cart", headers=sara)
    assert cleared.status_code == 200
    assert (await client.get("/api/cart", headers=sara)).json()["items"] == []


async def test_cart_rejects_bad_adds(client) -> None:
    sara = await login(client, "demo.customer@example.com")
    qa = await _menu(client, "QA")
    missing = await client.post(
        "/api/cart/items", json={"menuItemId": "nope", "quantity": 1}, headers=sara
    )
    assert missing.status_code == 404
    sold_out = await client.post(
        "/api/cart/items", json={"menuItemId": qa["QA Soup"]["id"], "quantity": 1}, headers=sara
    )
    assert sold_out.status_code == 409 and sold_out.json()["error"] == "ItemUnavailable"
    too_many = await client.post(
        "/api/cart/items", json={"menuItemId": qa["QA Burger"]["id"], "quantity": 21}, headers=sara
    )
    assert too_many.status_code == 400
    await client.post(
        "/api/cart/items", json={"menuItemId": qa["QA Burger"]["id"], "quantity": 1}, headers=sara
    )
    pizza = await _menu(client, "Pizza")
    other = await client.post(
        "/api/cart/items",
        json={"menuItemId": pizza["Margherita"]["id"], "quantity": 1},
        headers=sara,
    )
    assert other.status_code == 409 and other.json()["error"] == "Conflict"
    unknown = await client.patch("/api/cart/items/nope", json={"quantity": 2}, headers=sara)
    assert unknown.status_code == 404
    assert (await client.get("/api/cart")).status_code == 401


async def test_validate_reports_and_applies_menu_changes(client) -> None:
    sara = await login(client, "demo.customer@example.com")
    owner = await login(client, "qa.restaurant@delivery-plus.test")
    qa = await _menu(client, "QA")
    for name in ("QA Burger", "QA Pasta", "QA Fries"):
        await client.post(
            "/api/cart/items", json={"menuItemId": qa[name]["id"], "quantity": 2}, headers=sara
        )
    fine = await client.post("/api/cart/validate", headers=sara)
    assert fine.status_code == 201 and fine.json()["total"] == 53.0
    # the kitchen changes its menu under the cart
    await client.patch(
        f"/api/menus/menu-items/{qa['QA Burger']['id']}", json={"price": 11}, headers=owner
    )
    await client.patch(
        f"/api/menus/menu-items/{qa['QA Pasta']['id']}/availability",
        json={"available": False},
        headers=owner,
    )
    await client.delete(f"/api/menus/menu-items/{qa['QA Fries']['id']}", headers=owner)
    changed = await client.post("/api/cart/validate", headers=sara)
    body = assert_conforms("POST", "/api/cart/validate", changed)
    assert changed.status_code == 409 and body["error"] == "CartChanged"
    changes = {c["name"]: c for c in body["details"]["changes"]}
    assert changes["QA Burger"]["change"] == "PRICE_CHANGED"
    assert (
        changes["QA Burger"]["previousPrice"] == 10.0
        and changes["QA Burger"]["currentPrice"] == 11.0
    )
    assert changes["QA Pasta"]["change"] == "UNAVAILABLE"
    assert changes["QA Fries"]["change"] == "REMOVED"
    cart = (await client.get("/api/cart", headers=sara)).json()
    assert [(i["name"], i["price"], i["quantity"]) for i in cart["items"]] == [
        ("QA Burger", 11.0, 2)
    ]
    assert cart["total"] == 22.0
    assert (await client.post("/api/cart/validate", headers=sara)).status_code == 201


def test_quantity_cap_is_a_validate_change() -> None:
    from foood.cart import MAX_ITEM_QUANTITY, Cart, CartItem, reconcile
    from foood.store import Store

    store = Store()
    item = MenuItem(restaurant_id="r", name="Thing", price=Decimal("1.00"))
    store.menu_items[item.id] = item
    cart = Cart(user_id="u", restaurant_id="r")
    cart.items.append(CartItem(item.id, "Thing", Decimal("1.00"), MAX_ITEM_QUANTITY + 5))
    changes = reconcile(cart, store)
    assert changes == [
        {
            "menuItemId": item.id,
            "name": "Thing",
            "change": "QUANTITY_LIMITED",
            "previousQuantity": MAX_ITEM_QUANTITY + 5,
            "currentQuantity": MAX_ITEM_QUANTITY,
        }
    ]
    assert cart.items[0].quantity == MAX_ITEM_QUANTITY
