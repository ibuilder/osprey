"""Shared validated field types.

``EmailStr`` here is deliberately *not* ``pydantic.EmailStr``. That one pulls in
``email-validator`` (and ``idna``), which the PyInstaller-frozen desktop sidecar
would need an extra hook to carry, for a check we can do exactly as well with a
syntactic rule. Address *deliverability* is never established by a regex anyway --
only by sending mail -- so the useful job here is rejecting the malformed input
that would otherwise fail deeper in the stack, and normalising case.
"""

from __future__ import annotations

import re
from typing import Annotated

from pydantic import AfterValidator, StringConstraints

# One @, no whitespace, a dotted domain with a 2+ character TLD, no consecutive
# dots, and no leading/trailing dot in either half. Intentionally stricter than
# RFC 5322 (which permits quoted strings and comments that no real signup uses)
# and looser than any deliverability claim.
_EMAIL_RE = re.compile(
    r"^(?!\.)[A-Za-z0-9!#$%&'*+/=?^_`{|}~.-]{1,64}(?<!\.)"
    r"@"
    r"(?!-)[A-Za-z0-9-]{1,63}(?<!-)"
    r"(\.(?!-)[A-Za-z0-9-]{1,63}(?<!-))*"
    r"\.[A-Za-z]{2,63}$"
)


def _validate_email(value: str) -> str:
    candidate = value.strip()
    if ".." in candidate or not _EMAIL_RE.match(candidate):
        raise ValueError("not a valid email address")
    local, _, domain = candidate.rpartition("@")
    # Domains are case-insensitive; local parts are not, per RFC, but every
    # provider treats them so and Osprey compares addresses for identity.
    return f"{local}@{domain.lower()}"


EmailStr = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=3, max_length=254),
    AfterValidator(_validate_email),
]
