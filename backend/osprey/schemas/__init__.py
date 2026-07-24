"""Pydantic DTOs for the REST API."""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..models import ActionType, AIProvider, Role


# ---- Auth ------------------------------------------------------------------ #
class RegisterRequest(BaseModel):
    email: str
    password: str = Field(min_length=8)
    full_name: str = ""
    org_name: str = "My Org"


class LoginRequest(BaseModel):
    email: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: Role
    org_id: str
    user_id: str


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
