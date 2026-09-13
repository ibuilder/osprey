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
    # Off unless asked for. Defaulting it on meant every production deploy that
    # did not explicitly set OSPREY_DEBUG=false -- the Helm chart, the Docker
    # image and docker-compose all -- was refused at boot by assert_prod_secrets.
    # Development turns it on through .env (see .env.example).
    debug: bool = False
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
    # Refresh tokens are opaque, stored hashed, and revocable one-by-one. Access
    # tokens stay short-lived because they are not checked against the database
    # on every request beyond the cheap token-version comparison.
    refresh_token_ttl_days: int = 14
    jwt_algorithm: str = "HS256"
    jwt_issuer: str = "osprey"
    jwt_audience: str = "osprey-api"

    # ---- HTTP hardening ------------------------------------------------------
    # Emitted only over TLS-terminated prod traffic; a browser ignores HSTS on
    # plain HTTP anyway, but sending it in dev would poison localhost.
    hsts_max_age_seconds: int = 63_072_000  # 2 years, preload-eligible
    security_headers_enabled: bool = True
    max_request_body_bytes: int = 10 * 1024 * 1024  # 10 MiB
    # Trust X-Forwarded-For only behind a proxy you control; otherwise a client
    # can forge its own source address and defeat per-IP rate limiting.
    trust_proxy_headers: bool = False

    # ---- Rate limiting -------------------------------------------------------
    # Redis-backed when a worker/queue Redis is configured, in-process otherwise
    # (correct for a single replica, best-effort for many — see security/ratelimit).
    rate_limit_enabled: bool = True
    rate_limit_backend: Literal["auto", "memory", "redis"] = "auto"
    rate_limit_authenticated_per_minute: int = 600
    rate_limit_anonymous_per_minute: int = 60
    # Credential endpoints are metered far harder, per (ip, email) pair.
    rate_limit_login_per_minute: int = 10
    rate_limit_login_per_hour: int = 60
    # Consecutive failures before an account is temporarily locked out.
    login_max_failures: int = 10
    login_lockout_minutes: int = 15

    # ---- Password policy -----------------------------------------------------
    password_min_length: int = 12
    password_require_classes: int = 3  # of: lower, upper, digit, symbol
    # PBKDF2 rounds. Lowered only by the test suite, which hashes hundreds of
    # passwords per run and would otherwise spend minutes doing it; a production
    # value below the OWASP floor is refused at startup (assert_prod_secrets).
    password_hash_iterations: int = 390_000

    # ---- SSO (OIDC authorization-code + PKCE) --------------------------------
    oidc_enabled: bool = False
    oidc_issuer: str = ""  # e.g. https://login.microsoftonline.com/<tenant>/v2.0
    oidc_client_id: str = ""
    oidc_client_secret: str = ""
    oidc_redirect_url: str = ""  # {public_base_url}/auth/sso/callback
    oidc_scopes: list[str] = ["openid", "profile", "email"]
    # Only these email domains may sign in via SSO. Empty means any domain the
    # IdP vouches for, which is right for a single-tenant IdP and wrong for a
    # multi-tenant one (e.g. Microsoft "common") — set it there.
    oidc_allowed_email_domains: list[str] = []
    # Org that SSO users join, and the role they get. Unset => SSO users must
    # already have a membership (provisioned by SCIM or invited).
    oidc_default_org_id: str = ""
    oidc_default_role: Literal["owner", "admin", "pm", "viewer"] = "viewer"
    oidc_auto_provision: bool = False

    # ---- SCIM 2.0 provisioning ----------------------------------------------
    scim_enabled: bool = False

    # ---- Data governance -----------------------------------------------------
    # 0 disables purging. Signals age out first (they are the bulk); items and
    # their scores are kept longer because they carry the decision record.
    retention_signal_days: int = 0
    retention_item_days: int = 0
    retention_audit_days: int = 0  # audit is hash-chained; purging breaks the chain
    retention_snapshot_days: int = 0

    # ---- Metrics -------------------------------------------------------------
    metrics_enabled: bool = True
    # When set, /metrics requires "Authorization: Bearer <token>". Leave empty to
    # serve it unauthenticated and restrict it at the network layer instead.
    metrics_token: str = ""

    # ---- Database -----------------------------------------------------------
    database_url: str = "sqlite+aiosqlite:///./osprey.db"

    # ---- Redis / queue ------------------------------------------------------
    redis_url: str = "redis://localhost:6379/0"

    # ---- AI layer -----------------------------------------------------------
    ai_provider: Literal["deterministic", "claude", "openai", "ollama"] = "deterministic"
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-5"
    # Used only when OSPREY_AI_PROVIDER=openai. Deliberately separate: a provider
    # must never fall back to another vendor's key, because that sends one
    # company's secret to another company's API.
    openai_api_key: str = ""
    openai_model: str = "gpt-4o"
    openai_base_url: str = ""
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
        """Return fatal production misconfigurations. Empty list means safe to boot.

        These are the settings whose default is *insecure*, not merely suboptimal —
        a deploy carrying any of them is not protecting anything, so
        :func:`osprey.main.lifespan` refuses to start rather than pretending to.
        Advisory items live in :meth:`prod_warnings`.
        """
        problems: list[str] = []
        insecure = "dev-only-insecure-change-me"
        if not self.is_prod:
            return problems
        if self.secret_key == insecure:
            problems.append("OSPREY_SECRET_KEY is still the insecure default")
        elif len(self.secret_key) < 32:
            problems.append("OSPREY_SECRET_KEY is shorter than 32 characters")
        if self.encryption_key == insecure:
            problems.append("OSPREY_ENCRYPTION_KEY is still the insecure default")
        if self.webhook_hmac_secret == insecure:
            problems.append("OSPREY_WEBHOOK_HMAC_SECRET is still the insecure default")
        if self.is_sqlite:
            problems.append("SQLite in production — set OSPREY_DATABASE_URL to Postgres")
        if self.debug:
            problems.append("OSPREY_DEBUG is on in production")
        if self.password_hash_iterations < 390_000:
            problems.append("OSPREY_PASSWORD_HASH_ITERATIONS is below the OWASP floor of 390,000")
        if not self.cors_allow_origins:
            problems.append(
                "OSPREY_CORS_ALLOW_ORIGINS is empty — browsers cannot reach this API. "
                'Set it to the client origins, e.g. ["https://osprey.example.com"].'
            )
        if "*" in self.cors_allow_origins:
            problems.append(
                "OSPREY_CORS_ALLOW_ORIGINS contains '*', which cannot be combined with "
                "credentialed requests"
            )
        if self.oidc_enabled and not (self.oidc_issuer and self.oidc_client_id):
            problems.append("OSPREY_OIDC_ENABLED is set but issuer/client_id are missing")
        return problems

    def prod_warnings(self) -> list[str]:
        """Non-fatal production advisories — logged at startup, never block a boot."""
        warnings: list[str] = []
        if not self.is_prod:
            return warnings
        if not self.rls_enabled and not self.is_sqlite:
            warnings.append(
                "row-level security is off; tenant isolation rests on application "
                "code alone (set OSPREY_RLS_ENABLED after migration 0002)"
            )
        if not self.rate_limit_enabled:
            warnings.append("rate limiting is disabled")
        if not self.public_base_url:
            warnings.append("OSPREY_PUBLIC_BASE_URL is unset; webhook subscriptions cannot renew")
        if self.retention_signal_days == 0 and self.retention_item_days == 0:
            warnings.append("no data-retention window configured; nothing is ever purged")
        if self.metrics_enabled and not self.metrics_token:
            warnings.append("/metrics is unauthenticated; restrict it at the network layer")
        return warnings


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
