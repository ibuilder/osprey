# Enterprise setup — teams, SSO, and provisioning

Osprey starts as a single-tenant, single-user install: `POST /auth/register`
creates an organization and makes you its owner. This document covers everything
after that — getting colleagues in, and handing user lifecycle to your identity
provider.

Three ways in, in increasing order of how much you want the IdP to own:

| | Who creates the account | Best for |
|---|---|---|
| **Invite** | an Osprey admin | small teams, contractors, anyone outside your directory |
| **SSO** | the user, on first sign-in (optional) | you already run an IdP and want one password |
| **SCIM** | your IdP, automatically | joiners/movers/leavers must be automatic |

SSO and SCIM compose: SCIM creates and deprovisions the accounts, SSO
authenticates them. That is the combination most enterprises want.

---

## 1. Invites

Osprey **sends no email.** An invite returns a single-use token once, and you
deliver it however your organization already delivers things. That keeps SMTP
credentials, bounce handling, and deliverability out of a self-hosted product
that would otherwise have to own all three.

```bash
curl -X POST https://osprey.example.com/orgs/current/invites \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"email": "sam@example.com", "role": "pm", "expires_days": 7}'
```

```json
{ "id": "…", "email": "sam@example.com", "role": "pm", "token": "vQ7…" }
```

That `token` is shown **once**; listing invites never returns it again. The
recipient redeems it, unauthenticated, and is signed in immediately:

```bash
curl -X POST https://osprey.example.com/invites/accept \
  -H "Content-Type: application/json" \
  -d '{"token": "vQ7…", "password": "…", "full_name": "Sam Ruiz"}'
```

### Roles

`viewer` < `pm` < `admin` < `owner`.

| Role | Can |
|---|---|
| viewer | read the hotlist and items |
| pm | act on items — done, snooze, dismiss, escalate, assign |
| admin | the above, plus manage connections, members, and invites |
| owner | the above, plus retention, erasure, export, and SCIM tokens |

Two rules the API enforces and will not let you talk it out of:

- **Nobody grants a role above their own.** An admin cannot mint an owner and then
  act through it. This applies to demotion too — you cannot demote someone you
  could not have promoted.
- **An org always keeps a live owner.** Removing, demoting, or deactivating the
  last active one returns 409. There is no endpoint that can repair an
  ownerless tenant, so the API will not create one.

Role changes take effect **immediately**, not when the token expires: the change
bumps the user's token version and their next request gets a 401 telling the
client to refresh.

---

## 2. Single sign-on (OIDC)

```bash
OSPREY_OIDC_ENABLED=true
OSPREY_OIDC_ISSUER=https://login.microsoftonline.com/<tenant-id>/v2.0
OSPREY_OIDC_CLIENT_ID=<application-id>
OSPREY_OIDC_CLIENT_SECRET=<client-secret>
# Your CLIENT's callback route -- not an Osprey API path.
OSPREY_OIDC_REDIRECT_URL=https://osprey.example.com/auth/callback
```

### How the redirect works, and why it is not an API path

Osprey's callback is `POST /auth/sso/callback` taking `{code, state}` as JSON.
The provider cannot redirect a browser to that — it issues a `GET` with query
parameters. So the redirect target is **your client**, which reads `code` and
`state` off its own query string and posts them to the API:

```
browser -> IdP -> GET https://osprey.example.com/auth/callback?code=…&state=…
                       (your web client)
your client -> POST /auth/sso/callback {code, state} -> Osprey session
```

The authorization code therefore never lands in an Osprey access log or in a
browser history entry pointing at the API.

**The desktop app needs none of this.** It binds an ephemeral loopback port per
sign-in, passes it on `POST /auth/sso/start`, and catches the redirect itself —
the native-app pattern from RFC 8252. Register `http://127.0.0.1` with your
provider as a **native/public client** redirect (providers allow any port on
loopback). The server accepts a client-supplied `redirect_uri` only when it is
the configured URL or a literal loopback address (`127.0.0.1` / `[::1]`);
`localhost` is refused because it resolves through host name resolution that
another process can influence. Whichever value is used on `/authorize` is sealed
into the signed state and reused for the token exchange, so the two cannot be
decoupled.

Osprey uses authorization-code + PKCE throughout, so a public-client
registration with no secret works.

Verified on every sign-in, all of it non-negotiable: the ID token's signature
against the issuer's published JWKS (looked up by `kid`, re-fetched on rotation),
`iss`, `aud`, `exp`, the `nonce` bound to our own signed state, and
`email_verified`. Only asymmetric algorithms are accepted — an `HS256` ID token
would verify against the client secret, which would make anyone holding that
secret an issuer.

### Who is allowed in

By default an SSO user must **already have an Osprey account** — invited, or
provisioned by SCIM. SSO then only replaces the password.

Auto-provisioning creates accounts on first sign-in:

```bash
OSPREY_OIDC_AUTO_PROVISION=true
OSPREY_OIDC_DEFAULT_ORG_ID=<org id>
OSPREY_OIDC_DEFAULT_ROLE=viewer
```

**Turn this on only when your IdP is already scoped to exactly the people who
should have access.** With it on, anyone your provider will authenticate gets an
account. If the issuer is multi-tenant — Microsoft's `common` endpoint, for
instance — "verified by the IdP" means very little, so constrain it:

```bash
OSPREY_OIDC_ALLOWED_EMAIL_DOMAINS='["example.com","example.co.uk"]'
```

### Provider notes

- **Entra ID** — issuer `https://login.microsoftonline.com/<tenant>/v2.0`. Add the
  `email` optional claim to the ID token, or Entra omits it and every sign-in is
  refused for having no address.
- **Okta** — issuer `https://<org>.okta.com` (or `/oauth2/<server-id>`). Its
  default authorization server emits `email_verified`.
- **Google Workspace** — issuer `https://accounts.google.com`. Always set
  `OSPREY_OIDC_ALLOWED_EMAIL_DOMAINS`; Google will happily authenticate any
  consumer Gmail account.

---

## 3. SCIM 2.0 provisioning

Turn it on and mint a token per IdP connector:

```bash
OSPREY_SCIM_ENABLED=true
```

```bash
curl -X POST https://osprey.example.com/orgs/current/scim-tokens \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"name": "Okta production", "max_role": "pm"}'
```

The response carries the secret **once**. Configure your IdP with:

- **Base URL** — `https://osprey.example.com/scim/v2`
- **Auth** — HTTP header, `Authorization: Bearer osp_scim_…`

`max_role` is a ceiling, not a default-with-override: a token can never grant
above it, and it may never be `owner`. A leaked provisioning credential that
could mint owners is a full tenant takeover.

### What is implemented

`/Users` — create, read, list (with `userName eq "…"` filtering), replace,
patch, delete. `/ServiceProviderConfig` advertises exactly this.

`/Groups` returns **501**, deliberately, rather than 404 — so a connector probing
for it gets an honest answer instead of "wrong URL". Okta, Entra, and JumpCloud
all drive full user lifecycle through `/Users` alone. Roles come from the SCIM
`roles` attribute; an unmappable value falls back to the token's ceiling rather
than guessing, because a wrong guess about role assignment is a privilege bug.

### Deprovisioning

Both `active: false` and `DELETE` **disable** the account rather than erasing it:
sessions are revoked immediately and live access tokens stop working, but the row
survives so the audit trail still resolves who did what. To erase a person
entirely, use the [right-to-delete flow](operations.md#4-data-retention-and-erasure).

A SCIM-managed user cannot have their role changed through Osprey's own member
API — the next sync would overwrite it. Change it in the IdP.

---

## 4. Sessions

Sign-in returns a short-lived access token and an opaque refresh token. Clients
call `POST /auth/refresh` when the access token expires; refresh tokens are
**single-use and rotate**.

Presenting one twice is treated as theft — not a retry — and revokes every token
descended from that login. This is the standard reuse-detection response, and it
means a client must not send the same refresh token from two requests at once.
The bundled desktop client shares one in-flight exchange for exactly this reason.

| Endpoint | Effect |
|---|---|
| `GET /auth/sessions` | list your live sessions, with device and IP |
| `DELETE /auth/sessions/{id}` | end one |
| `POST /auth/logout` | end the current one |
| `POST /auth/logout-all` | end all, and invalidate access tokens already in flight |
| `POST /auth/password` | change password; ends every other session |

Signing someone out is immediate everywhere. Every access token carries the
user's token version and it is checked on each request, so deactivation,
demotion, removal, and password change all take effect on the next call rather
than whenever the token happened to expire.

---

## 5. Multi-tenancy

One Osprey instance can host many organizations. Isolation is enforced twice:
every query is scoped by `org_id` in application code, and Postgres row-level
security enforces the same boundary in the database, so a query that forgets its
filter still cannot cross tenants.

RLS is **off by default** because it only works if the app connects as an
ordinary role — see [operations.md §1](operations.md#tenant-isolation). Verify it
took effect rather than assuming:

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  https://osprey.example.com/admin/security/tenant-isolation
```

`{"enforced": true}` is the only acceptable answer.

Both SSO and SCIM are scoped to one tenant at a time: a SCIM token belongs to one
org, and `OSPREY_OIDC_DEFAULT_ORG_ID` names one. Running several tenants that
each need their own IdP means running an instance per tenant.
