"""Single sign-on routes — OIDC authorization-code flow.

Three endpoints:

``GET  /auth/sso/config``   what the client should render (is SSO on, what to call it)
``POST /auth/sso/start``    returns the provider URL to open in a browser
``POST /auth/sso/callback`` exchanges the returned code for an Osprey session

The callback is a POST taking ``{code, state}`` rather than the GET redirect
target itself, for the same reason the connector OAuth flow is shaped this way:
the desktop app catches the browser redirect on its loopback listener and relays
the parameters, so an authorization code never lands in a server access log or a
browser history entry pointed at Osprey.
"""

from __future__ import annotations

import logging
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..models import Membership, Org, Role, User, utcnow
from ..schemas import TokenResponse
from ..security import audit, oidc, sessions
from ..security.auth import Principal, create_access_token
from ..security.oauth import make_pkce, sign_state, verify_state
from ..security.rls import set_current_org
from .deps import client_address, db_session

log = logging.getLogger("osprey.sso")

router = APIRouter(prefix="/auth/sso", tags=["auth"])

_STATE_PURPOSE = "sso-login"


class SSOConfig(BaseModel):
    enabled: bool
    issuer: str = ""
    button_label: str = "Sign in with SSO"
    # True when an unknown but IdP-verified user is created on first sign-in.
    auto_provision: bool = False


class SSOStartRequest(BaseModel):
    """Optional loopback redirect for a native client.

    Omitted by browser clients, which are served from the deployment's configured
    redirect origin. A desktop app binds an ephemeral port and passes it here;
    ``oidc.allowed_redirect_uri`` decides whether it is acceptable.
    """

    redirect_uri: str | None = None


class SSOStart(BaseModel):
    authorize_url: str
    state: str
    #: Echoed back so the client can confirm the server honoured its loopback.
    redirect_uri: str


class SSOCallback(BaseModel):
    code: str
    state: str


@router.get("/config", response_model=SSOConfig)
async def sso_config() -> SSOConfig:
    """Public: lets the sign-in screen decide whether to show an SSO button."""
    return SSOConfig(
        enabled=oidc.is_configured(),
        issuer=settings.oidc_issuer if oidc.is_configured() else "",
        auto_provision=settings.oidc_auto_provision,
    )


@router.post("/start", response_model=SSOStart)
async def sso_start(body: SSOStartRequest | None = None) -> SSOStart:
    """Begin the flow. The nonce, PKCE verifier and redirect URI ride inside the
    signed state, so the server keeps no pending-authorization table and the
    callback cannot be talked into using a different redirect than /authorize did."""
    body = body or SSOStartRequest()
    # "SSO is not configured here" is a 503 about the server; "your redirect_uri is
    # not acceptable" is a 400 about the request. Resolving the redirect first
    # would report the former as the latter.
    if not oidc.is_configured():
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "single sign-on is not configured on this server",
        )
    try:
        redirect_uri = oidc.allowed_redirect_uri(body.redirect_uri)
    except oidc.OIDCError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    try:
        verifier, challenge = make_pkce()
        nonce = secrets.token_urlsafe(24)
        state = sign_state(
            {
                "purpose": _STATE_PURPOSE,
                "verifier": verifier,
                "nonce": nonce,
                "redirect_uri": redirect_uri,
            }
        )
        url = await oidc.build_authorize_url(
            state=state, nonce=nonce, code_challenge=challenge, redirect_uri=redirect_uri
        )
    except oidc.OIDCError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    return SSOStart(authorize_url=url, state=state, redirect_uri=redirect_uri)


@router.post("/callback", response_model=TokenResponse)
async def sso_callback(
    body: SSOCallback,
    request: Request,
    session: AsyncSession = Depends(db_session),
) -> TokenResponse:
    try:
        state = verify_state(body.state)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "sign-in state is invalid or expired"
        ) from exc
    # A state minted for connector authorization must not be redeemable for a
    # login session; the purpose claim is what keeps the two flows apart.
    if state.get("purpose") != _STATE_PURPOSE:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "sign-in state is invalid")

    try:
        tokens = await oidc.exchange_code(
            code=body.code,
            code_verifier=state["verifier"],
            # From the state, never the request: the provider requires it to match
            # what /authorize sent, and taking it from the caller here would let
            # them decouple the two.
            redirect_uri=state.get("redirect_uri"),
        )
        claims = await oidc.verify_id_token(tokens["id_token"], nonce=state["nonce"])
    except oidc.OIDCError as exc:
        log.warning("SSO sign-in rejected: %s", exc)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc

    user, membership = await _resolve_user(session, claims)

    user.last_login_at = utcnow()
    user.failed_login_count = 0
    user.locked_until = None
    if claims.full_name and not user.full_name:
        user.full_name = claims.full_name
    session.add(user)

    await set_current_org(session, membership.org_id)
    principal = Principal(
        user_id=user.id,
        org_id=membership.org_id,
        role=membership.role,
        email=user.email,
        token_version=user.token_version,
    )
    refresh, _ = await sessions.issue(
        session,
        user=user,
        org_id=membership.org_id,
        user_agent=request.headers.get("user-agent", ""),
        ip=client_address(request),
    )
    await audit.record(
        session,
        org_id=membership.org_id,
        actor=user.email,
        action="auth.sso_login",
        target=user.id,
        meta={"issuer": settings.oidc_issuer},
    )
    return TokenResponse(
        access_token=create_access_token(principal),
        refresh_token=refresh,
        expires_in=settings.access_token_ttl_minutes * 60,
        role=membership.role,
        org_id=membership.org_id,
        user_id=user.id,
    )


async def _resolve_user(session: AsyncSession, claims: oidc.OIDCClaims) -> tuple[User, Membership]:
    """Find (or provision) the Osprey account behind a verified SSO identity.

    Matching order is subject first, then email. The subject is stable across a
    mailbox rename; email is the fallback that links an SSO identity to an
    account created earlier by invite or by SCIM.
    """
    user = (
        await session.execute(select(User).where(User.sso_subject == claims.subject))
    ).scalar_one_or_none()
    if user is None:
        user = (
            await session.execute(select(User).where(func.lower(User.email) == claims.email))
        ).scalar_one_or_none()
        if user is not None and not user.sso_subject:
            # First SSO sign-in for an account that already existed. Bind the
            # subject now so a later rename does not orphan it.
            user.sso_subject = claims.subject

    if user is not None and not user.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "this account is disabled")

    if user is not None:
        membership = (
            (await session.execute(select(Membership).where(Membership.user_id == user.id)))
            .scalars()
            .first()
        )
        if membership is not None:
            return user, membership
        if not settings.oidc_auto_provision:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "your account has no organization; ask an administrator to invite you",
            )

    if not settings.oidc_auto_provision:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "no Osprey account matches this identity; ask an administrator to invite you",
        )

    org_id = settings.oidc_default_org_id
    if not org_id:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "auto-provisioning is on but OSPREY_OIDC_DEFAULT_ORG_ID is not set",
        )
    org = await session.get(Org, org_id)
    if org is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "OSPREY_OIDC_DEFAULT_ORG_ID does not name an existing organization",
        )

    await set_current_org(session, org_id)
    if user is None:
        user = User(
            email=claims.email,
            full_name=claims.full_name,
            password_hash="",  # SSO-only: no local credential exists to steal
            sso_subject=claims.subject,
        )
        session.add(user)
        await session.flush()

    membership = Membership(org_id=org_id, user_id=user.id, role=Role(settings.oidc_default_role))
    session.add(membership)
    await session.flush()
    log.info("provisioned SSO user into org %s with role %s", org_id, membership.role.value)
    return user, membership
