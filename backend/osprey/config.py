"""Application settings (pydantic-settings). All env vars use the ``OSPREY_`` prefix."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="OSPREY_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---- App ----------------------------------------------------------------
    env: Literal["dev", "test", "prod"] = "dev"
    debug: bool = True
    log_level: str = "INFO"
    app_name: str = "Osprey"
    public_base_url: str = ""  # external URL for webhook callbacks (subscriptions)
    # Create tables from the models at startup. Off for servers, which migrate with
    # Alembic; on for the desktop bundle, which ships no migration step.
    create_schema_on_start: bool = False
    # Origins allowed to call the API in production. Dev allows everything. The
    # desktop bundle sets this to the Tauri webview origins, which are not "*".
    cors_allow_origins: list[str] = []

    # ---- Security -----------------------------------------------------------
    # Override BOTH of these in production. Defaults are intentionally insecure
    # so a misconfigured prod deploy is obvious.
    secret_key: str = "dev-only-insecure-change-me"
    encryption_key: str = "dev-only-insecure-change-me"
    access_token_ttl_minutes: int = 60
    jwt_algorithm: str = "HS256"

    # ---- Database -----------------------------------------------------------
    database_url: str = "sqlite+aiosqlite:///./osprey.db"

    # ---- Redis / queue ------------------------------------------------------
    redis_url: str = "redis://localhost:6379/0"

    # ---- AI layer -----------------------------------------------------------
    ai_provider: Literal["deterministic", "claude", "openai", "ollama"] = "deterministic"
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-5"
    ollama_host: str = "http://localhost:11434"
    ollama_model: str = "llama3.1"

    # ---- Embeddings ---------------------------------------------------------
    embedding_provider: Literal["hashing"] = "hashing"
    embedding_dim: int = 256

    # ---- Engine -------------------------------------------------------------
    cluster_similarity_threshold: float = 0.82
    hotlist_top_n: int = 25

    # Scoring weights (per-project tunable; these are the org defaults). The
    # learning loop nudges per-project copies of these.
    weight_urgency: float = 0.40
    weight_impact: float = 0.50
    weight_confidence: float = 0.10

    # ---- Connectors (OAuth apps; the desktop app drives the user consent) ----
    msgraph_client_id: str = ""
    msgraph_client_secret: str = ""
    msgraph_tenant_id: str = ""
    google_client_id: str = ""
    google_client_secret: str = ""
    procore_client_id: str = ""
    procore_client_secret: str = ""
    procore_base_url: str = "https://api.procore.com"
    webhook_hmac_secret: str = "dev-only-insecure-change-me"

    # Poller pacing. Providers meter per OAuth app, so these are process-wide per
    # source_type; see connectors/http.py. Deliberately conservative — being slow
    # costs a poll cycle, being throttled costs the whole connection.
    connector_rate_per_sec: float = 5.0
    connector_rate_burst: float = 10.0
    connector_max_attempts: int = 4

    # ---- Script tasks (user-authored Python background jobs) -----------------
    scripts_enabled: bool = True
    scripts_max_timeout_seconds: int = 60

    # ---- Feature flags -------------------------------------------------------
    feature_ai_sift: bool = True
    feature_scripts: bool = True

    # ---- Observability (OpenTelemetry; optional 'otel' extra) ----------------
    otel_enabled: bool = False
    otel_service_name: str = "osprey-api"
    otel_exporter_otlp_endpoint: str = ""  # e.g. http://otel-collector:4317

    # ---- Row-level security (Postgres tenant isolation) ----------------------
    rls_enabled: bool = False  # requires the 0002 migration + a non-superuser DB role

    # ---- Push notifications --------------------------------------------------
    # "logging" (offline default) | "auto" (route by device platform to any
    # configured backend) | "apns" | "fcm" | "webpush"
    push_provider: Literal["logging", "auto", "apns", "fcm", "webpush"] = "logging"
    # APNs (token-based auth)
    apns_team_id: str = ""
    apns_key_id: str = ""
    apns_private_key: str = ""  # contents of the .p8 (PKCS8 EC PEM)
    apns_bundle_id: str = ""  # apns-topic
    apns_use_sandbox: bool = False
    # FCM (HTTP v1 + service account)
    fcm_project_id: str = ""
    fcm_service_account_json: str = ""  # inline service-account JSON
    # Web Push (VAPID) — device token is a JSON subscription {endpoint, keys{p256dh, auth}}
    vapid_public_key: str = ""
    vapid_private_key: str = ""
    vapid_subject: str = "mailto:ops@ospreyhq.dev"

    @property
    def is_prod(self) -> bool:
        return self.env == "prod"

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    def assert_prod_secrets(self) -> list[str]:
        """Return a list of misconfiguration warnings for a production boot."""
        problems: list[str] = []
        insecure = "dev-only-insecure-change-me"
        if self.is_prod:
            if self.secret_key == insecure:
                problems.append("OSPREY_SECRET_KEY is still the insecure default")
            if self.encryption_key == insecure:
                problems.append("OSPREY_ENCRYPTION_KEY is still the insecure default")
            if self.webhook_hmac_secret == insecure:
                problems.append("OSPREY_WEBHOOK_HMAC_SECRET is still the insecure default")
            if self.is_sqlite:
                problems.append("SQLite in production — set OSPREY_DATABASE_URL to Postgres")
        return problems


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
