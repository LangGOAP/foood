"""The customer's cart: one restaurant at a time, re-checked against the live menu.

``reconcile`` is shared with order creation: a cart that drifted from the menu
(price, availability, a removed item, a quantity over the cap) is corrected in
place and the differences are reported as a 409 ``CartChanged``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from foood.auth import CurrentUser, StoreDep
from foood.errors import ApiError
from foood.store import Store, iso, now

router = APIRouter(prefix="/api", tags=["cart"])

MAX_ITEM_QUANTITY = 20


@dataclass
class CartItem:
    menu_item_id: str
    name: str
    price: Decimal
    quantity: int
    image_url: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "menuItemId": self.menu_item_id,
            "name": self.name,
            "price": float(self.price),
            "quantity": self.quantity,
            "imageUrl": self.image_url,
        }


@dataclass
class Cart:
    user_id: str
    restaurant_id: str | None = None
    items: list[CartItem] = field(default_factory=list)
    updated_at: datetime = field(default_factory=now)

    @property
    def subtotal(self) -> Decimal:
        return sum((i.price * i.quantity for i in self.items), Decimal("0"))

    def find(self, menu_item_id: str) -> CartItem | None:
        return next((i for i in self.items if i.menu_item_id == menu_item_id), None)

    def touch(self) -> None:
        self.updated_at = now()
        if not self.items:
            self.restaurant_id = None

    def to_json(self) -> dict[str, Any]:
        return {
            "userId": self.user_id,
            "restaurantId": self.restaurant_id,
            "items": [i.to_json() for i in self.items],
            "updatedAt": iso(self.updated_at),
            "total": float(self.subtotal),
        }


def cart_of(store: Store, user_id: str) -> Cart:
    cart = store.carts.get(user_id)
    if cart is None:
        cart = store.carts[user_id] = Cart(user_id=user_id)
    return cart


def reconcile(cart: Cart, store: Store) -> list[dict[str, Any]]:
    """Bring ``cart`` in line with the menu; return the CartChanged ``changes`` list."""
    changes: list[dict[str, Any]] = []
    kept: list[CartItem] = []
    for line in cart.items:
        item = store.menu_items.get(line.menu_item_id)
        if item is None or item.archived:
            changes.append(
                {"menuItemId": line.menu_item_id, "name": line.name, "change": "REMOVED"}
            )
            continue
        if not item.available:
            changes.append(
                {"menuItemId": line.menu_item_id, "name": line.name, "change": "UNAVAILABLE"}
            )
            continue
        if item.price != line.price:
            changes.append(
                {
                    "menuItemId": line.menu_item_id,
                    "name": line.name,
                    "change": "PRICE_CHANGED",
                    "previousPrice": float(line.price),
                    "currentPrice": float(item.price),
                }
            )
            line.price = item.price
        if line.quantity > MAX_ITEM_QUANTITY:
            changes.append(
                {
                    "menuItemId": line.menu_item_id,
                    "name": line.name,
                    "change": "QUANTITY_LIMITED",
                    "previousQuantity": line.quantity,
                    "currentQuantity": MAX_ITEM_QUANTITY,
                }
            )
            line.quantity = MAX_ITEM_QUANTITY
        line.name = item.name
        kept.append(line)
    if changes:
        cart.items = kept
        cart.touch()
    return changes


def cart_changed(changes: list[dict[str, Any]]) -> ApiError:
    return ApiError(
        409,
        "Your cart changed: a price, an item or a quantity is different now.",
        error="CartChanged",
        details={"changes": changes},
    )


class AddItem(BaseModel):
    menuItemId: str
    quantity: int = 1


class Quantity(BaseModel):
    quantity: int


def _check_quantity(quantity: int) -> None:
    if quantity < 1 or quantity > MAX_ITEM_QUANTITY:
        raise ApiError(400, f"quantity must be between 1 and {MAX_ITEM_QUANTITY}")


@router.get("/cart")
async def get_cart(user: CurrentUser, store: StoreDep) -> dict[str, Any]:
    return cart_of(store, user.id).to_json()


@router.delete("/cart")
async def clear_cart(user: CurrentUser, store: StoreDep) -> dict[str, Any]:
    cart = cart_of(store, user.id)
    cart.items.clear()
    cart.touch()
    return cart.to_json()


@router.post("/cart/items", status_code=201)
async def add_item(body: AddItem, user: CurrentUser, store: StoreDep) -> dict[str, Any]:
    _check_quantity(body.quantity)
    item = store.menu_items.get(body.menuItemId)
    if item is None or item.archived:
        raise ApiError(404, "Menu item not found")
    if not item.available:
        raise ApiError(409, f"{item.name} is sold out", error="ItemUnavailable")
    cart = cart_of(store, user.id)
    if cart.items and cart.restaurant_id != item.restaurant_id:
        raise ApiError(409, "Your cart has items from another restaurant", error="Conflict")
    line = cart.find(item.id)
    if line is None:
        cart.items.append(CartItem(item.id, item.name, item.price, body.quantity, item.image_url))
    else:
        line.quantity += body.quantity
    _check_quantity(cart.find(item.id).quantity)  # type: ignore[union-attr]
    cart.restaurant_id = item.restaurant_id
    cart.touch()
    return cart.to_json()


@router.patch("/cart/items/{menuItemId}")
async def update_item(
    menuItemId: str, body: Quantity, user: CurrentUser, store: StoreDep
) -> dict[str, Any]:
    _check_quantity(body.quantity)
    cart = cart_of(store, user.id)
    line = cart.find(menuItemId)
    if line is None:
        raise ApiError(404, "Item is not in the cart")
    line.quantity = body.quantity
    cart.touch()
    return cart.to_json()


@router.delete("/cart/items/{menuItemId}")
async def remove_item(menuItemId: str, user: CurrentUser, store: StoreDep) -> dict[str, Any]:
    cart = cart_of(store, user.id)
    line = cart.find(menuItemId)
    if line is None:
        raise ApiError(404, "Item is not in the cart")
    cart.items.remove(line)
    cart.touch()
    return cart.to_json()


@router.post("/cart/validate", status_code=201)
async def validate_cart(user: CurrentUser, store: StoreDep) -> dict[str, Any]:
    cart = cart_of(store, user.id)
    changes = reconcile(cart, store)
    if changes:
        raise cart_changed(changes)
    return cart.to_json()
