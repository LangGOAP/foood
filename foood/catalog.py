"""Restaurants and menus: the customer's read side and the owner's controls.

Path parameters are named exactly as in the Delivery Plus contract (``{id}``,
``{restaurantId}``) so the served OpenAPI document matches it literally.
"""

from __future__ import annotations

import math
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Query, Response
from pydantic import BaseModel, Field

from foood.auth import CurrentUser, StoreDep
from foood.errors import ApiError
from foood.models import MenuCategory, MenuItem, Restaurant
from foood.store import Store, User, now

router = APIRouter(prefix="/api", tags=["catalog"])

CUSTOMER_VISIBLE = ("OPEN", "CLOSED", "BUSY")


def distance_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    radius = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


def page_of(items: list[Any], page: int, limit: int) -> dict[str, Any]:
    total = len(items)
    start = (page - 1) * limit
    return {
        "items": items[start : start + limit],
        "page": page,
        "limit": limit,
        "total": total,
        "totalPages": math.ceil(total / limit) if total else 0,
    }


def owned_restaurant(store: Store, user: User, restaurant_id: str) -> Restaurant:
    restaurant = store.restaurants.get(restaurant_id)
    if restaurant is None:
        raise ApiError(404, "Restaurant not found")
    if user.role != "ADMIN" and restaurant.owner_id != user.id:
        raise ApiError(403, "Not your restaurant")
    return restaurant


def menu_of(store: Store, restaurant_id: str) -> dict[str, Any]:
    categories = sorted(
        (c for c in store.categories.values() if c.restaurant_id == restaurant_id),
        key=lambda c: (c.display_order, c.name),
    )
    items = [
        i for i in store.menu_items.values() if i.restaurant_id == restaurant_id and not i.archived
    ]
    return {
        "restaurantId": restaurant_id,
        "categories": [c.to_json() for c in categories],
        "items": [i.to_json() for i in items],
    }


# --- restaurants -----------------------------------------------------------------


class RestaurantDetails(BaseModel):
    name: str | None = None
    description: str | None = None
    address: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    cuisineTags: list[str] | None = None
    legalName: str | None = None
    commercialRegistrationNumber: str | None = None
    taxCardNumber: str | None = None
    contactPhone: str | None = None


class StatusChange(BaseModel):
    status: str


@router.get("/restaurants")
async def list_restaurants(
    store: StoreDep,
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    status: str | None = None,
    search: str | None = None,
    sortBy: str = "name",
    sortOrder: str = "ASC",
    lat: float | None = None,
    lng: float | None = None,
) -> dict[str, Any]:
    rows = [
        r
        for r in store.restaurants.values()
        if r.approval_status == "APPROVED" and r.status in CUSTOMER_VISIBLE
    ]
    if status:
        rows = [r for r in rows if r.status == status]
    if search and search.strip():
        needle = search.strip().lower()
        rows = [r for r in rows if needle in r.name.lower()]
    if lat is not None and lng is not None:
        ranked = sorted(
            ((r, distance_km(lat, lng, r.latitude, r.longitude)) for r in rows),
            key=lambda pair: pair[1],
        )
        return page_of([r.to_json(d) for r, d in ranked], page, limit)
    if sortBy == "createdAt":
        rows.sort(key=lambda r: r.created_at, reverse=sortOrder.upper() == "DESC")
    else:
        rows.sort(key=lambda r: r.name.lower(), reverse=sortOrder.upper() == "DESC")
    return page_of([r.to_json() for r in rows], page, limit)


@router.get("/restaurants/me")
async def my_restaurants(user: CurrentUser, store: StoreDep) -> list[dict[str, Any]]:
    return [r.to_json() for r in store.restaurants.values() if r.owner_id == user.id]


@router.post("/restaurants", status_code=201)
async def create_restaurant(
    body: RestaurantDetails, user: CurrentUser, store: StoreDep
) -> dict[str, Any]:
    if user.role != "RESTAURANT_OWNER":
        raise ApiError(403, "Only restaurant owners can create a restaurant")
    if not body.name or not body.address:
        raise ApiError(400, "name and address are required")
    restaurant = Restaurant(
        name=body.name,
        owner_id=user.id,
        address=body.address,
        latitude=body.latitude or 0.0,
        longitude=body.longitude or 0.0,
        status="CLOSED",
        approval_status="DRAFT",
        description=body.description,
        cuisine_tags=body.cuisineTags or [],
    )
    store.restaurants[restaurant.id] = restaurant
    return restaurant.to_json()


@router.get("/restaurants/{id}")
async def get_restaurant(id: str, store: StoreDep) -> dict[str, Any]:
    restaurant = store.restaurants.get(id)
    if restaurant is None or restaurant.approval_status != "APPROVED":
        raise ApiError(404, "Restaurant not found")
    return restaurant.to_json()


@router.patch("/restaurants/{id}")
async def update_restaurant(
    id: str, body: RestaurantDetails, user: CurrentUser, store: StoreDep
) -> dict[str, Any]:
    restaurant = owned_restaurant(store, user, id)
    for attr, value in (
        ("name", body.name),
        ("description", body.description),
        ("address", body.address),
        ("latitude", body.latitude),
        ("longitude", body.longitude),
        ("cuisine_tags", body.cuisineTags),
    ):
        if value is not None:
            setattr(restaurant, attr, value)
    restaurant.updated_at = now()
    return restaurant.to_json()


@router.post("/restaurants/{id}/submit")
async def submit_restaurant(id: str, user: CurrentUser, store: StoreDep) -> dict[str, Any]:
    """No staff console here: a submitted application is approved on the spot."""
    restaurant = owned_restaurant(store, user, id)
    restaurant.approval_status = "APPROVED"
    restaurant.updated_at = now()
    return restaurant.to_json()


@router.patch("/restaurants/{id}/status")
async def set_restaurant_status(
    id: str, body: StatusChange, user: CurrentUser, store: StoreDep
) -> dict[str, Any]:
    restaurant = owned_restaurant(store, user, id)
    if body.status not in CUSTOMER_VISIBLE:
        raise ApiError(400, "status must be OPEN, CLOSED or BUSY")
    if restaurant.approval_status != "APPROVED":
        raise ApiError(409, "Only an approved restaurant can change its status")
    restaurant.status = body.status
    restaurant.updated_at = now()
    return restaurant.to_json()


# --- menus -------------------------------------------------------------------------


class Availability(BaseModel):
    available: bool


class MenuItemDraft(BaseModel):
    restaurantId: str | None = None
    name: str | None = None
    description: str | None = None
    price: float | None = Field(default=None, ge=0)
    categoryId: str | None = None


class CategoryDraft(BaseModel):
    restaurantId: str | None = None
    name: str | None = None
    displayOrder: int | None = None


class CategoryOrder(BaseModel):
    categoryIds: list[str]


@router.get("/menus/restaurants/{restaurantId}/menu")
async def get_menu(restaurantId: str, store: StoreDep) -> dict[str, Any]:
    if restaurantId not in store.restaurants:
        raise ApiError(404, "Restaurant not found")
    return menu_of(store, restaurantId)


@router.get("/menus/menu-items/{id}")
async def get_menu_item(id: str, store: StoreDep) -> dict[str, Any]:
    item = store.menu_items.get(id)
    if item is None or item.archived:
        raise ApiError(404, "Menu item not found")
    return item.to_json()


@router.patch("/menus/menu-items/{id}/availability")
async def set_availability(
    id: str, body: Availability, user: CurrentUser, store: StoreDep
) -> dict[str, Any]:
    item = store.menu_items.get(id)
    if item is None:
        raise ApiError(404, "Menu item not found")
    owned_restaurant(store, user, item.restaurant_id)
    item.available = body.available
    item.updated_at = now()
    return item.to_json()


@router.post("/menus/menu-items", status_code=201)
async def create_menu_item(
    body: MenuItemDraft, user: CurrentUser, store: StoreDep
) -> dict[str, Any]:
    if not body.restaurantId or not body.name or body.price is None:
        raise ApiError(400, "restaurantId, name and price are required")
    owned_restaurant(store, user, body.restaurantId)
    item = MenuItem(
        restaurant_id=body.restaurantId,
        name=body.name,
        description=body.description,
        price=Decimal(str(body.price)),
        category_id=body.categoryId,
    )
    store.menu_items[item.id] = item
    return item.to_json()


@router.patch("/menus/menu-items/{id}")
async def update_menu_item(
    id: str, body: MenuItemDraft, user: CurrentUser, store: StoreDep
) -> dict[str, Any]:
    item = store.menu_items.get(id)
    if item is None:
        raise ApiError(404, "Menu item not found")
    owned_restaurant(store, user, item.restaurant_id)
    if body.name is not None:
        item.name = body.name
    if body.description is not None:
        item.description = body.description
    if body.price is not None:
        item.price = Decimal(str(body.price))
    if body.categoryId is not None:
        item.category_id = body.categoryId
    item.updated_at = now()
    return item.to_json()


@router.delete("/menus/menu-items/{id}")
async def archive_menu_item(id: str, user: CurrentUser, store: StoreDep) -> dict[str, Any]:
    item = store.menu_items.get(id)
    if item is None:
        raise ApiError(404, "Menu item not found")
    owned_restaurant(store, user, item.restaurant_id)
    item.archived = True
    item.updated_at = now()
    return item.to_json()


@router.post("/menus/categories", status_code=201)
async def create_category(
    body: CategoryDraft, user: CurrentUser, store: StoreDep
) -> dict[str, Any]:
    if not body.restaurantId or not body.name:
        raise ApiError(400, "restaurantId and name are required")
    owned_restaurant(store, user, body.restaurantId)
    category = MenuCategory(body.restaurantId, body.name, display_order=body.displayOrder or 0)
    store.categories[category.id] = category
    return category.to_json()


@router.patch("/menus/categories/{id}")
async def rename_category(
    id: str, body: CategoryDraft, user: CurrentUser, store: StoreDep
) -> dict[str, Any]:
    category = store.categories.get(id)
    if category is None:
        raise ApiError(404, "Category not found")
    owned_restaurant(store, user, category.restaurant_id)
    if body.name is not None:
        category.name = body.name
    if body.displayOrder is not None:
        category.display_order = body.displayOrder
    return category.to_json()


@router.delete("/menus/categories/{id}", status_code=204)
async def delete_category(id: str, user: CurrentUser, store: StoreDep) -> Response:
    category = store.categories.get(id)
    if category is None:
        raise ApiError(404, "Category not found")
    owned_restaurant(store, user, category.restaurant_id)
    if any(i.category_id == id and not i.archived for i in store.menu_items.values()):
        raise ApiError(409, "Category still has items")
    del store.categories[id]
    return Response(status_code=204)


@router.patch("/menus/restaurants/{restaurantId}/categories/order")
async def reorder_categories(
    restaurantId: str, body: CategoryOrder, user: CurrentUser, store: StoreDep
) -> list[dict[str, Any]]:
    owned_restaurant(store, user, restaurantId)
    for position, category_id in enumerate(body.categoryIds, start=1):
        category = store.categories.get(category_id)
        if category is not None and category.restaurant_id == restaurantId:
            category.display_order = position
    categories: list[dict[str, Any]] = menu_of(store, restaurantId)["categories"]
    return categories
