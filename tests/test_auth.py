"""Auth as a thin JWT issuer with the Delivery Plus demo accounts (issue #3)."""

from __future__ import annotations

import jwt
import pytest

from tests.support import assert_conforms

PASSWORD = "password123"


async def _login(client, email: str, password: str = PASSWORD, app_id: str = "customer"):
    response = await client.post(
        "/api/auth/login", json={"email": email, "password": password, "appId": app_id}
    )
    return response


@pytest.mark.parametrize(
    ("email", "role"),
    [
        ("demo.customer@example.com", "CUSTOMER"),
        ("demo.driver@example.com", "DRIVER"),
        ("driver@example.com", "DRIVER"),
        ("owner@example.com", "RESTAURANT_OWNER"),
        ("pizza.napoli.owner@example.com", "RESTAURANT_OWNER"),
        ("qa.restaurant@delivery-plus.test", "RESTAURANT_OWNER"),
    ],
)
async def test_demo_accounts_log_in(client, email: str, role: str) -> None:
    response = await _login(client, email)
    body = assert_conforms("POST", "/api/auth/login", response)
    assert response.status_code == 200
    assert body["email"] == email and body["role"] == role
    claims = jwt.decode(body["accessToken"], options={"verify_signature": False})
    assert claims["sub"] == body["userId"]
    assert claims["email"] == email and claims["role"] == role


async def test_wrong_password_is_401_with_the_error_shape(client) -> None:
    response = await _login(client, "demo.customer@example.com", "nope")
    assert response.status_code == 401
    body = response.json()
    assert body["statusCode"] == 401 and body["error"] == "Unauthorized" and body["message"]


async def test_register_returns_a_session(client) -> None:
    response = await client.post(
        "/api/auth/register",
        json={"email": "new@example.com", "password": "secret12", "fullName": "New Person"},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["role"] == "CUSTOMER" and body["accessToken"] and body["refreshToken"]
    again = await client.post(
        "/api/auth/register",
        json={"email": "new@example.com", "password": "secret12", "fullName": "New Person"},
    )
    assert again.status_code == 400
    assert again.json()["error"] == "Bad Request"


async def test_refresh_rotates_and_reuse_ends_the_session(client) -> None:
    first = (await _login(client, "demo.customer@example.com")).json()
    rotated = await client.post("/api/auth/refresh", json={"refreshToken": first["refreshToken"]})
    body = assert_conforms("POST", "/api/auth/refresh", rotated)
    assert rotated.status_code == 200
    assert body["refreshToken"] != first["refreshToken"]
    reuse = await client.post("/api/auth/refresh", json={"refreshToken": first["refreshToken"]})
    assert reuse.status_code == 401
    after = await client.post("/api/auth/refresh", json={"refreshToken": body["refreshToken"]})
    assert after.status_code == 401, "reuse of an old token ends the whole session"


async def test_logout_is_204_even_for_unknown_tokens(client) -> None:
    first = (await _login(client, "demo.customer@example.com")).json()
    assert (
        await client.post("/api/auth/logout", json={"refreshToken": first["refreshToken"]})
    ).status_code == 204
    assert (await client.post("/api/auth/logout", json={"refreshToken": "nope"})).status_code == 204
    assert (
        await client.post("/api/auth/refresh", json={"refreshToken": first["refreshToken"]})
    ).status_code == 401


async def test_me_endpoints(client) -> None:
    session = (await _login(client, "demo.customer@example.com")).json()
    headers = {"Authorization": f"Bearer {session['accessToken']}"}
    me = await client.get("/api/auth/me", headers=headers)
    assert me.status_code == 200
    assert me.json() == {
        "sub": session["userId"],
        "email": "demo.customer@example.com",
        "role": "CUSTOMER",
    }
    profile = await client.get("/api/users/me", headers=headers)
    assert profile.status_code == 200
    body = profile.json()
    assert body["fullName"] == "Sara Demo" and body["address"].startswith("742 Evergreen")
    assert body["phoneVerifiedAt"] is None
    updated = await client.patch("/api/users/me", json={"address": "1 New St"}, headers=headers)
    assert updated.status_code == 200 and updated.json()["address"] == "1 New St"
    assert (await client.get("/api/users/me")).status_code == 401


async def test_phone_verification_with_the_demo_code(client) -> None:
    session = (await _login(client, "demo.customer@example.com")).json()
    headers = {"Authorization": f"Bearer {session['accessToken']}"}
    started = await client.post("/api/users/me/phone/verification", json={}, headers=headers)
    assert started.status_code == 202
    assert set(started.json()) >= {"phone", "expiresAt", "resendAvailableAt"}
    wrong = await client.post(
        "/api/users/me/phone/verify", json={"code": "000000"}, headers=headers
    )
    assert wrong.status_code == 400
    right = await client.post(
        "/api/users/me/phone/verify", json={"code": "123456"}, headers=headers
    )
    assert right.status_code == 200 and right.json()["phoneVerifiedAt"]


async def test_password_change_and_reset_flows(client) -> None:
    session = (await _login(client, "demo.customer@example.com")).json()
    headers = {"Authorization": f"Bearer {session['accessToken']}"}
    changed = await client.post(
        "/api/auth/password/change",
        json={"currentPassword": PASSWORD, "newPassword": "another12"},
        headers=headers,
    )
    assert changed.status_code == 200
    assert (await _login(client, "demo.customer@example.com", PASSWORD)).status_code == 401
    forgot = await client.post(
        "/api/auth/password/forgot", json={"email": "demo.customer@example.com"}
    )
    assert forgot.status_code == 202
    reset = await client.post(
        "/api/auth/password/reset",
        json={"email": "demo.customer@example.com", "code": "123456", "newPassword": PASSWORD},
    )
    assert reset.status_code == 200
    assert (await _login(client, "demo.customer@example.com", PASSWORD)).status_code == 200
