# Code signing

Osprey's Windows installer is currently **unsigned**. Windows SmartScreen shows
"Windows protected your PC" on first run, and the user has to click *More info →
Run anyway*. For a tool that asks to read a builder's email, that warning is the
single largest adoption barrier — it is exactly the moment a cautious user stops.

This page records the plan, so whoever picks it up does not have to re-do the
research.

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

Apply at <https://signpath.org/apply>. They ask for:

- **Repository** — <https://github.com/ibuilder/osprey>
- **Licence** — Apache-2.0 (see `LICENSE`)
- **What the software does** — a self-hosted background agent that reads a
  construction team's own email and project-management sources and produces a
  prioritized, explainable hotlist of items needing attention.
- **Build system** — GitHub Actions (`.github/workflows/release.yml`), building
  a Tauri 2 desktop bundle with a PyInstaller-frozen Python backend.
- **Reproducibility** — the release workflow builds from a tag with pinned
  dependencies (`backend/constraints.txt`, `Cargo.lock`, `package-lock.json`).

SignPath requires that signing happen in CI from an unmodified public build, not
on a developer machine. The release workflow already meets that.

## Wiring it up once approved

SignPath provides a GitHub Action that submits the built artifact for signing.
It slots in **after** `tauri-action` produces the installer and **before** the
release assets are uploaded — the installer must be signed, and the updater
signature computed over the signed file, or the updater will reject it.

Two things to get right:

1. **Sign before computing the updater signature.** Tauri's updater hashes the
   artifact; signing changes the bytes.
2. **The `.exe` inside the bundle also matters.** The frozen backend
   (`osprey-backend.exe`) ships as a bundled resource. Signing only the
   installer leaves an unsigned executable that antivirus can still flag, so
   sign the backend before Tauri bundles it.

## In the meantime

Until signing is in place, mitigations already applied:

- The backend is a PyInstaller **directory** build, not `--onefile`. Onefile's
  runtime self-extraction is a common antivirus heuristic trigger.
- **UPX is disabled** (`backend/packaging/osprey-backend.spec`). Packed
  executable sections read as obfuscation.

If a specific antivirus vendor flags a release, submit it as a false positive —
most vendors have a form for this and turn them around in a few days.
