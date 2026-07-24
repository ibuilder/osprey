"""Connector-token vault — AES-256-GCM envelope encryption.

OAuth tokens are never persisted in plaintext. The 256-bit data key is derived
from ``OSPREY_ENCRYPTION_KEY`` (a KMS/Vault-managed secret in server mode, or an
OS-keychain value in local mode) via SHA-256, so any string works as the
configured key while the actual cipher key is always a full 32 bytes.

Envelope layout (then base64url-encoded):  version(1) ‖ nonce(12) ‖ ciphertext+tag
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ..config import settings

_VERSION = b"\x01"
_NONCE_LEN = 12


def _data_key(key_material: str | None = None) -> bytes:
    material = (key_material if key_material is not None else settings.encryption_key).encode(
        "utf-8"
    )
    return hashlib.sha256(material).digest()  # 32 bytes -> AES-256


def seal(data: dict[str, Any], *, key_material: str | None = None) -> str:
    """Encrypt a JSON-serializable dict into a base64url token string."""
    plaintext = json.dumps(data, separators=(",", ":"), sort_keys=True).encode("utf-8")
    aes = AESGCM(_data_key(key_material))
    nonce = os.urandom(_NONCE_LEN)
    ct = aes.encrypt(nonce, plaintext, _VERSION)  # version bound as associated data
    return base64.urlsafe_b64encode(_VERSION + nonce + ct).decode("ascii")


def open_sealed(token: str, *, key_material: str | None = None) -> dict[str, Any]:
    """Decrypt a token produced by :func:`seal`. Raises on tamper/wrong key."""
    blob = base64.urlsafe_b64decode(token.encode("ascii"))
    version, nonce, ct = blob[:1], blob[1 : 1 + _NONCE_LEN], blob[1 + _NONCE_LEN :]
    if version != _VERSION:
        raise ValueError(f"unsupported vault version: {version!r}")
    aes = AESGCM(_data_key(key_material))
    plaintext = aes.decrypt(nonce, ct, version)
    return json.loads(plaintext.decode("utf-8"))


def rotate(token: str, *, old_key: str, new_key: str) -> str:
    """Re-seal a token under a new master key (key rotation support)."""
    return seal(open_sealed(token, key_material=old_key), key_material=new_key)
