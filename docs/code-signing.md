# Code signing

Osprey's Windows installer is currently **unsigned**. Windows SmartScreen shows
"Windows protected your PC" on first run, and the user has to click *More info →
Run anyway*. For a tool that asks to read a builder's email, that warning is the
single largest adoption barrier — it is exactly the moment a cautious user stops.

This page records the plan, so whoever picks it up does not have to re-do the
research.

## What *is* signed today

Three things already protect a download, and it is worth being precise about what
each one does, because they are often conflated:

| | What it proves | What it does not |
| --- | --- | --- |
| **Updater signature** (minisign, `TAURI_SIGNING_PRIVATE_KEY`) | every update an installed copy applies came from the holder of the private key | nothing at install time — the first download is not covered |
| **`SHA256SUMS.txt`** | the bytes you downloaded are the bytes that were published | nothing about *who* published them |
| **Build provenance** (`actions/attest-build-provenance`) | these artefacts were built by this workflow, from this commit, recorded in a public transparency log | nothing the OS consults — SmartScreen and Gatekeeper do not read it |

Only an Authenticode/Apple certificate removes the OS warning. That is what the
rest of this page is about.

The updater key is not optional. `tauri.conf.json` ships a committed public key
and sets `createUpdaterArtifacts`, so an installed copy verifies every update
against it. A release signed with a *different* key produces an app that can
never update again, and the only symptom is silence — which is why
`release.yml` refuses to build without the key rather than discovering it twenty
minutes in. Verify provenance with:

```bash
gh attestation verify Osprey_0.3.1_x64-setup.exe --repo ibuilder/osprey
```

## What changed in 2024, and why it matters

Extended Validation (EV) certificates used to grant an instant SmartScreen
reputation bypass. Microsoft removed that. An EV certificate and an
Organization Validation (OV) certificate now behave the same way: reputation is
earned by download volume over time, not bought up front.

The practical consequence is that the cheapest credible option is now as good as
the most expensive one. There is no reason to buy an EV certificate.

## Options

| Option | Cost | Notes |
| --- | --- | --- |
| **SignPath Foundation** | **Free** for OSS | Publisher shows as "SignPath Foundation", not your own name. Requires an application and an OSS licence. |
| Azure Artifact Signing | ~$10/month | Individuals limited to USA/Canada. Publisher is your own identity. |
| Commercial OV certificate | ~$200–400/year | Requires organization validation; hardware token or cloud HSM. |

**Recommendation: apply to SignPath Foundation.** It is free, it is designed for
exactly this case, and since the 2024 change its OV certificate gives the same
SmartScreen behaviour as anything costlier. The trade-off is the publisher name:
users see "SignPath Foundation" rather than a name of ours. For an open-source
project with no legal entity behind it, that is an honest description.

## Applying (needs a human)

Everything that can live in the repository is already here. What remains needs
you, because it involves your accounts:

1. **Turn on MFA** for your GitHub account. SignPath Foundation requires it for
   every team member, on GitHub and on SignPath.
2. **Check the policy page is live** at
   <https://ibuilder.github.io/osprey/code-signing-policy.html> (source:
   `docs/code-signing-policy.html`, linked from the site's nav and footer). The
   Foundation requires it to be reachable from the homepage.
3. **Apply** at <https://signpath.org/apply>. Useful answers:
   - **Repository** — <https://github.com/ibuilder/osprey>
   - **Licence** — AGPL-3.0-only for the backend (`LICENSE`), Apache-2.0 for the
     desktop client (`clients/desktop/package.json`). Both are OSI-approved and
     neither is also sold under a commercial licence.
   - **What the software does** — a self-hosted background agent that reads a
     construction team's own email and project-management sources and produces a
     prioritized, explainable hotlist of items needing attention.
   - **Build system** — GitHub Actions (`.github/workflows/release.yml`), building
     a Tauri 2 desktop bundle with a PyInstaller-frozen Python backend.
   - **What gets signed** — the Windows NSIS and MSI installers, and the bundled
     `osprey-backend.exe`. Only this project's own binaries.
   - **Code signing policy** — the URL above.

The certificate is issued to SignPath Foundation, so signed installers show
**SignPath Foundation** as the publisher, not a name of ours.

## Wiring it up once approved

The workflow side is already in `release.yml`, and **switched off** until two
secrets exist. Until then releases build exactly as before.

### In SignPath

1. Create a project (slug `osprey`, or set the repository variable
   `SIGNPATH_PROJECT_SLUG` to whatever you choose).
2. Link the predefined **GitHub.com** trusted build system to the project.
3. Add two **artifact configurations**, pasting the files from the repository:
   - slug `backend` — `.signpath/artifact-configurations/backend.xml`
   - slug `installers` — `.signpath/artifact-configurations/installers.xml`

   SignPath's editor validates the schema when you save. If it rejects anything,
   trust the editor, and update the file in the repository to match.
4. Create a **signing policy** (slug `release-signing`, or set
   `SIGNPATH_SIGNING_POLICY_SLUG`) that requires **manual approval**, with you as
   approver. The Foundation requires approval for every release.
5. Create an API token for a user with **submitter** permission.

### In GitHub

Settings → Secrets and variables → Actions:

| Name | Kind | Value |
| --- | --- | --- |
| `SIGNPATH_API_TOKEN` | secret | the submitter API token |
| `SIGNPATH_ORGANIZATION_ID` | secret | your SignPath organization ID |
| `SIGNPATH_PROJECT_SLUG` | variable | only if not `osprey` |
| `SIGNPATH_SIGNING_POLICY_SLUG` | variable | only if not `release-signing` |

```powershell
gh secret set SIGNPATH_API_TOKEN            # prompts; does not echo
gh secret set SIGNPATH_ORGANIZATION_ID
```

### What a release then does

On a **tag** build, the Windows leg:

1. freezes the backend and sends **only `osprey-backend.exe`** to SignPath, before
   Tauri bundles it. The rest of the PyInstaller directory is third-party runtime,
   which the Foundation does not sign for us. The spec embeds a version resource
   (`ProductName` Osprey, version from `tauri.conf.json`) because the artifact
   configuration enforces it;
2. builds and uploads the installers to the draft as before;
3. sends the NSIS and MSI installers to SignPath;
4. recomputes each updater `.sig` over the **signed** bytes and replaces the
   installer and `.sig` on the draft. This ordering is the part that must not
   change: signing rewrites the installer, so a `.sig` computed before signing
   makes every installed copy reject the update.

Then `release-integrity`, which runs after every platform has finished, copies the
published `.sig` values into `latest.json` (`scripts/sync_manifest_signatures.py`)
before `scripts/verify_release.py` checks, among other things, that the manifest
and the published signatures agree.

**Each release needs two approvals in SignPath** — backend, then installers — and
the Windows job waits up to two hours for each. Dry runs (`workflow_dispatch`) and
`unsigned` builds never submit anything, so they never page the approver.

SmartScreen warnings do not stop on the first signed release: its reputation for
a certificate builds with download volume.

## macOS

macOS is unsigned too, and Gatekeeper is stricter about it than SmartScreen: an
unsigned `.dmg` needs right-click → *Open* rather than a double-click.

Signing macOS needs a paid Apple Developer Program membership ($99/year) — there
is no free-for-open-source equivalent of SignPath here. Set `APPLE_CERTIFICATE`
(base64 PKCS#12), `APPLE_CERTIFICATE_PASSWORD` and `APPLE_SIGNING_IDENTITY` as
repository secrets, plus `APPLE_ID`, `APPLE_PASSWORD` and `APPLE_TEAM_ID` for
notarization, and the release workflow picks them up automatically.

Until then the build degrades gracefully rather than failing: a probe step in
`release.yml` tries the certificate import into a throwaway keychain, and only
exports the Apple variables when it succeeds. **The variables must be absent, not
empty** — the Tauri bundler checks whether `APPLE_CERTIFICATE` exists rather than
whether it has content, so setting it to `''` still sends it down the codesign
path and fails there.

## In the meantime

Until signing is in place, mitigations already applied:

- The backend is a PyInstaller **directory** build, not `--onefile`. Onefile's
  runtime self-extraction is a common antivirus heuristic trigger.
- **UPX is disabled** (`backend/packaging/osprey-backend.spec`). Packed
  executable sections read as obfuscation.

If a specific antivirus vendor flags a release, submit it as a false positive —
most vendors have a form for this and turn them around in a few days.

## Generating and storing the keys

See [`docs/signing-keys.md`](signing-keys.md) for the one-time key generation and
the exact secret names. Short version: the updater key is generated once, backed
up offline, and pasted into repository secrets — it never lives on a developer
machine after that, and it is never passed to a third-party action that could log
it.
