"""Role-based access control.

Role hierarchy (higher number => more capability):

    viewer(0) < pm(1) < admin(2) < owner(3)

A required role is satisfied by that role or any higher one.
"""

from __future__ import annotations

from ..models import Role

_RANK: dict[Role, int] = {
    Role.viewer: 0,
    Role.pm: 1,
    Role.admin: 2,
    Role.owner: 3,
}


def rank(role: Role) -> int:
    return _RANK[role]


def satisfies(actual: Role, required: Role) -> bool:
    return rank(actual) >= rank(required)


# Capability map — the minimum role each action requires.
CAN_VIEW = Role.viewer
CAN_ACT_ON_ITEM = Role.pm  # done/snooze/dismiss/escalate/assign
CAN_MANAGE_CONNECTIONS = Role.admin
CAN_MANAGE_ORG = Role.owner
