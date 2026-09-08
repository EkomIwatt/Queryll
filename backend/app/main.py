"""The FastAPI application.

Two things in here are load-bearing beyond the obvious wiring:

**There is no compression middleware, deliberately.** GZip middleware buffers in order
to compress, which coalesces SSE frames and turns a token stream into one burst at the
end -- and it does that only once there is a real deploy in front of it, so it passes
every local test. Contract 7 §1 requires the ask route to be excluded from compression;
the simplest way to guarantee that is to have none to exclude it from. If compression is
ever added, it MUST skip `text/event-stream`.

**Every non-2xx response goes out as `{"error": "<sentence>"}`** (Contract 9), including
the ones FastAPI and Starlette raise on their own -- a 422 from request validation, a 404
from an unmatched route, a 405 from a wrong method. Instance 3 renders `error` directly,
so a response that escaped the envelope would surface as a blank error box.
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.answer.claude_client import AnthropicAnswerClient
from app.config import get_settings
from app.database import dispose_engine
from app.embeddings.voyage import VoyageEmbeddingClient
from app.errors import AppError
from app.routers import auth, conversations, documents
from app.schemas import error_envelope

logger = logging.getLogger(__name__)

# Sentences used when Starlette raises before any of our own code does.
_STATUS_SENTENCES = {
    401: "You need to sign in to do that.",
    403: "You do not have access to that.",
    404: "That was not found.",
    405: "That request is not supported here.",
    413: "That file is larger than the 20 MB limit.",
    415: "Queryll can read PDF, plain text and Markdown files only.",
    422: "The request could not be processed as sent.",
    429: "Too many requests. Try again in a moment.",
}
_FALLBACK_SENTENCE = "Something went wrong. Please try again."


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.embedding_client = VoyageEmbeddingClient()
    app.state.answer_client = AnthropicAnswerClient()
    if not settings.voyage_api_key:
        logger.warning(
            "VOYAGE_API_KEY is not set. Retrieval will return 503 until it is."
        )
    if not settings.anthropic_api_key:
        logger.warning(
            "ANTHROPIC_API_KEY is not set. Questions that retrieve sources will return "
            "502 until it is; the insufficient-context path still works."
        )
    try:
        yield
    finally:
        await app.state.embedding_client.aclose()
        await app.state.answer_client.aclose()
        await dispose_engine()


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="Queryll API",
        description=(
            "Retrieval, grounded answers and auth for Queryll. "
            "Ingestion is a separate process; the database is the only medium between them."
        ),
        version="1.0.0",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Accept"],
    )

    _register_exception_handlers(app)

    app.include_router(auth.router)
    app.include_router(documents.router)
    app.include_router(conversations.router)

    @app.get("/api/health", tags=["ops"])
    async def health() -> dict:
        return {"status": "ok"}

    return app


def _register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=exc.envelope())

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # Pydantic's error list names internal field paths and echoes submitted values.
        # Neither belongs in a response body, so it is collapsed to one sentence.
        return JSONResponse(
            status_code=422, content=error_envelope(_STATUS_SENTENCES[422])
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        detail = exc.detail if isinstance(exc.detail, str) else None
        sentence = _STATUS_SENTENCES.get(exc.status_code) or detail or _FALLBACK_SENTENCE
        return JSONResponse(
            status_code=exc.status_code,
            content=error_envelope(sentence),
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        # Logged with a traceback for us; one sentence for the caller. No stack traces,
        # no SQL, no provider payloads, no key fragments -- ever.
        logger.exception("Unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(status_code=500, content=error_envelope(_FALLBACK_SENTENCE))


app = create_app()
