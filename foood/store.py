"""In-memory state of the platform, seeded with the Delivery Plus demo accounts.

One ``Store`` per application instance; everything the API serves lives here
until a scenario or a restart clears it. Keeping it in memory is deliberate:
runs are deterministic and the whole platform starts with one command.
"""

from __future__ import annotations

import secrets
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from foood.cart import Cart
    from foood.models import MenuCategory, MenuItem, Restaurant
    from foood.orders import Order
    from foood.payments import Payment, Refund

PASSWORD = "password123"
DEMO_CODE = "123456"
ACCESS_TTL = timedelta(minutes=15)
REFRESH_TTL = timedelta(days=30)

RESTAURANT_NAMES = [
    "Burger Palace",
    "Pizza Napoli",
    "Cairo Kitchen",
    "Taco Fiesta",
    "Sakura Sushi",
    "Green Bowl",
]


def owner_email(restaurant: str) -> str:
    """Delivery Plus convention: Burger Palace keeps owner@example.com, the rest get a slug."""
    if restaurant == "Burger Palace":
        return "owner@example.com"
    slug = "".join(c if c.isalnum() else "." for c in restaurant.lower()).strip(".")
    while ".." in slug:
        slug = slug.replace("..", ".")
    return f"{slug}.owner@example.com"


def now() -> datetime:
    return datetime.now(UTC)


def iso(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


@dataclass
class User:
    id: str
    email: str
    password: str
    full_name: str
    role: str
    phone: str | None = None
    address: str | None = None
    avatar_url: str | None = None
    phone_verified_at: datetime | None = None
    created_at: datetime = field(default_factory=now)
    updated_at: datetime = field(default_factory=now)

    def profile(self) -> dict[str, object]:
        return {
            "id": self.id,
            "email": self.email,
            "fullName": self.full_name,
            "phone": self.phone,
            "address": self.address,
            "avatarUrl": self.avatar_url,
            "phoneVerifiedAt": iso(self.phone_verified_at) if self.phone_verified_at else None,
            "createdAt": iso(self.created_at),
            "updatedAt": iso(self.updated_at),
        }


@dataclass
class RefreshSession:
    user_id: str
    token: str
    expires_at: datetime
    family: str  # one family per login; reuse of a rotated token revokes the family


@dataclass
class Store:
    users: dict[str, User] = field(default_factory=dict)
    restaurants: dict[str, Restaurant] = field(default_factory=dict)
    categories: dict[str, MenuCategory] = field(default_factory=dict)
    menu_items: dict[str, MenuItem] = field(default_factory=dict)
    carts: dict[str, Cart] = field(default_factory=dict)
    orders: dict[str, Order] = field(default_factory=dict)
    payments: dict[str, Payment] = field(default_factory=dict)
    refunds: dict[str, Refund] = field(default_factory=dict)  # keyed by payment id
    #: (user id, Idempotency-Key) -> id of the resource that request created
    idempotency: dict[tuple[str, str], str] = field(default_factory=dict)
    listeners: dict[str, list[Callable[..., None]]] = field(default_factory=dict)
    #: replaceable decision points (``charge``, later dispatch and timing) for the scenario engine
    hooks: dict[str, Callable[..., Any]] = field(default_factory=dict)

    def subscribe(self, event: str, listener: Callable[..., None]) -> None:
        self.listeners.setdefault(event, []).append(listener)

    def publish(self, event: str, **payload: Any) -> None:
        """Tell the other modules (dispatch, notifications, scenarios) what just happened."""
        for listener in self.listeners.get(event, []):
            listener(**payload)

    refresh: dict[str, RefreshSession] = field(default_factory=dict)
    consumed: dict[str, str] = field(default_factory=dict)  # rotated token -> family
    revoked_families: set[str] = field(default_factory=set)

    # -- users ---------------------------------------------------------------
    def user_by_email(self, email: str) -> User | None:
        wanted = email.lower()
        return next((u for u in self.users.values() if u.email.lower() == wanted), None)

    def add_user(
        self, email: str, password: str, full_name: str, role: str, **extra: object
    ) -> User:
        if self.user_by_email(email):
            raise ValueError("email already registered")
        user = User(
            id=str(uuid.uuid4()),
            email=email,
            password=password,
            full_name=full_name,
            role=role,
            **extra,  # type: ignore[arg-type]
        )
        self.users[user.id] = user
        return user

    # -- refresh sessions ---------------------------------------------------
    def new_refresh(self, user_id: str, family: str | None = None) -> RefreshSession:
        session = RefreshSession(
            user_id=user_id,
            token=secrets.token_urlsafe(32),
            expires_at=now() + REFRESH_TTL,
            family=family or str(uuid.uuid4()),
        )
        self.refresh[session.token] = session
        return session

    def rotate_refresh(self, token: str) -> RefreshSession | None:
        """Exchange a live token for a new one; a token seen twice revokes its family."""
        if token in self.consumed:
            self.revoked_families.add(self.consumed[token])
            return None
        session = self.refresh.pop(token, None)
        if session is None or session.family in self.revoked_families:
            return None
        if session.expires_at < now():
            return None
        self.consumed[token] = session.family
        return self.new_refresh(session.user_id, session.family)

    def revoke(self, token: str) -> None:
        session = self.refresh.pop(token, None)
        if session is not None:
            self.revoked_families.add(session.family)

    def revoke_all(self, user_id: str) -> None:
        for token, session in list(self.refresh.items()):
            if session.user_id == user_id:
                self.revoked_families.add(session.family)
                del self.refresh[token]


def seed_demo(store: Store) -> Store:
    """The accounts the Delivery Plus apps and their seed scripts expect."""
    store.add_user(
        "demo.customer@example.com",
        PASSWORD,
        "Sara Demo",
        "CUSTOMER",
        phone="+20 101 234 5678",
        phone_verified_at=now(),
        address="742 Evergreen Terrace, Apt 3B",
    )
    store.add_user("admin@example.com", PASSWORD, "Platform Admin", "ADMIN")
    store.add_user("demo.driver@example.com", PASSWORD, "Demo Driver", "DRIVER")
    store.add_user("driver@example.com", PASSWORD, "Base Driver", "DRIVER")
    for name in RESTAURANT_NAMES:
        full_name = "Restaurant Owner" if name == "Burger Palace" else f"{name} Owner"
        store.add_user(owner_email(name), PASSWORD, full_name, "RESTAURANT_OWNER")
    from foood.seed import seed_restaurants

    seed_restaurants(store)
    return store
