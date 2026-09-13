"""Pydantic DTOs for the REST API."""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..models import ActionType, AIProvider, Role
from .types import EmailStr


# ---- Auth ------------------------------------------------------------------ #
class RegisterRequest(BaseModel):
    # EmailStr rejects the addresses that would otherwise reach the connector and
    # notification layers and fail there instead, where the error is opaque.
    # Strength is enforced separately (security.passwords.check_policy) so the
    # rule set stays configurable per deployment rather than baked into the DTO.
    email: EmailStr
    password: str = Field(min_length=8, max_length=1024)
    full_name: str = Field(default="", max_length=200)
    org_name: str = Field(default="My Org", max_length=200)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(max_length=1024)


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=16, max_length=512)


class PasswordChangeRequest(BaseModel):
    current_password: str = Field(max_length=1024)
    new_password: str = Field(min_length=8, max_length=1024)


class SessionOut(BaseModel):
    id: str
    created_at: str
    expires_at: str
    user_agent: str = ""
    ip: str = ""


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    # Absent on flows that must not mint a long-lived session (SCIM, service
    # tokens); present on interactive sign-in.
    refresh_token: str | None = None
    expires_in: int = 0
    role: Role
    org_id: str
    user_id: str


# ---- Members / invites ----------------------------------------------------- #
class InviteCreate(BaseModel):
    email: EmailStr
    role: Role = Role.viewer
    expires_days: int = Field(default=7, ge=1, le=90)


class InviteOut(BaseModel):
    id: str
    email: str
    role: Role
    invited_by: str = ""
    expires_at: str = ""
    accepted: bool = False
    # The single-use token, returned once at creation so the caller can deliver
    # it however they like (Osprey does not send mail). Never re-readable.
    token: str | None = None


class InviteAccept(BaseModel):
    token: str = Field(min_length=16, max_length=512)
    password: str = Field(min_length=8, max_length=1024)
    full_name: str = Field(default="", max_length=200)


class MemberOut(BaseModel):
    user_id: str
    email: str
    full_name: str = ""
    role: Role
    is_active: bool = True
    scim_managed: bool = False
    last_login_at: str | None = None


class MemberRoleUpdate(BaseModel):
    role: Role


# ---- Projects -------------------------------------------------------------- #
class ProjectCreate(BaseModel):
    name: str


class ProjectOut(BaseModel):
    id: str
    name: str
    org_id: str
    weights: dict = {}


class WeightsUpdate(BaseModel):
    urgency: float | None = None
    impact: float | None = None
    confidence: float | None = None


# ---- Connections ----------------------------------------------------------- #
class ConnectionCreate(BaseModel):
    project_id: str
    source_type: str
    account_ref: str = ""
    tokens: dict = Field(default_factory=dict)  # sealed at rest immediately
    scopes: list[str] = Field(default_factory=list)


class ConnectionOut(BaseModel):
    id: str
    project_id: str
    source_type: str
    account_ref: str
    status: str
    scopes: list[str] = []
    last_sync: str | None = None


class ForwardEmail(BaseModel):
    """Forward-To / File-Drop ingestion payload."""

    raw: str  # RFC822 email or CSV text
    kind: str = "email"  # email | csv | doc
    external_id: str | None = None
    source_kind: str = "general"


class SourceInfo(BaseModel):
    source_type: str
    auth: str  # "oauth" | "forward" | "internal"
    scopes: list[str] = []
    configured: bool = True  # OAuth app credentials present on server


class ExchangeRequest(BaseModel):
    code: str
    state: str
    redirect_uri: str | None = None


# ---- AI connections + sift ------------------------------------------------- #
class AIConnectionCreate(BaseModel):
    provider: AIProvider = AIProvider.claude
    label: str = ""
    model: str = ""
    api_key: str = ""  # sealed at rest immediately; never returned
    base_url: str | None = None
    project_id: str | None = None


class AIConnectionOut(BaseModel):
    id: str
    provider: str
    label: str
    model: str
    status: str
    project_id: str | None = None
    has_key: bool = False


class SiftRequest(BaseModel):
    instruction: str = Field(min_length=3)
    ai_connection_id: str | None = None  # None => use server default provider
    lookback_days: int = 30
    max_signals: int = 200


class SiftFindingOut(BaseModel):
    item_id: str
    title: str
    category: str
    score: float
    bucket: str
    matched_signal_ids: list[str] = []


class SiftResponse(BaseModel):
    findings: list[SiftFindingOut]
    scanned_signals: int


# ---- Script tasks ---------------------------------------------------------- #
class ScriptCreate(BaseModel):
    name: str
    source_code: str
    enabled: bool = True
    schedule_minutes: int = 0
    timeout_seconds: int = 30


class ScriptOut(BaseModel):
    id: str
    name: str
    enabled: bool
    schedule_minutes: int
    status: str
    last_run: str | None = None
    last_result: dict = {}


class ScriptRunResult(BaseModel):
    status: str
    emitted: int
    created: int
    logs: list[str] = []
    error: str | None = None


# ---- Items / actions ------------------------------------------------------- #
class ActionRequest(BaseModel):
    type: ActionType
    meta: dict = Field(default_factory=dict)


class SignalOut(BaseModel):
    id: str
    source_type: str
    source_kind: str
    title: str
    url: str | None = None
    occurred_at: str | None = None


class ItemOut(BaseModel):
    id: str
    title: str
    category: str
    summary: str
    status: str
    owner: str | None = None
    score: float | None = None
    bucket: str | None = None


# ---- Governance ------------------------------------------------------------ #
class RetentionPolicy(BaseModel):
    """Per-tenant retention overrides, in days. None inherits the deployment
    default; 0 means keep forever."""

    signal_days: int | None = Field(default=None, ge=0, le=3650)
    item_days: int | None = Field(default=None, ge=0, le=3650)


class RetentionOut(BaseModel):
    signal_days: int | None = None
    item_days: int | None = None
    effective_signal_days: int = 0
    effective_item_days: int = 0


class PurgePreview(BaseModel):
    """What a retention run would delete right now, without deleting it."""

    signals: int = 0
    items: int = 0
    scores: int = 0
    snapshots: int = 0
    cutoff_signal: str | None = None
    cutoff_item: str | None = None


class DeletionRequest(BaseModel):
    # Deleting a tenant is unrecoverable, so the caller must name it exactly.
    # A checkbox is not enough friction for an irreversible cross-table wipe.
    confirm_org_name: str


class DeletionStatus(BaseModel):
    org_id: str
    requested_at: str | None = None
    completed: bool = False
    deleted_rows: dict[str, int] = {}


# ---- SCIM tokens ----------------------------------------------------------- #
class ScimTokenCreate(BaseModel):
    name: str = Field(default="", max_length=120)
    max_role: Role = Role.pm


class ScimTokenOut(BaseModel):
    id: str
    name: str
    max_role: Role
    created_at: str = ""
    last_used_at: str | None = None
    revoked: bool = False
    #: Returned once, at creation.
    token: str | None = None
