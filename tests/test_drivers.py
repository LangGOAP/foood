"""Driver profiles (issue #8): vehicle, verification and going online."""

from __future__ import annotations

from tests.support import assert_conforms, login


async def test_seeded_drivers_are_verified_and_offline(client) -> None:
    base = await login(client, "driver@example.com")
    me = await client.get("/api/drivers/me", headers=base)
    body = assert_conforms("GET", "/api/drivers/me", me)
    assert me.status_code == 200
    assert (body["vehicleType"], body["licensePlate"]) == ("Scooter", "DMO-2030")
    assert body["status"] == "OFFLINE" and body["verificationStatus"] == "VERIFIED"
    qa = await login(client, "qa.driver@delivery-plus.test")
    qa_me = (await client.get("/api/drivers/me", headers=qa)).json()
    assert (qa_me["vehicleType"], qa_me["licensePlate"]) == ("Bicycle", "QA-0001")
    application = (await client.get("/api/drivers/me/application", headers=base)).json()
    assert application["status"] == "VERIFIED" and application["missingDocuments"] == []
    assert application["lastReview"]["decision"] == "VERIFIED"


async def test_new_driver_registers_waits_for_review_then_goes_online(client) -> None:
    demo = await login(client, "demo.driver@example.com")
    admin = await login(client, "admin@example.com")
    assert (await client.get("/api/drivers/me", headers=demo)).status_code == 404
    registered = await client.post(
        "/api/drivers/register",
        json={"vehicleType": "Scooter", "licensePlate": "QA-NEW"},
        headers=demo,
    )
    assert registered.status_code == 201, registered.text
    driver = registered.json()
    assert driver["status"] == "OFFLINE" and driver["verificationStatus"] == "PENDING"
    twice = await client.post(
        "/api/drivers/register",
        json={"vehicleType": "Scooter", "licensePlate": "QA-NEW"},
        headers=demo,
    )
    assert twice.status_code == 409
    refused = await client.post("/api/drivers/me/online", json={}, headers=demo)
    assert refused.status_code == 403 and refused.json()["error"] == "DriverNotVerified"
    assert refused.json()["details"] == {"verificationStatus": "PENDING"}
    application = (await client.get("/api/drivers/me/application", headers=demo)).json()
    assert application["status"] == "DRAFT" and len(application["missingDocuments"]) == 5
    assert application["canSubmit"] is False
    verified = await client.patch(
        f"/api/drivers/{driver['id']}/verification",
        json={"verificationStatus": "VERIFIED"},
        headers=admin,
    )
    assert verified.status_code == 200 and verified.json()["verifiedAt"]
    online = await client.post("/api/drivers/me/online", json={}, headers=demo)
    assert online.status_code == 201 and online.json()["status"] == "AVAILABLE"
    offline = await client.post("/api/drivers/me/offline", json={}, headers=demo)
    assert offline.status_code == 201 and offline.json()["status"] == "OFFLINE"
    explicit = await client.post(
        "/api/drivers/me/status", json={"status": "AVAILABLE"}, headers=demo
    )
    assert explicit.status_code == 201 and explicit.json()["status"] == "AVAILABLE"
    bad = await client.post("/api/drivers/me/status", json={"status": "BUSY"}, headers=demo)
    assert bad.status_code == 400


async def test_rejection_needs_a_note_and_knocks_the_driver_offline(client) -> None:
    base = await login(client, "driver@example.com")
    admin = await login(client, "admin@example.com")
    me = (await client.get("/api/drivers/me", headers=base)).json()
    await client.post("/api/drivers/me/online", json={}, headers=base)
    no_note = await client.patch(
        f"/api/drivers/{me['id']}/verification",
        json={"verificationStatus": "REJECTED"},
        headers=admin,
    )
    assert no_note.status_code == 400
    rejected = await client.patch(
        f"/api/drivers/{me['id']}/verification",
        json={"verificationStatus": "REJECTED", "note": "Plate does not match the licence"},
        headers=admin,
    )
    assert rejected.status_code == 200
    assert (
        rejected.json()["status"] == "OFFLINE"
        and rejected.json()["verificationStatus"] == "REJECTED"
    )
    application = (await client.get("/api/drivers/me/application", headers=base)).json()
    assert application["status"] == "REJECTED"
    assert application["verificationNote"] == "Plate does not match the licence"
    again = await client.post("/api/drivers/me/online", json={}, headers=base)
    assert again.json()["details"] == {"verificationStatus": "REJECTED"}


async def test_vehicle_changes_only_while_offline(client) -> None:
    base = await login(client, "driver@example.com")
    await client.post("/api/drivers/me/online", json={}, headers=base)
    blocked = await client.patch(
        "/api/drivers/me/vehicle",
        json={"vehicleType": "Car", "licensePlate": "DMO-1"},
        headers=base,
    )
    assert blocked.status_code == 409
    await client.post("/api/drivers/me/offline", json={}, headers=base)
    changed = await client.patch(
        "/api/drivers/me/vehicle",
        json={"vehicleType": "Car", "licensePlate": "DMO-1"},
        headers=base,
    )
    assert changed.status_code == 200 and changed.json()["licensePlate"] == "DMO-1"


async def test_who_may_see_a_driver(client) -> None:
    base = await login(client, "driver@example.com")
    other = await login(client, "qa.driver@delivery-plus.test")
    admin = await login(client, "admin@example.com")
    customer = await login(client, "demo.customer@example.com")
    me = (await client.get("/api/drivers/me", headers=base)).json()
    assert (await client.get(f"/api/drivers/{me['id']}", headers=base)).status_code == 200
    assert (await client.get(f"/api/drivers/{me['id']}", headers=admin)).status_code == 200
    assert (await client.get(f"/api/drivers/{me['id']}", headers=other)).status_code == 403
    assert (await client.get("/api/drivers/nope", headers=admin)).status_code == 404
    not_a_driver = await client.post(
        "/api/drivers/register", json={"vehicleType": "Car", "licensePlate": "X"}, headers=customer
    )
    assert not_a_driver.status_code == 403


def test_dispatch_queue_is_longest_idle_first() -> None:
    from datetime import timedelta

    from foood.drivers import Driver, available_drivers
    from foood.store import Store

    store = Store()
    first = Driver(user_id="a", vehicle_type="Bike", license_plate="1")
    second = Driver(user_id="b", vehicle_type="Bike", license_plate="2")
    pending = Driver(user_id="c", vehicle_type="Bike", license_plate="3")
    for driver in (first, second, pending):
        store.drivers[driver.id] = driver
    first.verify("VERIFIED", None)
    second.verify("VERIFIED", None)
    second.set_status("AVAILABLE")
    first.set_status("AVAILABLE")
    pending.set_status("AVAILABLE")  # not verified: never dispatched
    # two drivers can come online within the clock's resolution; set who waited longer
    second.available_since = first.available_since - timedelta(seconds=30)
    assert [d.user_id for d in available_drivers(store)] == ["b", "a"]
    second.set_status("BUSY")
    assert [d.user_id for d in available_drivers(store)] == ["a"]
