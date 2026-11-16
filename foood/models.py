"""Domain objects served to the apps; field names mirror the apps' TypeScript types."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from foood.store import iso, now


def money(value: Decimal | float | str) -> str:
    """Prices travel as strings with two decimals (the apps accept string or number)."""
    return str(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def new_id() -> str:
    return str(uuid.uuid4())


@dataclass
class Restaurant:
    name: str
    owner_id: str
    address: str
    latitude: float
    longitude: float
    status: str = "OPEN"  # OPEN | CLOSED | BUSY | SUSPENDED
    approval_status: str = "APPROVED"  # DRAFT | SUBMITTED | APPROVED | REJECTED
    description: str | None = None
    delivery_radius_km: float = 8.0
    cuisine_tags: list[str] = field(default_factory=list)
    cover_image_url: str | None = None
    logo_url: str | None = None
    prep_minutes: int = 10
    id: str = field(default_factory=new_id)
    created_at: datetime = field(default_factory=now)
    updated_at: datetime = field(default_factory=now)

    def to_json(self, distance_km: float | None = None) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "ownerId": self.owner_id,
            "name": self.name,
            "description": self.description,
            "coverImageUrl": self.cover_image_url,
            "logoUrl": self.logo_url,
            "address": self.address,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "deliveryRadiusKm": self.delivery_radius_km,
            "status": self.status,
            "approvalStatus": self.approval_status,
            "cuisineTags": list(self.cuisine_tags),
            "createdAt": iso(self.created_at),
            "updatedAt": iso(self.updated_at),
        }
        if distance_km is not None:
            data["distanceKm"] = round(distance_km, 2)
        return data


@dataclass
class MenuCategory:
    restaurant_id: str
    name: str
    display_order: int = 0
    id: str = field(default_factory=new_id)

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "restaurantId": self.restaurant_id,
            "name": self.name,
            "displayOrder": self.display_order,
        }


@dataclass
class MenuItem:
    restaurant_id: str
    name: str
    price: Decimal
    category_id: str | None = None
    description: str | None = None
    available: bool = True
    archived: bool = False
    image_url: str | None = None
    stock: int | None = None  # None = unlimited; the scenario engine counts it down
    id: str = field(default_factory=new_id)
    created_at: datetime = field(default_factory=now)
    updated_at: datetime = field(default_factory=now)

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "restaurantId": self.restaurant_id,
            "categoryId": self.category_id,
            "name": self.name,
            "description": self.description,
            "price": money(self.price),
            "imageUrl": self.image_url,
            "available": self.available and not self.archived,
            "images": [],
            "createdAt": iso(self.created_at),
            "updatedAt": iso(self.updated_at),
        }
