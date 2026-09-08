"""Password hashing and JWTs (Contract 1).

Carried over from LedgerLite / TaskFlow unchanged in shape: argon2 via passlib, HS256
access tokens with a 15-minute expiry, and a rotating httpOnly refresh cookie with a
30-day expiry. This layer is deliberately NOT redesigned -- the whole point of copying
it is that the new difficulty in this project stays concentrated on RAG.

Note on refresh-token revocation: Contract 2 freezes the schema and it has no refresh
token table, so rotation here means "every refresh issues a fresh token", not
"the previous token is invalidated server-side". A stolen refresh token stays valid
until its 30 days elapse. Adding a token table would be a schema ESCALATION, not an edit.
"""

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import jwt
from passlib.context import CryptContext

from app.config import get_settings
from app.errors import UnauthorizedError, ValidationError

MIN_PASSWORD_LENGTH = 8

TOKEN_TYPE_ACCESS = "access"
TOKEN_TYPE_REFRESH = "refresh"

_pwd_context = CryptContext(schemes=["argon2"], deprecated="auto")


# ---------------------------------------------------------------------------
# Passwords -- never logged, never returned, never echoed in an error message.
# ---------------------------------------------------------------------------


def hash_password(password: str) -> str:
    return _pwd_context.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _pwd_context.verify(password, password_hash)
    except ValueError:
        # A malformed stored hash must read as "wrong password", not as a 500.
        return False


def validate_password_strength(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValidationError(
            "Your password needs to be at least 8 characters long."
        )


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------


def _encode(payload: Dict[str, Any]) -> str:
    settings = get_settings()
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def create_access_token(user_id: uuid.UUID) -> str:
    settings = get_settings()
    now = datetime.now(timezone.utc)
    return _encode(
        {
            "sub": str(user_id),
            "type": TOKEN_TYPE_ACCESS,
            "iat": int(now.timestamp()),
            "exp": int(
                (
                    now + timedelta(minutes=settings.access_token_expire_minutes)
                ).timestamp()
            ),
        }
    )


def create_refresh_token(user_id: uuid.UUID) -> str:
    settings = get_settings()
    now = datetime.now(timezone.utc)
    return _encode(
        {
            "sub": str(user_id),
            "type": TOKEN_TYPE_REFRESH,
            # A fresh jti on every issue is what makes each rotation a distinct token.
            "jti": uuid.uuid4().hex,
            "iat": int(now.timestamp()),
            "exp": int(
                (
                    now + timedelta(days=settings.refresh_token_expire_days)
                ).timestamp()
            ),
        }
    )


def decode_token(token: str, expected_type: str) -> uuid.UUID:
    """Return the subject user id, or raise UnauthorizedError.

    Every failure mode -- bad signature, expired, wrong token type, unparseable
    subject -- produces the same 401 sentence. Which one it was is not the caller's
    business and telling them narrows an attacker's search.
    """
    settings = get_settings()
    try:
        payload = jwt.decode(
            token, settings.jwt_secret, algorithms=[settings.jwt_algorithm]
        )
    except jwt.PyJWTError:
        raise UnauthorizedError()

    if payload.get("type") != expected_type:
        raise UnauthorizedError()

    subject: Optional[str] = payload.get("sub")
    if not subject:
        raise UnauthorizedError()
    try:
        return uuid.UUID(subject)
    except (ValueError, AttributeError, TypeError):
        raise UnauthorizedError()


def display_name_for(email: str) -> str:
    """Fall back to the email local-part (Contract 1). ASSUMED -- display-only."""
    local = email.split("@", 1)[0].strip()
    return local or email
