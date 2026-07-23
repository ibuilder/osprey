"""Token vault, password hashing, and audit-chain integrity."""

from __future__ import annotations

import pytest
from cryptography.exceptions import InvalidTag

from osprey.models import Org
from osprey.security import audit, crypto
from osprey.security.passwords import hash_password, verify_password


def test_token_vault_roundtrip_and_tamper():
    tokens = {"access_token": "abc", "refresh_token": "xyz", "scope": "Mail.Read"}
    sealed = crypto.seal(tokens)
    assert "abc" not in sealed  # not plaintext at rest
    assert crypto.open_sealed(sealed) == tokens

    # Tampering the ciphertext must fail authentication (AES-GCM tag).
    bad = sealed[:-2] + ("AA" if not sealed.endswith("AA") else "BB")
    with pytest.raises(InvalidTag):
        crypto.open_sealed(bad)


def test_token_vault_wrong_key_fails():
    sealed = crypto.seal({"a": 1}, key_material="key-one")
    with pytest.raises(InvalidTag):
        crypto.open_sealed(sealed, key_material="key-two")


def test_password_hash_verify():
    h = hash_password("supersecret1")
    assert h.startswith("pbkdf2_sha256$")
    assert verify_password("supersecret1", h)
    assert not verify_password("wrong", h)


async def test_audit_chain_is_tamper_evident(session):
    org = Org(name="Chain Co")
    session.add(org)
    await session.flush()

    await audit.record(session, org_id=org.id, actor="a@x", action="login", target="u1")
    await audit.record(session, org_id=org.id, actor="a@x", action="project.created", target="p1")
    e3 = await audit.record(session, org_id=org.id, actor="a@x", action="export", target="p1")
    await session.flush()

    assert await audit.verify_chain(session, org.id)

    # Tamper with a record -> chain breaks.
    e3.action = "deleted"
    session.add(e3)
    await session.flush()
    assert not await audit.verify_chain(session, org.id)
