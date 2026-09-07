"""Shared FastAPI dependencies.

The client NEVER sends a user id. Ownership is derived from the access token, always,
and every ownership check in this codebase starts from `CurrentUser`.
"""

from typing import Optional

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from typing_extensions import Annotated

from app.answer.claude_client import AnswerClient
from app.database import get_db
from app.embeddings.base import EmbeddingClient
from app.errors import UnauthorizedError
from app.models import User
from app.security import TOKEN_TYPE_ACCESS, decode_token

DbDep = Annotated[AsyncSession, Depends(get_db)]


def _bearer_token(request: Request) -> str:
    header: Optional[str] = request.headers.get("authorization")
    if not header:
        raise UnauthorizedError("You need to sign in to do that.")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise UnauthorizedError("You need to sign in to do that.")
    return token.strip()


async def get_current_user(request: Request, db: DbDep) -> User:
    user_id = decode_token(_bearer_token(request), TOKEN_TYPE_ACCESS)
    user = await db.get(User, user_id)
    if user is None:
        # A valid signature for a user that no longer exists is still just "signed out".
        raise UnauthorizedError()
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def get_embedding_client(request: Request) -> EmbeddingClient:
    """The query-side Voyage client, built once at startup and shared.

    Tests replace `app.state.embedding_client` with the deterministic fake; no test in
    this suite may reach the live Voyage API (Contract 4).
    """
    return request.app.state.embedding_client


def get_answer_client(request: Request) -> AnswerClient:
    return request.app.state.answer_client


EmbeddingDep = Annotated[EmbeddingClient, Depends(get_embedding_client)]
AnswerDep = Annotated[AnswerClient, Depends(get_answer_client)]
