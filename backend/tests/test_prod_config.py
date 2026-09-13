"""Production configuration must fail loudly, not degrade quietly.

A deploy carrying a default signing key is not "degraded", it is unprotected.
These tests pin the distinction between what blocks a boot and what only warns,
because getting that line wrong in either direction is expensive: too strict and
a valid deploy will not start, too lax and an unprotected one will.
"""

from __future__ import annotations

import pytest

from osprey.config import Settings

INSECURE = "dev-only-insecure-change-me"
GOOD_KEY = "k" * 48


def _prod(**overrides) -> Settings:
    """A settings object that should pass a production boot.

    Every security-relevant field is passed explicitly. Settings still reads the
    environment for anything omitted, and conftest lowers
    OSPREY_PASSWORD_HASH_ITERATIONS for the suite -- so leaving it out here would
    make this fixture depend on the harness rather than describe a deployment.
    """
    base = {
        "env": "prod",
        "debug": False,
        "secret_key": GOOD_KEY,
        "encryption_key": "real-encryption-key",
        "webhook_hmac_secret": "real-hmac-secret",
        "database_url": "postgresql+asyncpg://osprey_app:x@db:5432/osprey",
        "cors_allow_origins": ["https://osprey.example.com"],
        "password_hash_iterations": 390_000,
    }
    base.update(overrides)
    return Settings(**base)


def test_a_correctly_configured_production_boot_is_allowed():
    assert _prod().assert_prod_secrets() == []


@pytest.mark.parametrize(
    ("override", "expected"),
    [
        ({"secret_key": INSECURE}, "OSPREY_SECRET_KEY"),
        ({"secret_key": "tooshort"}, "shorter than 32"),
        ({"encryption_key": INSECURE}, "OSPREY_ENCRYPTION_KEY"),
        ({"webhook_hmac_secret": INSECURE}, "OSPREY_WEBHOOK_HMAC_SECRET"),
        ({"database_url": "sqlite+aiosqlite:///./osprey.db"}, "SQLite in production"),
        ({"debug": True}, "OSPREY_DEBUG"),
        ({"cors_allow_origins": []}, "OSPREY_CORS_ALLOW_ORIGINS is empty"),
        ({"cors_allow_origins": ["*"]}, "contains '*'"),
        ({"password_hash_iterations": 1000}, "OWASP floor"),
        ({"oidc_enabled": True}, "issuer/client_id"),
    ],
)
def test_each_insecure_setting_blocks_the_boot(override, expected):
    problems = _prod(**override).assert_prod_secrets()
    assert any(expected in p for p in problems), problems


def test_development_is_never_blocked():
    """The insecure defaults are the point of the dev experience."""
    assert Settings(env="dev").assert_prod_secrets() == []
    assert Settings(env="test").assert_prod_secrets() == []


def test_lowering_the_kdf_cost_is_allowed_outside_production():
    """The test suite depends on this; production must not."""
    assert Settings(env="test", password_hash_iterations=1000).assert_prod_secrets() == []


def test_advisories_warn_without_blocking():
    settings = _prod(rls_enabled=False, rate_limit_enabled=False, public_base_url="")
    assert settings.assert_prod_secrets() == []  # none of these are fatal
    warnings = settings.prod_warnings()
    assert any("row-level security" in w for w in warnings)
    assert any("rate limiting" in w for w in warnings)
    assert any("OSPREY_PUBLIC_BASE_URL" in w for w in warnings)


def test_a_well_configured_deployment_has_few_advisories():
    settings = _prod(
        rls_enabled=True,
        public_base_url="https://osprey.example.com",
        retention_signal_days=90,
        retention_item_days=365,
        metrics_token="scrape",
    )
    assert settings.prod_warnings() == []


def test_the_app_refuses_to_start_on_a_fatal_misconfiguration(monkeypatch):
    """The check is wired into the lifespan, not merely available to call."""
    import asyncio

    from osprey.config import settings as live
    from osprey.main import ConfigurationError, lifespan

    monkeypatch.setattr(live, "env", "prod")
    monkeypatch.setattr(live, "secret_key", INSECURE)

    async def boot():
        async with lifespan(object()):  # pragma: no cover - must not be reached
            raise AssertionError("startup should have been refused")

    with pytest.raises(ConfigurationError) as excinfo:
        asyncio.get_event_loop().run_until_complete(boot()) if False else asyncio.run(boot())
    assert "OSPREY_SECRET_KEY" in str(excinfo.value)


def test_debug_is_off_unless_asked_for():
    """A production boot must not need an explicit OSPREY_DEBUG=false.

    It used to default on, so the Helm chart, the Docker image and docker-compose
    -- none of which set it -- were all refused at boot. Found by the kind smoke
    deploy crash-looping on "OSPREY_DEBUG is on in production".
    """
    assert Settings.model_fields["debug"].default is False


def test_an_image_style_environment_boots(monkeypatch):
    """The environment the Helm chart actually provides: prod, secrets, no DEBUG."""
    monkeypatch.delenv("OSPREY_DEBUG", raising=False)
    settings = Settings(
        _env_file=None,
        env="prod",
        secret_key=GOOD_KEY,
        encryption_key="real-encryption-key",
        webhook_hmac_secret="real-hmac-secret",
        database_url="postgresql+asyncpg://osprey_app:x@db:5432/osprey",
        cors_allow_origins=["https://osprey.example.com"],
        password_hash_iterations=390_000,
    )
    assert settings.debug is False
    assert settings.assert_prod_secrets() == []
