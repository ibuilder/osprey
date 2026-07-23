# Osprey Desktop (Tauri 2.0)

The always-on desktop surface: system-tray presence, a **live hotlist** (WebSocket),
and the **connections manager** that authorizes each source in the user's own browser.

## Why connectors are authorized here (not via any AI/MCP layer)

Connecting a source (Outlook, Gmail, Procore, …) runs an OAuth2 authorization-code
flow **initiated by the user in their system browser**:

1. The UI calls the Rust command `oauth_connect(source_type, project_id)`.
2. The Rust shell binds a loopback listener (`http://127.0.0.1:<port>/callback`),
   asks the backend for the provider consent URL (`POST /connections/authorize`,
   with PKCE + a signed state), and opens it in the browser.
3. The user consents; the provider redirects to the loopback with a `code`.
4. The shell relays only the `code` to the backend (`POST /connections/exchange`),
   which performs the confidential token exchange and **seals the tokens server-side**.

Provider tokens never touch the client or any AI/MCP tool. See
`src-tauri/src/lib.rs` (`oauth_connect`) and backend `osprey/api/connections.py`.

## Features

- **Hotlist** — live via `ws://…/ws/projects/{id}/hotlist`; refresh; export Excel/PDF; escalate/done.
- **Connections** — one-click OAuth connect per source; connection health.
- **AI** — connect your own Claude/OpenAI/Ollama key and run natural-language *sift → hotlist*.
- **Scripts** — author Python background scripts that emit signals into the hotlist.

## Develop

```bash
npm install
npm run tauri dev        # requires the Rust toolchain (rustup) + Tauri prereqs
```

Frontend only (no Rust): `npm run dev` then open http://localhost:1420 — the OAuth
`Connect` buttons need the Rust shell, but every other screen works against a running
backend (`uvicorn osprey.main:app`).

## Build

```bash
npm run tauri build      # produces signed installers per OS
```

## Icons

Place brand icons in `src-tauri/icons/` (generate from `osprey-brand/logo/osprey-icon.svg`):

```bash
npm run tauri icon ../../osprey-brand/logo/osprey-icon.png
```

This emits `32x32.png`, `128x128.png`, `icon.icns`, `icon.ico`, and `tray.png`.

## Releasing (signed bundles + auto-update)

Push a `v*` tag and `.github/workflows/release.yml` builds signed bundles for
win/mac/linux via `tauri-action` and drafts a GitHub Release. Auto-update is wired:
`bundle.createUpdaterArtifacts` + `plugins.updater` (public key embedded in
`tauri.conf.json`) let the app verify and install updates from the latest release.

The **updater signing keypair** is required by CI. It is stored as repo secrets
(already set for this repo):

```bash
# Generate a keypair (once) — keep the private key OFF the repo:
npm run tauri signer generate -w osprey_updater.key -p ""

# Register it with GitHub Actions:
gh secret set TAURI_SIGNING_PRIVATE_KEY < osprey_updater.key
gh secret set TAURI_SIGNING_PRIVATE_KEY_PASSWORD --body ""   # empty if no password
```

The **public** key lives in `tauri.conf.json` → `plugins.updater.pubkey`. If you lose
the private key you must generate a new pair and re-embed the new public key (old
installs won't accept updates signed by a new key). For macOS notarization also set
`APPLE_*` secrets (see the workflow).

## Mobile (iOS / Android)

Tauri 2 builds the **same** project for mobile — the app is a **viewer + push**
(the always-on monitor stays the backend). See [`../mobile/README.md`](../mobile/README.md).
