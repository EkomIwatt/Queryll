"""Worker configuration, read from the environment.

The embedding settings here are Contract 4 and are deliberately *not* clever: the same env
var names with the same defaults exist on the API side, written independently. If these two
ever disagree the app does not crash — retrieval quietly returns near-random passages — so
the values are literals, the defaults are literals, and the merge-time cosine probe is what
finally proves the two sides agree.
"""

from __future__ import annotations

import os
import socket
import uuid
from dataclasses import dataclass, field

# --- Contract 4: frozen embedding constants -------------------------------------------
VOYAGE_API_URL = "https://api.voyageai.com/v1/embeddings"
DEFAULT_EMBEDDING_MODEL = "voyage-4"
DEFAULT_EMBEDDING_DIM = 1024
INPUT_TYPE_DOCUMENT = "document"  # the worker never embeds a query; that is Instance 2's half
OUTPUT_DTYPE = "float"
MAX_INPUTS_PER_REQUEST = 128
MAX_TOKENS_PER_INPUT = 32_000
NORM_TOLERANCE = 1e-3

# --- Contract 5: frozen chunking targets ----------------------------------------------
DEFAULT_TARGET_TOKENS = 512
DEFAULT_OVERLAP_TOKENS = 64
DEFAULT_MIN_TOKENS = 32
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_PAGES = 500

SUPPORTED_MIME_TYPES = frozenset(
    {"application/pdf", "text/plain", "text/markdown"}
)

# --- Contract 3: job protocol ----------------------------------------------------------
MAX_ATTEMPTS = 3
RECLAIM_AFTER = "15 minutes"  # mirrored literally in the Contract 3 §3 claim statement


class ConfigError(RuntimeError):
    """The process is misconfigured and cannot start."""


def _env_str(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None or value == "":
        raise ConfigError(f"{name} is not set")
    return value


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:  # pragma: no cover - trivial
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:  # pragma: no cover - trivial
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc


def _default_worker_id() -> str:
    """A human-readable id for `ingestion_jobs.locked_by` — observability only."""
    return f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"


@dataclass(frozen=True)
class ChunkingSettings:
    """Contract 5 §2. Tunable, because chunk shape is a quality knob, not a constant."""

    target_tokens: int = DEFAULT_TARGET_TOKENS
    overlap_tokens: int = DEFAULT_OVERLAP_TOKENS
    min_tokens: int = DEFAULT_MIN_TOKENS

    def __post_init__(self) -> None:
        if self.target_tokens < 64:
            raise ConfigError("CHUNK_TARGET_TOKENS must be at least 64")
        if not 0 <= self.overlap_tokens < self.target_tokens:
            raise ConfigError("CHUNK_OVERLAP_TOKENS must be in [0, CHUNK_TARGET_TOKENS)")
        if not 0 <= self.min_tokens <= self.target_tokens:
            raise ConfigError("CHUNK_MIN_TOKENS must be in [0, CHUNK_TARGET_TOKENS]")


@dataclass(frozen=True)
class Settings:
    """Everything the worker needs to run. Built once, at startup, from the environment."""

    database_url: str
    voyage_api_key: str
    embedding_model: str = DEFAULT_EMBEDDING_MODEL
    embedding_dim: int = DEFAULT_EMBEDDING_DIM
    embedding_batch_size: int = MAX_INPUTS_PER_REQUEST
    embedding_provider: str = "voyage"  # "voyage" | "fake" — "fake" is for local dev only
    worker_id: str = field(default_factory=_default_worker_id)
    poll_interval_seconds: float = 2.0
    idle_log_interval_seconds: float = 300.0
    max_attempts: int = MAX_ATTEMPTS
    max_pages: int = MAX_PAGES
    max_file_bytes: int = MAX_FILE_BYTES
    chunking: ChunkingSettings = field(default_factory=ChunkingSettings)
    log_level: str = "INFO"

    def __post_init__(self) -> None:
        if self.embedding_dim < 1:
            raise ConfigError("EMBEDDING_DIM must be a positive integer")
        if not 1 <= self.embedding_batch_size <= MAX_INPUTS_PER_REQUEST:
            raise ConfigError(
                f"EMBEDDING_BATCH_SIZE must be in [1, {MAX_INPUTS_PER_REQUEST}]"
            )
        if self.embedding_provider not in {"voyage", "fake"}:
            raise ConfigError("EMBEDDING_PROVIDER must be 'voyage' or 'fake'")
        if not self.database_url.startswith("postgresql+asyncpg://"):
            raise ConfigError(
                "DATABASE_URL must be a postgresql+asyncpg:// URL — this worker uses asyncpg"
            )


def load_settings(*, require_api_key: bool = True) -> Settings:
    """Build `Settings` from the process environment.

    Raises:
        ConfigError: if a required variable is missing or malformed.
    """
    provider = os.environ.get("EMBEDDING_PROVIDER", "voyage").strip().lower()
    if provider == "fake":
        api_key = os.environ.get("VOYAGE_API_KEY", "")
    else:
        api_key = _env_str("VOYAGE_API_KEY") if require_api_key else os.environ.get(
            "VOYAGE_API_KEY", ""
        )

    return Settings(
        database_url=_env_str("DATABASE_URL"),
        voyage_api_key=api_key,
        embedding_model=os.environ.get("EMBEDDING_MODEL") or DEFAULT_EMBEDDING_MODEL,
        embedding_dim=_env_int("EMBEDDING_DIM", DEFAULT_EMBEDDING_DIM),
        embedding_batch_size=_env_int("EMBEDDING_BATCH_SIZE", MAX_INPUTS_PER_REQUEST),
        embedding_provider=provider,
        worker_id=os.environ.get("WORKER_ID") or _default_worker_id(),
        poll_interval_seconds=_env_float("WORKER_POLL_INTERVAL_SECONDS", 2.0),
        max_attempts=_env_int("WORKER_MAX_ATTEMPTS", MAX_ATTEMPTS),
        max_pages=_env_int("MAX_PAGES", MAX_PAGES),
        max_file_bytes=_env_int("MAX_FILE_BYTES", MAX_FILE_BYTES),
        chunking=ChunkingSettings(
            target_tokens=_env_int("CHUNK_TARGET_TOKENS", DEFAULT_TARGET_TOKENS),
            overlap_tokens=_env_int("CHUNK_OVERLAP_TOKENS", DEFAULT_OVERLAP_TOKENS),
            min_tokens=_env_int("CHUNK_MIN_TOKENS", DEFAULT_MIN_TOKENS),
        ),
        log_level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    )
