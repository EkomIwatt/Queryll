"""Authentication (Contract 1) -- carried over from LedgerLite / TaskFlow, unchanged.

Deliberately not redesigned and deliberately not modernized. The whole point of copying
this layer verbatim is that the new difficulty in Queryll stays concentrated on RAG.

The access token lives in the client's memory and is sent as `Authorization: Bearer`.
The refresh token lives only in an httpOnly cookie, is rotated on every refresh, and is
never readable by JavaScript -- so an XSS bug cannot walk off with a 30-day credential.
"""

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Request, Response, status
from sqlalchemy import select

from app.config import get_settings
from app.deps import CurrentUser, DbDep
from app.errors import ConflictError, UnauthorizedError
from app.models import User
from app.schemas import AuthResponse, LoginRequest, SignupRequest, UserOut
from app.security import (
    TOKEN_TYPE_REFRESH,
    create_access_token,
    create_refresh_token,
    decode_token,
    display_name_for,
    hash_password,
    validate_password_strength,
    verify_password,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _set_refresh_cookie(response: Response, user_id: uuid.UUID) -> None:
    settings = get_settings()
    response.set_cookie(
        key=settings.refresh_cookie_name,
        value=create_refresh_token(user_id),
        max_age=settings.refresh_token_expire_days * 24 * 60 * 60,
        httponly=True,
        secure=settings.cookie_secure,
        samesite=settings.cookie_samesite,
        domain=settings.cookie_domain,
        path="/",
    )


def _clear_refresh_cookie(response: Response) -> None:
    settings = get_settings()
    response.delete_cookie(
        key=settings.refresh_cookie_name,
        httponly=True,
        secure=settings.cookie_secure,
        samesite=settings.cookie_samesite,
        domain=settings.cookie_domain,
        path="/",
    )


def _auth_payload(user: User) -> AuthResponse:
    return AuthResponse(
        access_token=create_access_token(user.id), user=UserOut.model_validate(user)
    )


@router.post("/signup", status_code=status.HTTP_201_CREATED, response_model=AuthResponse)
async def signup(payload: SignupRequest, response: Response, db: DbDep) -> AuthResponse:
    validate_password_strength(payload.password)
    email = payload.email.strip().lower()

    existing = (
        await db.execute(select(User.id).where(User.email == email))
    ).scalar_one_or_none()
    if existing is not None:
        raise ConflictError("An account with that email address already exists.")

    user = User(
        id=uuid.uuid4(),
        email=email,
        password_hash=hash_password(payload.password),
        display_name=display_name_for(email),
        created_at=datetime.now(timezone.utc),
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)

    _set_refresh_cookie(response, user.id)
    return _auth_payload(user)


@router.post("/login", response_model=AuthResponse)
async def login(payload: LoginRequest, response: Response, db: DbDep) -> AuthResponse:
    email = payload.email.strip().lower()
    user = (
        await db.execute(select(User).where(User.email == email))
    ).scalar_one_or_none()

    # One sentence for both "no such account" and "wrong password". Which of the two it
    # was is not the caller's business, and saying turns login into an account oracle.
    if user is None or not verify_password(payload.password, user.password_hash):
        raise UnauthorizedError("That email address and password do not match.")

    _set_refresh_cookie(response, user.id)
    return _auth_payload(user)


@router.post("/refresh", response_model=AuthResponse)
async def refresh(request: Request, response: Response, db: DbDep) -> AuthResponse:
    settings = get_settings()
    token = request.cookies.get(settings.refresh_cookie_name)
    if not token:
        raise UnauthorizedError("Your session has expired. Please sign in again.")

    user_id = decode_token(token, TOKEN_TYPE_REFRESH)
    user = await db.get(User, user_id)
    if user is None:
        raise UnauthorizedError()

    # Rotated on every refresh: a new token with a new jti replaces the one just used.
    _set_refresh_cookie(response, user.id)
    return _auth_payload(user)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(response: Response) -> None:
    # The injected Response carries the Set-Cookie header onto the 204; returning a
    # freshly constructed Response here would silently drop it.
    _clear_refresh_cookie(response)


@router.get("/me", response_model=UserOut)
async def me(user: CurrentUser) -> UserOut:
    return UserOut.model_validate(user)
