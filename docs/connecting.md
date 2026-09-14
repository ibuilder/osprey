# Connecting real sources

Osprey authorizes every source **in your own browser, through the desktop app** —
you approve read-only access, and the tokens are sealed on your server. Nothing goes
through any third-party AI service.

This guide covers wiring up the OAuth apps (a one-time admin step) and testing
against provider sandboxes before going live. Never test against live production
data — use each provider's sandbox / test tenant.

## How the flow works

1. In the desktop app: **Connections → Connect** next to a source.
2. Your system browser opens the provider's consent screen (read-only scopes).
3. You approve; the provider redirects to a loopback address the app is listening on.
4. The app hands the one-time code to your Osprey backend, which exchanges it for
   tokens and seals them (AES-256-GCM). Tokens never touch the client.

The backend needs the OAuth **app credentials** for each source (below). These are
the *application's* identity, not any user's — set once by an admin.

## Microsoft 365 / Outlook (Microsoft Graph)

1. Entra admin center → **App registrations → New registration**.
2. Redirect URI (type *Mobile & desktop*): add `http://127.0.0.1` (the app appends
   the loopback port at runtime).
3. **API permissions** → Microsoft Graph → *Delegated* → `Mail.Read`,
   `Calendars.Read`, `offline_access`. Grant admin consent.
4. Copy the Application (client) ID and a client secret into:
   ```
   OSPREY_MSGRAPH_CLIENT_ID=...
   OSPREY_MSGRAPH_CLIENT_SECRET=...
   OSPREY_MSGRAPH_TENANT_ID=...     # or "common"
   ```
5. Test tenant: use a Microsoft 365 Developer tenant.

## Gmail / Google Calendar (Google Cloud)

1. Google Cloud Console → **APIs & Services → Credentials → OAuth client ID**
   (type *Desktop app*). Enable the Gmail API and Calendar API.
2. Scopes: `gmail.readonly`, `calendar.readonly`.
3. Set:
   ```
   OSPREY_GOOGLE_CLIENT_ID=...
   OSPREY_GOOGLE_CLIENT_SECRET=...
   ```
4. Test with a personal/test Google account before organization rollout.

## Procore

1. Procore Developer Portal → create an app → OAuth (Authorization Code + PKCE).
2. Redirect URI: `http://127.0.0.1` (loopback).
3. Use the **sandbox** company for testing. Subscribe webhooks to RFIs, submittals,
   change orders, observations, and invoices.
4. Set:
   ```
   OSPREY_PROCORE_CLIENT_ID=...
   OSPREY_PROCORE_CLIENT_SECRET=...
   ```

## Autodesk Construction Cloud (ACC Issues)

1. [APS Developer Portal](https://aps.autodesk.com/myapps) → create an app with the
   **Autodesk Construction Cloud API** enabled. Choose a **Desktop, Mobile,
   Single-Page App** type: Osprey uses authorization code with PKCE.
2. Callback URL: `http://127.0.0.1` (loopback).
3. An ACC **account admin** must add the app's client id under
   *Account Admin → Custom Integrations*, or every request is refused.
4. Set:
   ```
   OSPREY_ACC_CLIENT_ID=...
   OSPREY_ACC_CLIENT_SECRET=...      # optional for a PKCE public client
   ```
5. Connect from the desktop app, and set the connection's account to the **ACC
   project id** (with or without the `b.` prefix).

Osprey reads the project's issues with the read-only `data:read` scope. Drafts,
closed and void issues are skipped. An issue's assignee is an Autodesk user id, so
hotlist items show the issue's title and description rather than a name Osprey would
have to guess.

### Optional: webhooks (near-instant updates)

By default Osprey polls ACC every cycle. To get new and updated issues within
seconds instead, tick **Allow Osprey to register ACC webhooks (data:write)** before
pressing Connect. Autodesk requires the `data:write` scope to create a webhook, so this
is an explicit opt-in:

- Osprey uses `data:write` only to register its webhooks and their signing secret. It
  never edits project data.
- The person connecting must be a **Project Admin** on the ACC project, or Autodesk
  refuses to create the hooks.
- `OSPREY_PUBLIC_BASE_URL` must be reachable from the internet, because Autodesk calls it.

Once connected, the hourly renewal job registers a signing secret and hooks for
`issue.created-1.0` and `issue.updated-1.0`, and re-creates them if they disappear.
Every callback is verified against its `x-adsk-signature` before it does anything, and
a verified callback triggers a poll of the project. Polling continues as a safety net
either way. Without the opt-in, Osprey never requests `data:write` and never creates
a webhook.

## Sage Intacct (open receivables)

Osprey reads **Accounts Receivable invoices that still have money due** through the
Sage Intacct REST API, and ranks them by due date and outstanding amount. Paid
invoices are skipped. Nothing is ever written back.

1. Register a Sage Intacct REST API client for your company (Sage's developer
   documentation covers where your edition keeps this), and give it a **read-only**
   role covering Accounts Receivable. Note the client id and secret.
2. Create the connection with those credentials. They are sealed at rest
   immediately, like every connection token:
   ```
   POST /connections
   {"project_id": "...", "source_type": "sage-intacct",
    "tokens": {"client_id": "...", "client_secret": "...", "entity": "optional-entity-id"}}
   ```
   `entity` is only needed for a multi-entity company, to choose whose books to read.

Osprey exchanges the credentials for a token on each poll (`client_credentials`),
so there is no browser consent step for this source. AP bills are not read yet.

## File-Drop / Forward-To (no OAuth)

For any source without an API, forward email to Osprey or drop a CSV export — it
still lands on the hotlist. This is the universal fallback and needs no setup beyond
creating a `filedrop` connection.

## Argus Enterprise (export, no API)

Argus Enterprise has no generally available API, so Osprey reads its exports.

1. In Argus, export a **tenancy schedule** or **lease expiry** report as CSV.
2. Create an `argus` connection once: `POST /connections` with
   `{"project_id": "...", "source_type": "argus"}`.
3. Send each export to it: `POST /connections/{id}/forward` with
   `{"kind": "csv", "raw": "<the CSV text>"}`. Re-sending the same export ingests
   nothing twice.

Each lease becomes one hotlist item, built from:

- **the option notice deadline**, for each renewal, termination, extension or
  expansion option. It is scored as a contractual notice, the highest weight Osprey
  has, because a missed option notice forfeits the option. If the export has a notice
  *period* rather than a date, the deadline is computed back from the lease end
  (months by default; days when the column or value says so);
- **the lease expiration.**

Annual rent is the dollar exposure. Vacant rows are skipped.

Report layouts differ by Argus template, so columns are matched by name, ignoring
case and punctuation. It needs a tenant column and a lease-end or notice-date
column. The recognized spellings are in `COLUMN_ALIASES` in
`backend/osprey/connectors/argus/__init__.py`: add yours there if a template uses
another. An option with no notice date and no notice period is never guessed at; its
expiration item says to check the lease.

## Verifying

The connector network paths (token acquisition, delta pagination, list+get,
resource iteration) are covered by integration tests in
`backend/tests/test_integration_connectors.py`. Run them before wiring a real
tenant; then connect a sandbox account and confirm signals appear on the hotlist
within one poll cycle.
