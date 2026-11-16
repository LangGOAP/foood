"""Auth: a thin JWT issuer with rotating refresh tokens, and the user profile."""

from __future__ import annotations

import os
from typing import Annotated, Any

import jwt
from fastapi import APIRouter, Depends, Header, Request, Response
from pydantic import BaseModel, EmailStr, Field

from foood.errors import ApiError
from foood.store import ACCESS_TTL, DEMO_CODE, Store, User, iso, now

# A demo platform: the signing key is public. FOOOD_JWT_SECRET overrides it.
SECRET = os.environ.get("FOOOD_JWT_SECRET", "foood-dev-secret-not-for-production-use-0001")
ALGORITHM = "HS256"

router = APIRouter(prefix="/api", tags=["auth"])


def get_store(request: Request) -> Store:
    return request.app.state.store  # type: ignore[no-any-return]


def issue_access_token(user: User) -> tuple[str, str]:
    expires = now() + ACCESS_TTL
    token = jwt.encode(
        {"sub": user.id, "email": user.email, "role": user.role, "exp": expires},
        SECRET,
        algorithm=ALGORITHM,
    )
    return token, iso(expires)


def session_response(store: Store, user: User, family: str | None = None) -> dict[str, Any]:
    access, access_expires = issue_access_token(user)
    refresh = store.new_refresh(user.id, family)
    return {
        "accessToken": access,
        "accessTokenExpiresAt": access_expires,
        "refreshToken": refresh.token,
        "refreshTokenExpiresAt": iso(refresh.expires_at),
        "userId": user.id,
        "email": user.email,
        "role": user.role,
    }


def current_user(
    store: Annotated[Store, Depends(get_store)],
    authorization: Annotated[str | None, Header()] = None,
) -> User:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise ApiError(401, "Missing or invalid JWT")
    try:
        claims = jwt.decode(authorization.split(" ", 1)[1], SECRET, algorithms=[ALGORITHM])
    except jwt.PyJWTError as exc:
        raise ApiError(401, "Missing or invalid JWT") from exc
    user = store.users.get(str(claims.get("sub")))
    if user is None:
        raise ApiError(401, "Missing or invalid JWT")
    return user


CurrentUser = Annotated[User, Depends(current_user)]
StoreDep = Annotated[Store, Depends(get_store)]


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8)
    fullName: str = Field(min_length=1)
    phone: str | None = None
    role: str = "CUSTOMER"
    appId: str | None = None


class LoginRequest(BaseModel):
    email: EmailStr
    password: str
    appId: str | None = None


class RefreshRequest(BaseModel):
    refreshToken: str


class ChangePasswordRequest(BaseModel):
    currentPassword: str
    newPassword: str = Field(min_length=8)


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    email: EmailStr
    code: str
    newPassword: str = Field(min_length=8)


class VerifyEmailRequest(BaseModel):
    email: EmailStr
    token: str


class ResendVerificationRequest(BaseModel):
    email: EmailStr


class UpdateProfileRequest(BaseModel):
    fullName: str | None = None
    phone: str | None = None
    address: str | None = None


class VerifyPhoneRequest(BaseModel):
    code: str


@router.post("/auth/register", status_code=201)
async def register(body: RegisterRequest, store: StoreDep) -> dict[str, Any]:
    if body.role not in {"CUSTOMER", "RESTAURANT_OWNER", "DRIVER"}:
        raise ApiError(400, "Unknown role")
    try:
        user = store.add_user(body.email, body.password, body.fullName, body.role, phone=body.phone)
    except ValueError as exc:
        raise ApiError(400, "An account with this email already exists") from exc
    return session_response(store, user)


@router.post("/auth/login")
async def login(body: LoginRequest, store: StoreDep) -> dict[str, Any]:
    user = store.user_by_email(body.email)
    if user is None or user.password != body.password:
        raise ApiError(401, "Invalid email or password")
    return session_response(store, user)


@router.post("/auth/refresh")
async def refresh(body: RefreshRequest, store: StoreDep) -> dict[str, Any]:
    rotated = store.rotate_refresh(body.refreshToken)
    if rotated is None:
        store.revoke(body.refreshToken)
        raise ApiError(401, "Refresh token is invalid, expired or already used")
    user = store.users[rotated.user_id]
    # ``rotate_refresh`` already minted the new token; hand it out with a fresh access token.
    access, access_expires = issue_access_token(user)
    return {
        "accessToken": access,
        "accessTokenExpiresAt": access_expires,
        "refreshToken": rotated.token,
        "refreshTokenExpiresAt": iso(rotated.expires_at),
        "userId": user.id,
        "email": user.email,
        "role": user.role,
    }


@router.post("/auth/logout", status_code=204)
async def logout(body: RefreshRequest, store: StoreDep) -> Response:
    store.revoke(body.refreshToken)
    return Response(status_code=204)


@router.post("/auth/logout-all", status_code=204)
async def logout_all(user: CurrentUser, store: StoreDep) -> Response:
    store.revoke_all(user.id)
    return Response(status_code=204)


@router.get("/auth/me")
async def auth_me(user: CurrentUser) -> dict[str, str]:
    return {"sub": user.id, "email": user.email, "role": user.role}


@router.post("/auth/password/change")
async def change_password(
    body: ChangePasswordRequest, user: CurrentUser, store: StoreDep
) -> dict[str, str]:
    if user.password != body.currentPassword:
        raise ApiError(400, "Current password is incorrect")
    user.password = body.newPassword
    store.revoke_all(user.id)
    return {"message": "Password changed"}


@router.post("/auth/password/forgot", status_code=202)
async def forgot_password(body: ForgotPasswordRequest, store: StoreDep) -> dict[str, str]:
    # No SMS provider: the demo code is always 123456, and unknown emails get the same answer.
    return {"message": "If the account exists, a code was sent"}


@router.post("/auth/password/reset")
async def reset_password(body: ResetPasswordRequest, store: StoreDep) -> dict[str, str]:
    user = store.user_by_email(body.email)
    if user is None or body.code != DEMO_CODE:
        raise ApiError(401, "Invalid or expired code")
    user.password = body.newPassword
    store.revoke_all(user.id)
    return {"message": "Password reset"}


@router.post("/auth/verify-email")
async def verify_email(body: VerifyEmailRequest, store: StoreDep) -> dict[str, str]:
    if store.user_by_email(body.email) is None or body.token != DEMO_CODE:
        raise ApiError(401, "Invalid or expired verification code")
    return {"message": "Email verified"}


@router.post("/auth/resend-verification")
async def resend_verification(body: ResendVerificationRequest) -> dict[str, str]:
    return {"message": "Verification code sent"}


@router.get("/users/me")
async def my_profile(user: CurrentUser) -> dict[str, Any]:
    return user.profile()


@router.patch("/users/me")
async def update_profile(body: UpdateProfileRequest, user: CurrentUser) -> dict[str, Any]:
    if body.fullName is not None:
        user.full_name = body.fullName
    if body.phone is not None and body.phone != user.phone:
        user.phone = body.phone
        user.phone_verified_at = None
    if body.address is not None:
        user.address = body.address
    user.updated_at = now()
    return user.profile()


@router.post("/users/me/phone/verification", status_code=202)
async def start_phone_verification(user: CurrentUser) -> dict[str, str]:
    if not user.phone:
        raise ApiError(400, "Add a phone number first")
    moment = now()
    return {
        "phone": user.phone,
        "expiresAt": iso(moment.replace(microsecond=0) + ACCESS_TTL),
        "resendAvailableAt": iso(moment.replace(microsecond=0)),
    }


@router.post("/users/me/phone/verify")
async def verify_phone(body: VerifyPhoneRequest, user: CurrentUser) -> dict[str, Any]:
    if body.code != DEMO_CODE:
        raise ApiError(400, "Invalid or expired code")
    user.phone_verified_at = now()
    user.updated_at = now()
    return user.profile()
