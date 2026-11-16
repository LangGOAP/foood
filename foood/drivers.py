"""Driver profiles: vehicle, verification and the online / offline switch.

Registration creates a PENDING profile; an admin verifies it (the apps show
"waiting for review" until then). Only a VERIFIED driver can go online, and an
online driver who is not on a delivery is what dispatch hands orders to.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from foood.auth import CurrentUser, StoreDep
from foood.errors import ApiError
from foood.models import new_id
from foood.store import Store, User, iso, now

router = APIRouter(prefix="/api", tags=["drivers"])

DOCUMENT_TYPES = [
    "national-id-front",
    "national-id-back",
    "driving-licence",
    "vehicle-licence",
    "selfie",
]


@dataclass
class Driver:
    user_id: str
    vehicle_type: str
    license_plate: str
    status: str = "OFFLINE"  # OFFLINE | AVAILABLE | BUSY | SUSPENDED
    verification_status: str = "PENDING"  # PENDING | VERIFIED | REJECTED
    verification_note: str | None = None
    verified_at: datetime | None = None
    reviewed_at: datetime | None = None
    available_since: datetime | None = None
    id: str = field(default_factory=new_id)
    created_at: datetime = field(default_factory=now)
    updated_at: datetime = field(default_factory=now)

    def verify(self, decision: str, note: str | None) -> None:
        self.verification_status = decision
        self.verification_note = note
        self.reviewed_at = now()
        self.verified_at = self.reviewed_at if decision == "VERIFIED" else None
        self.updated_at = self.reviewed_at

    def set_status(self, status: str) -> None:
        self.status = status
        self.available_since = now() if status == "AVAILABLE" else None
        self.updated_at = now()

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "userId": self.user_id,
            "vehicleType": self.vehicle_type,
            "licensePlate": self.license_plate,
            "status": self.status,
            "verificationStatus": self.verification_status,
            "verificationNote": self.verification_note,
            "verifiedAt": iso(self.verified_at) if self.verified_at else None,
            "createdAt": iso(self.created_at),
            "updatedAt": iso(self.updated_at),
        }

    def application_json(self) -> dict[str, Any]:
        """The onboarding view; a seeded or admin-verified driver has nothing left to do."""
        verified = self.verification_status == "VERIFIED"
        status = {"PENDING": "DRAFT", "VERIFIED": "VERIFIED", "REJECTED": "REJECTED"}[
            self.verification_status
        ]
        review = (
            {
                "decision": self.verification_status,
                "note": self.verification_note,
                "reviewedAt": iso(self.reviewed_at),
            }
            if self.reviewed_at
            else None
        )
        return {
            "driverId": self.id,
            "status": status,
            "submittedAt": iso(self.reviewed_at) if self.reviewed_at else None,
            "verificationNote": self.verification_note,
            "lastReview": review,
            "nationalIdLast4": None,
            "drivingLicenceExpiresOn": None,
            "vehicleLicenceExpiresOn": None,
            "documents": [],
            "missingDocuments": [] if verified else list(DOCUMENT_TYPES),
            "canSubmit": False,
            "expiringLicences": [],
        }

    def card(self, user: User | None) -> dict[str, Any]:
        """What a customer or restaurant may see of the driver."""
        return {
            "displayName": user.full_name.split()[0] if user else "Driver",
            "avatarUrl": user.avatar_url if user else None,
            "vehicleType": self.vehicle_type,
            "licensePlate": self.license_plate,
        }


def driver_of(store: Store, user_id: str) -> Driver | None:
    return next((d for d in store.drivers.values() if d.user_id == user_id), None)


def my_driver(store: Store, user: User) -> Driver:
    driver = driver_of(store, user.id)
    if driver is None:
        raise ApiError(404, "No driver profile yet")
    return driver


def available_drivers(store: Store) -> list[Driver]:
    """Verified, online, idle drivers, longest idle first (the dispatch queue)."""
    idle = [
        d
        for d in store.drivers.values()
        if d.status == "AVAILABLE" and d.verification_status == "VERIFIED"
    ]
    return sorted(idle, key=lambda d: (d.available_since or d.created_at, d.id))


def go_online(store: Store, driver: Driver) -> Driver:
    if driver.verification_status != "VERIFIED":
        raise ApiError(
            403,
            "Your application has not been approved yet",
            error="DriverNotVerified",
            details={"verificationStatus": driver.verification_status},
        )
    if driver.status == "SUSPENDED":
        raise ApiError(403, "Your account is suspended", error="AccountSuspended")
    if driver.status != "BUSY":
        driver.set_status("AVAILABLE")
        store.publish("driver.available", driver=driver)
    return driver


def go_offline(store: Store, driver: Driver) -> Driver:
    if driver.status == "BUSY":
        raise ApiError(409, "Finish your current delivery first", error="OnDelivery")
    if driver.status != "SUSPENDED":
        driver.set_status("OFFLINE")
    return driver


# --- routes -------------------------------------------------------------------------


class Vehicle(BaseModel):
    vehicleType: str = Field(min_length=1, max_length=50)
    licensePlate: str = Field(min_length=1, max_length=20)


class StatusRequest(BaseModel):
    status: str


class VerificationRequest(BaseModel):
    verificationStatus: str
    note: str | None = Field(default=None, max_length=300)


@router.post("/drivers/register", status_code=201)
async def register(body: Vehicle, user: CurrentUser, store: StoreDep) -> dict[str, Any]:
    if user.role != "DRIVER":
        raise ApiError(403, "Only driver accounts can register a vehicle")
    if driver_of(store, user.id) is not None:
        raise ApiError(409, "You already have a driver profile")
    driver = Driver(user_id=user.id, vehicle_type=body.vehicleType, license_plate=body.licensePlate)
    store.drivers[driver.id] = driver
    return driver.to_json()


@router.get("/drivers/me")
async def me(user: CurrentUser, store: StoreDep) -> dict[str, Any]:
    return my_driver(store, user).to_json()


@router.patch("/drivers/me/vehicle")
async def change_vehicle(body: Vehicle, user: CurrentUser, store: StoreDep) -> dict[str, Any]:
    driver = my_driver(store, user)
    if driver.status != "OFFLINE":
        raise ApiError(409, "Go offline before changing your vehicle")
    driver.vehicle_type = body.vehicleType
    driver.license_plate = body.licensePlate
    driver.updated_at = now()
    return driver.to_json()


@router.post("/drivers/me/online", status_code=201)
async def online(user: CurrentUser, store: StoreDep) -> dict[str, Any]:
    return go_online(store, my_driver(store, user)).to_json()


@router.post("/drivers/me/offline", status_code=201)
async def offline(user: CurrentUser, store: StoreDep) -> dict[str, Any]:
    return go_offline(store, my_driver(store, user)).to_json()


@router.post("/drivers/me/status", status_code=201)
async def set_status(body: StatusRequest, user: CurrentUser, store: StoreDep) -> dict[str, Any]:
    driver = my_driver(store, user)
    if body.status == "AVAILABLE":
        return go_online(store, driver).to_json()
    if body.status == "OFFLINE":
        return go_offline(store, driver).to_json()
    raise ApiError(400, "status must be AVAILABLE or OFFLINE")


@router.get("/drivers/me/application")
async def my_application(user: CurrentUser, store: StoreDep) -> dict[str, Any]:
    return my_driver(store, user).application_json()


@router.get("/drivers/{id}")
async def get_driver(id: str, user: CurrentUser, store: StoreDep) -> dict[str, Any]:
    driver = store.drivers.get(id)
    if driver is None:
        raise ApiError(404, "Driver not found")
    if user.role != "ADMIN" and driver.user_id != user.id:
        raise ApiError(403, "You do not have access to this driver")
    return driver.to_json()


@router.patch("/drivers/{id}/verification")
async def set_verification(
    id: str, body: VerificationRequest, user: CurrentUser, store: StoreDep
) -> dict[str, Any]:
    if user.role != "ADMIN":
        raise ApiError(403, "Only administrators can verify drivers")
    driver = store.drivers.get(id)
    if driver is None:
        raise ApiError(404, "Driver not found")
    if body.verificationStatus not in {"VERIFIED", "REJECTED"}:
        raise ApiError(400, "verificationStatus must be VERIFIED or REJECTED")
    if body.verificationStatus == "REJECTED" and not body.note:
        raise ApiError(400, "A note is required to reject")
    driver.verify(body.verificationStatus, body.note)
    if body.verificationStatus == "REJECTED" and driver.status == "AVAILABLE":
        driver.set_status("OFFLINE")
    return driver.to_json()
