"""Configuration for the Queryll API.

Every value that Contract 4 or Contract 7 pins is read from the env var name the
contract specifies, with the contract's default. Instance 1 reads the SAME names with
the SAME defaults for the embedding block; that shared naming is the first of the three
layers defusing the silent-divergence trap (CLAUDE.md, Decomposition rationale).
"""

from functools import lru_cache
from typing import List, Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---- Database -------------------------------------------------------------
    # Host port is 5433, not 5432 — Snipp holds 5432 on the dev machine.
    database_url: str = (
        "postgresql+asyncpg://queryll:queryll@localhost:5433/queryll"
    )
    db_echo: bool = False

    # ---- Auth (Contract 1, carried over from LedgerLite / TaskFlow) ------------
    jwt_secret: str = "dev-only-insecure-secret-change-me"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 15
    refresh_token_expire_days: int = 30
    refresh_cookie_name: str = "queryll_refresh"
    # Secure=True + SameSite=None is required for the Vercel -> Render cross-site
    # deployment. Over plain http://localhost a Secure cookie is not stored, so local
    # development sets COOKIE_SECURE=false and COOKIE_SAMESITE=lax.
    cookie_secure: bool = True
    cookie_samesite: str = "none"
    cookie_domain: Optional[str] = None

    # ---- CORS (Contract 9) ----------------------------------------------------
    # Comma-separated. Must include the Vercel production AND preview domains.
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    # ---- Embeddings (Contract 4) — frozen values ------------------------------
    embedding_provider_url: str = "https://api.voyageai.com/v1/embeddings"
    embedding_model: str = "voyage-4"
    embedding_dim: int = 1024
    voyage_api_key: Optional[str] = None
    voyage_timeout_seconds: float = 20.0
    voyage_max_retries: int = 4  # backoff 1s, 2s, 4s, 8s, then surface

    # ---- Retrieval (Contract 7 §4) — ASSUMED starting values, tunable ---------
    retrieval_top_k: int = 8
    retrieval_min_similarity: float = 0.35
    retrieval_per_document_cap: int = 4
    # HNSW is an approximate index and pgvector post-filters, so a query scoped to one
    # user can come back short. Over-fetch candidates, then cap and floor them.
    retrieval_candidate_multiplier: int = 6
    hnsw_ef_search: int = 100

    # ---- Answering (Contract 7) ----------------------------------------------
    anthropic_api_key: Optional[str] = None
    answer_model: str = "claude-sonnet-5"
    answer_max_tokens: int = 4096
    # "disabled" (default) or "adaptive". Sonnet 5 accepts both; grounded extraction
    # over eight passages does not need reasoning, and time-to-first-token is what the
    # product is judged on. Raise it under measurement, not on instinct.
    answer_thinking: str = "disabled"
    # Omitted from the request when None. `output_config.effort` is GA on Sonnet 5.
    answer_effort: Optional[str] = None
    history_max_messages: int = 6  # ASSUMED — tunable
    sse_heartbeat_seconds: float = 15.0

    # ---- Uploads (Contract 5 §1 / Contract 6 §1) ------------------------------
    max_upload_bytes: int = 20 * 1024 * 1024
    upload_read_chunk_bytes: int = 64 * 1024

    # ---- Pagination -----------------------------------------------------------
    default_page_limit: int = 50
    max_page_limit: int = 200

    @property
    def cors_origin_list(self) -> List[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
