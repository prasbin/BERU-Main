"""Centralised application configuration.

All configuration is sourced from environment variables (optionally loaded from
a local ``.env`` file). Secrets such as API keys MUST come from the environment
and are never hardcoded. Access settings via :func:`get_settings`, which caches
a single immutable ``Settings`` instance for the process.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from backend import __version__


class Settings(BaseSettings):
    """Application settings loaded from the environment / ``.env`` file.

    Field names map to environment variables case-insensitively, e.g. the field
    ``llm_api_key`` is populated from ``LLM_API_KEY``.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---- Application ----
    app_name: str = "BERU"
    version: str = __version__
    environment: str = Field(default="development", alias="BERU_ENV")
    debug: bool = True
    host: str = "127.0.0.1"
    port: int = 8000

    # ---- Logging ----
    log_level: str = "INFO"
    log_format: str = "text"  # "text" | "json"

    # ---- CORS ----
    # Stored as a raw string to avoid brittle list-from-env parsing; use
    # ``cors_origin_list`` to obtain the parsed value.
    cors_origins: str = "*"

    # ---- Security / auth ----
    # Single-user API key. When set, every ``/api/v1`` route requires the
    # ``X-API-Key`` header to match this value. Blank disables auth — intended
    # for localhost development only; binding to a non-localhost interface
    # without a key is refused at startup (see ``main.create_app``). The key
    # acts as the **owner** credential: it authenticates as the system owner
    # and can manage user accounts via ``POST /auth/users``.
    api_key: str = Field(default="", alias="BERU_API_KEY")

    # ---- Accounts (Stage 5.1) ----
    # First-run bootstrap: when auth is enabled and no owner account exists yet,
    # the app creates one with these credentials at startup (a random password
    # is generated and logged when BERU_OWNER_PASSWORD is blank).
    owner_username: str = Field(default="owner", alias="BERU_OWNER_USERNAME")
    owner_password: str = Field(default="", alias="BERU_OWNER_PASSWORD")

    # ---- Database ----
    database_url: str = "sqlite+aiosqlite:///./beru.db"
    db_echo: bool = False

    # ---- Health / readiness ----
    # When true, the /ready endpoint performs a live LLM probe (one minimal
    # generation) in addition to the database ping. Off by default so a paid
    # LLM provider isn't billed on every orchestrated healthcheck tick; the
    # built-in mock provider is always probed (instant, offline).
    health_llm_probe: bool = False

    # ---- LLM provider ----
    llm_provider: str = "mock"  # "mock" | "openai_compatible"
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str = ""
    llm_model: str = "gpt-4o-mini"
    llm_temperature: float = 0.7
    llm_max_tokens: int = 1024
    llm_timeout: float = 60.0

    # ---- Retry / backoff ----
    # Max retry attempts for transient LLM failures (429, 5xx, network errors).
    llm_max_retries: int = Field(default=3, alias="BERU_LLM_MAX_RETRIES")
    # Base delay in seconds for exponential backoff (doubled each retry).
    llm_retry_base_delay: float = Field(default=1.0, alias="BERU_LLM_RETRY_BASE_DELAY")
    # Max delay cap in seconds to prevent excessive waits.
    llm_retry_max_delay: float = Field(default=30.0, alias="BERU_LLM_RETRY_MAX_DELAY")

    # ---- Rate limiting ----
    # Max chat requests per minute per client (IP). 0 = unlimited.
    rate_limit_chat_rpm: int = Field(default=30, alias="BERU_RATE_LIMIT_CHAT_RPM")
    # Burst allowance: max consecutive requests before the limiter kicks in.
    rate_limit_burst: int = Field(default=5, alias="BERU_RATE_LIMIT_BURST")
    # Max login attempts per minute per client (IP). 0 = unlimited.
    rate_limit_auth_rpm: int = Field(default=10, alias="BERU_RATE_LIMIT_AUTH_RPM")
    # Burst allowance for /auth/login before the limiter activates.
    rate_limit_auth_burst: int = Field(default=3, alias="BERU_RATE_LIMIT_AUTH_BURST")

    # ---- Reverse proxy ----
    # Honor X-Forwarded-For when rate-limiting. Keep OFF unless BERU genuinely
    # sits behind a trusted reverse proxy, otherwise clients can spoof the header
    # to dodge the limiter.
    trust_proxy_headers: bool = Field(default=False, alias="BERU_TRUST_PROXY_HEADERS")

    # ---- Request size limits ----
    # Max request body size in bytes (default 1 MB).
    max_request_body_bytes: int = Field(default=1_048_576, alias="BERU_MAX_REQUEST_BODY_BYTES")
    # Max message length in characters for the chat endpoint.
    max_message_length: int = Field(default=32_000, alias="BERU_MAX_MESSAGE_LENGTH")

    # ---- Conversation / memory ----
    memory_window_size: int = 20
    beru_system_prompt: str | None = None

    # ---- Tool calling ----
    max_tool_iterations: int = Field(default=10, alias="BERU_MAX_TOOL_ITERATIONS")

    # ---- Proactive / autonomous ----
    # Start the background scheduler + monitor loops with the app lifespan.
    proactive_enabled: bool = Field(default=True, alias="BERU_PROACTIVE_ENABLED")
    # Delete audit history (task runs / trigger fires) older than this many
    # days when the proactive runtime starts. 0 disables retention pruning.
    proactive_audit_retention_days: float = Field(
        default=0, alias="BERU_PROACTIVE_AUDIT_RETENTION_DAYS"
    )

    # ---- Memory strategy ----
    # "window" = short-term window (default, current behavior)
    # "summarise" = summarise older messages when history exceeds the threshold
    # "semantic" = embedding-based recall of relevant past messages
    memory_strategy: str = Field(default="window", alias="BERU_MEMORY_STRATEGY")
    # Number of recent messages kept verbatim when using summarise strategy.
    memory_summary_keep: int = Field(default=10, alias="BERU_MEMORY_SUMMARY_KEEP")
    # Message count threshold above which summarisation is triggered.
    memory_summary_threshold: int = Field(default=20, alias="BERU_MEMORY_SUMMARY_THRESHOLD")

    # ---- Embedding provider ----
    embedding_provider: str = Field(default="mock", alias="BERU_EMBEDDING_PROVIDER")
    embedding_base_url: str = Field(default="", alias="BERU_EMBEDDING_BASE_URL")
    embedding_api_key: str = Field(default="", alias="BERU_EMBEDDING_API_KEY")
    embedding_model: str = Field(default="text-embedding-3-small", alias="BERU_EMBEDDING_MODEL")
    embedding_dimension: int = Field(default=64, alias="BERU_EMBEDDING_DIMENSION")

    # ---- Voice system ----
    # STT/TTS providers. "mock" is the hermetic default; real providers are wired
    # behind the same protocol interface (e.g. whisper, edge-tts) when installed.
    voice_stt_provider: str = Field(default="mock", alias="BERU_VOICE_STT_PROVIDER")
    voice_tts_provider: str = Field(default="mock", alias="BERU_VOICE_TTS_PROVIDER")
    # Comma-separated wake words (case-insensitive), e.g. "beru,hey beru".
    voice_wake_words: str = Field(default="beru", alias="BERU_VOICE_WAKE_WORDS")
    # Maximum audio payload accepted per request/session in bytes.
    voice_max_audio_bytes: int = Field(default=10_000_000, alias="BERU_VOICE_MAX_AUDIO_BYTES")
    # Session idle timeout in seconds before a session is considered expired.
    voice_session_timeout: float = Field(default=300.0, alias="BERU_VOICE_SESSION_TIMEOUT")
    # Default audio formats for synthesis (see engines/speech.py).
    voice_sample_rate: int = Field(default=16_000, alias="BERU_VOICE_SAMPLE_RATE")
    voice_channels: int = Field(default=1, alias="BERU_VOICE_CHANNELS")

    # ---- Derived helpers ----
    @property
    def cors_origin_list(self) -> list[str]:
        """Return CORS origins as a list. ``"*"`` maps to ``["*"]``."""
        raw = self.cors_origins.strip()
        if raw == "*" or raw == "":
            return ["*"]
        return [origin.strip() for origin in raw.split(",") if origin.strip()]

    @property
    def is_production(self) -> bool:
        return self.environment.lower() in {"production", "prod"}

    @property
    def auth_enabled(self) -> bool:
        """True when a single-user API key is configured (auth is enforced)."""
        return bool(self.api_key.strip())

    @property
    def is_localhost_host(self) -> bool:
        """True when the server binds to a loopback interface only."""
        return self.host.strip().lower() in {"127.0.0.1", "localhost", "::1"}


@lru_cache
def get_settings() -> Settings:
    """Return a cached ``Settings`` instance for the current process."""
    return Settings()
