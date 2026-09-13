"""Password hashing — PBKDF2-HMAC-SHA256 (stdlib, no native wheels).

Format:  pbkdf2_sha256$<iterations>$<salt_b64>$<hash_b64>
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os

from ..config import settings

_ALGO = "pbkdf2_sha256"
#: OWASP's 2023 floor for PBKDF2-HMAC-SHA256. The configured value may only go
#: *below* this outside production, which config.assert_prod_secrets enforces.
_ITERATIONS = 390_000
_SALT_BYTES = 16


def _iterations() -> int:
    return settings.password_hash_iterations


def hash_password(password: str, *, iterations: int | None = None) -> str:
    iterations = iterations if iterations is not None else _iterations()
    salt = os.urandom(_SALT_BYTES)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"{_ALGO}${iterations}${_b64(salt)}${_b64(dk)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iter_s, salt_b64, hash_b64 = stored.split("$")
        if algo != _ALGO:
            return False
        salt = _unb64(salt_b64)
        expected = _unb64(hash_b64)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iter_s))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk, expected)


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


# --------------------------------------------------------------------------- #
# Policy
# --------------------------------------------------------------------------- #
#: Passwords seen constantly in credential-stuffing lists. A full breach-corpus
#: check (k-anonymity against HIBP) would be better but needs a network call on
#: the registration path, which the offline-first testability contract rules out.
#: This catches the handful that survive a length rule.
_BANNED = frozenset(
    {
        "password",
        "passw0rd",
        "password1",
        "password123",
        "passwordpassword",
        "qwertyuiop",
        "qwerty123456",
        "1234567890ab",
        "administrator",
        "letmein12345",
        "iloveyou1234",
        "welcome12345",
        "changeme1234",
        "abc123456789",
        "111111111111",
        "osprey123456",
    }
)


class PasswordPolicyError(ValueError):
    """The proposed password does not meet the configured policy."""


def check_policy(password: str, *, email: str = "") -> None:
    """Raise :class:`PasswordPolicyError` if the password is unacceptable.

    Length carries most of the weight -- NIST SP 800-63B deprecates composition
    rules -- but ``password_require_classes`` stays configurable because plenty
    of enterprise policies still mandate them and a deployment that must satisfy
    an auditor should not have to patch the code.
    """
    minimum = settings.password_min_length
    if len(password) < minimum:
        raise PasswordPolicyError(f"password must be at least {minimum} characters")
    if len(password) > 1024:
        # PBKDF2 over an unbounded input is a free CPU-exhaustion primitive.
        raise PasswordPolicyError("password must be at most 1024 characters")

    lowered = password.lower()
    if lowered in _BANNED:
        raise PasswordPolicyError("password is too common")
    local = email.split("@", 1)[0].lower() if email else ""
    if local and len(local) >= 4 and local in lowered:
        raise PasswordPolicyError("password must not contain your email address")

    required = settings.password_require_classes
    if required > 0:
        classes = sum(
            (
                any(c.islower() for c in password),
                any(c.isupper() for c in password),
                any(c.isdigit() for c in password),
                any(not c.isalnum() for c in password),
            )
        )
        if classes < required:
            raise PasswordPolicyError(
                f"password must mix at least {required} of: lowercase, uppercase, digits, symbols"
            )


def needs_rehash(stored: str) -> bool:
    """True when a stored hash was made with weaker parameters than current."""
    try:
        algo, iterations, _salt, _hash = stored.split("$")
    except ValueError:
        return True
    return algo != _ALGO or int(iterations) < _iterations()
