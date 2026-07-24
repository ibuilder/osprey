"""SQLModel tables — the Osprey data model (SPEC §5).

    Org ──< User ──< Membership (role)
    Org ──< Project
    Org ──< Connection   (tokens encrypted at rest)
    Project ──< Signal   (raw normalized event; unique per connection+external_id)
    Signal  ──> Item     (clustered/deduped unit of work)
    Item    ──< Score    (versioned; urgency/impact/confidence/total + factors)
    Item    ──< Action   (user feedback -> learning loop)
    Project ──< HotlistSnapshot (immutable, for export + history)
    Org     ──< AuditLog (append-only, hash-chained)

Portable by design: UUID primary keys stored as 32-char hex strings, JSON columns,
and timezone-aware UTC datetimes, so the identical schema runs on SQLite (dev/test)
and Postgres (prod).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import Enum

from sqlalchemy import JSON, Column, DateTime, UniqueConstraint
from sqlmodel import Field, SQLModel


def new_id() -> str:
    return uuid.uuid4().hex


def utcnow() -> datetime:
    return datetime.now(UTC)


def _dt_column() -> Column:
    return Column(DateTime(timezone=True), nullable=True)


# --------------------------------------------------------------------------- #
# Enums
# --------------------------------------------------------------------------- #
class Role(str, Enum):
    owner = "owner"
    admin = "admin"
    pm = "pm"
    viewer = "viewer"


class ConnectionStatus(str, Enum):
    pending = "pending"
    active = "active"
    degraded = "degraded"
    error = "error"
    revoked = "revoked"


class SourceKind(str, Enum):
    email = "email"
    rfi = "rfi"
    submittal = "submittal"
    change_order = "change_order"
    invoice = "invoice"
    task = "task"
    event = "event"
    doc = "doc"
    observation = "observation"
    general = "general"


class Category(str, Enum):
    rfi = "rfi"
    change_order = "change_order"
    submittal = "submittal"
    invoice = "invoice"
    safety = "safety"
    schedule = "schedule"
    contractual_notice = "contractual_notice"
    general = "general"


class Bucket(str, Enum):
    act_today = "act_today"  # 🔴
    this_week = "this_week"  # 🟠
    watch = "watch"  # 🟡
    done = "done"  # cleared


class ActionType(str, Enum):
    done = "done"
    snooze = "snooze"
    dismiss = "dismiss"
    escalate = "escalate"
    assign = "assign"
    reopen = "reopen"


class ItemStatus(str, Enum):
    open = "open"
    snoozed = "snoozed"
    dismissed = "dismissed"
    done = "done"


class AIProvider(str, Enum):
    claude = "claude"
    openai = "openai"
    ollama = "ollama"


class ScriptStatus(str, Enum):
    idle = "idle"
    running = "running"
    ok = "ok"
    error = "error"
    disabled = "disabled"


# --------------------------------------------------------------------------- #
# Tenancy & identity
# --------------------------------------------------------------------------- #
class Org(SQLModel, table=True):
    __tablename__ = "org"
    id: str = Field(default_factory=new_id, primary_key=True)
    name: str
    created_at: datetime = Field(default_factory=utcnow, sa_column=_dt_column())


class User(SQLModel, table=True):
    __tablename__ = "user"
    id: str = Field(default_factory=new_id, primary_key=True)
    email: str = Field(index=True, unique=True)
    full_name: str = ""
    password_hash: str = ""  # PBKDF2; empty for SSO-only users
    is_active: bool = True
    created_at: datetime = Field(default_factory=utcnow, sa_column=_dt_column())


class Membership(SQLModel, table=True):
    __tablename__ = "membership"
    __table_args__ = (UniqueConstraint("org_id", "user_id", name="uq_membership"),)
    id: str = Field(default_factory=new_id, primary_key=True)
    org_id: str = Field(foreign_key="org.id", index=True)
    user_id: str = Field(foreign_key="user.id", index=True)
    role: Role = Role.viewer


class Project(SQLModel, table=True):
    __tablename__ = "project"
    id: str = Field(default_factory=new_id, primary_key=True)
    org_id: str = Field(foreign_key="org.id", index=True)
    name: str
    # Per-project scoring weight overrides {urgency,impact,confidence}; nudged by
    # the learning loop. Empty => use org/config defaults.
    weights: dict = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utcnow, sa_column=_dt_column())


# --------------------------------------------------------------------------- #
# Connections (source accounts)
# --------------------------------------------------------------------------- #
class Connection(SQLModel, table=True):
    __tablename__ = "connection"
    id: str = Field(default_factory=new_id, primary_key=True)
    org_id: str = Field(foreign_key="org.id", index=True)
    project_id: str = Field(foreign_key="project.id", index=True)
    source_type: str  # "outlook" | "gmail" | "procore" | "filedrop" ...
    account_ref: str = ""  # mailbox address / company id / label
    # AES-256-GCM sealed OAuth tokens (never plaintext at rest). See security/crypto.
    encrypted_tokens: str = ""
    scopes: list = Field(default_factory=list, sa_column=Column(JSON))
    status: ConnectionStatus = ConnectionStatus.pending
    cursor: str | None = None  # delta / history token for incremental poll
    last_sync: datetime | None = Field(default=None, sa_column=_dt_column())
    last_error: str | None = None
    created_at: datetime = Field(default_factory=utcnow, sa_column=_dt_column())


class AIConnection(SQLModel, table=True):
    """A user's own AI account (bring-your-own key) used to sift data into the hotlist."""

    __tablename__ = "ai_connection"
    id: str = Field(default_factory=new_id, primary_key=True)
    org_id: str = Field(foreign_key="org.id", index=True)
    project_id: str | None = Field(default=None, foreign_key="project.id", index=True)
    provider: AIProvider = AIProvider.claude
    label: str = ""
    model: str = ""
    # AES-256-GCM sealed API key (never plaintext at rest). Empty for local Ollama.
    encrypted_key: str = ""
    base_url: str | None = None
    status: ConnectionStatus = ConnectionStatus.active
    last_used: datetime | None = Field(default=None, sa_column=_dt_column())
    created_at: datetime = Field(default_factory=utcnow, sa_column=_dt_column())


class Device(SQLModel, table=True):
    """A mobile/desktop device registered to receive push (APNs/FCM/Web Push)."""

    __tablename__ = "device"
    __table_args__ = (UniqueConstraint("user_id", "token", name="uq_device_token"),)
    id: str = Field(default_factory=new_id, primary_key=True)
    org_id: str = Field(foreign_key="org.id", index=True)
    user_id: str = Field(foreign_key="user.id", index=True)
    platform: str = "web"  # ios | android | web
    token: str = ""
    created_at: datetime = Field(default_factory=utcnow, sa_column=_dt_column())


class ScriptTask(SQLModel, table=True):
    """A user-authored Python script that runs as a background task and emits signals."""

    __tablename__ = "script_task"
    id: str = Field(default_factory=new_id, primary_key=True)
    org_id: str = Field(foreign_key="org.id", index=True)
    project_id: str = Field(foreign_key="project.id", index=True)
    name: str
    source_code: str = ""
    enabled: bool = True
    schedule_minutes: int = 0  # 0 => run on demand only
    timeout_seconds: int = 30
    status: ScriptStatus = ScriptStatus.idle
    last_run: datetime | None = Field(default=None, sa_column=_dt_column())
    last_result: dict = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utcnow, sa_column=_dt_column())


# --------------------------------------------------------------------------- #
# Signals & Items
# --------------------------------------------------------------------------- #
class Signal(SQLModel, table=True):
    __tablename__ = "signal"
    __table_args__ = (UniqueConstraint("connection_id", "external_id", name="uq_signal_dedupe"),)
    id: str = Field(default_factory=new_id, primary_key=True)
    project_id: str = Field(foreign_key="project.id", index=True)
    connection_id: str = Field(foreign_key="connection.id", index=True)
    source_type: str
    source_kind: SourceKind = SourceKind.general
    external_id: str = Field(index=True)  # dedupe key within source
    thread_key: str | None = Field(default=None, index=True)
    title: str = ""
    body: str = ""  # cleaned text
    participants: list = Field(default_factory=list, sa_column=Column(JSON))
    due_at: datetime | None = Field(default=None, sa_column=_dt_column())
    amount: float | None = None  # $ exposure magnitude if present
    url: str | None = None  # deep link back to source
    raw: dict = Field(default_factory=dict, sa_column=Column(JSON))
    embedding: list | None = Field(default=None, sa_column=Column(JSON))
    item_id: str | None = Field(default=None, foreign_key="item.id", index=True)
    occurred_at: datetime = Field(default_factory=utcnow, sa_column=_dt_column())
    ingested_at: datetime = Field(default_factory=utcnow, sa_column=_dt_column())


class Item(SQLModel, table=True):
    __tablename__ = "item"
    id: str = Field(default_factory=new_id, primary_key=True)
    project_id: str = Field(foreign_key="project.id", index=True)
    title: str = ""
    category: Category = Category.general
    summary: str = ""
    status: ItemStatus = ItemStatus.open
    owner: str | None = None
    snooze_until: datetime | None = Field(default=None, sa_column=_dt_column())
    created_at: datetime = Field(default_factory=utcnow, sa_column=_dt_column())
    updated_at: datetime = Field(default_factory=utcnow, sa_column=_dt_column())


class Score(SQLModel, table=True):
    __tablename__ = "score"
    id: str = Field(default_factory=new_id, primary_key=True)
    item_id: str = Field(foreign_key="item.id", index=True)
    version: int = 1
    urgency: float = 0.0
    impact: float = 0.0
    confidence: float = 0.0
    total: float = 0.0
    bucket: Bucket = Bucket.watch
    factors: dict = Field(default_factory=dict, sa_column=Column(JSON))
    explanation: str = ""
    created_at: datetime = Field(default_factory=utcnow, sa_column=_dt_column())


class Action(SQLModel, table=True):
    __tablename__ = "action"
    id: str = Field(default_factory=new_id, primary_key=True)
    item_id: str = Field(foreign_key="item.id", index=True)
    project_id: str = Field(foreign_key="project.id", index=True)
    user_id: str | None = Field(default=None, foreign_key="user.id")
    type: ActionType
    meta: dict = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utcnow, sa_column=_dt_column())


class HotlistSnapshot(SQLModel, table=True):
    __tablename__ = "hotlist_snapshot"
    id: str = Field(default_factory=new_id, primary_key=True)
    project_id: str = Field(foreign_key="project.id", index=True)
    top_n: int = 25
    generated_by: str | None = None
    # Frozen, denormalized rows so Excel and PDF exports always agree.
    payload: dict = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utcnow, sa_column=_dt_column())


class AuditLog(SQLModel, table=True):
    __tablename__ = "audit_log"
    id: str = Field(default_factory=new_id, primary_key=True)
    org_id: str = Field(foreign_key="org.id", index=True)
    actor: str = "system"
    action: str = ""
    target: str = ""
    meta: dict = Field(default_factory=dict, sa_column=Column(JSON))
    prev_hash: str = ""
    hash: str = ""  # sha256(prev_hash + canonical record)
    created_at: datetime = Field(default_factory=utcnow, sa_column=_dt_column())


__all__ = [
    "new_id",
    "utcnow",
    "Role",
    "ConnectionStatus",
    "SourceKind",
    "Category",
    "Bucket",
    "ActionType",
    "ItemStatus",
    "AIProvider",
    "ScriptStatus",
    "Org",
    "User",
    "Membership",
    "Project",
    "Connection",
    "AIConnection",
    "ScriptTask",
    "Device",
    "Signal",
    "Item",
    "Score",
    "Action",
    "HotlistSnapshot",
    "AuditLog",
]
