# Runbook — cutting a release

Commands are PowerShell, since that is where Osprey is developed.

A release is the one operation here that cannot be undone. Deleting or rewriting a
published release breaks the download for anyone mid-transfer and invalidates
signatures they have already fetched, so the order below is deliberate: everything
that can fail, fails before anything is published.

---

## Before you tag

### 1. Bump the version, in both places

```powershell
# clients\desktop\src-tauri\tauri.conf.json   -> "version"
# clients\desktop\package.json                -> "version"
```

**This is the step that has actually gone wrong.** Tag `v0.2.0` shipped
`Osprey_0.1.0_*` binaries because `tauri.conf.json` was never bumped. The manifest
was internally consistent, every signature was valid, and nothing noticed for
months — the release simply contained the wrong software. `scripts/verify_release.py`
now catches it, but it catches it *after* the build; catching it here is free.

```powershell
Select-String -Path clients\desktop\src-tauri\tauri.conf.json,clients\desktop\package.json -Pattern '"version"'
```

Both must agree, and must match the tag you are about to push.

### 2. The gates CI runs anyway

```powershell
cd backend
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check osprey tests
.\.venv\Scripts\python.exe -m ruff format --check osprey tests
.\.venv\Scripts\python.exe -m mypy osprey
.\.venv\Scripts\python.exe tools\regen_constraints.py --check
```

### 3. Dry-run the release workflow

```powershell
gh workflow run release.yml --ref main
gh run watch
```

This builds every platform, signs nothing, publishes nothing. **Do this before
every tag.** `release.yml` has shipped broken twice — v0.2.0's CSP/CORS, and
v0.2.1 producing no macOS artifacts at all, which `fail-fast: false` hid because
the passing jobs still created the release. A half-failed run looks like a release.

---

## Tag and publish

```powershell
git tag v0.3.0
git push origin v0.3.0
gh run watch
```

The workflow then:

1. builds and signs bundles for Windows, macOS (arm64 + Intel) and Linux;
2. opens a **draft** release;
3. runs `scripts/verify_release.py` — blocking, before anything is hashed;
4. publishes `SHA256SUMS.txt` and signed build provenance;
5. pushes the backend image and moves `:latest`.

Step 3 is before step 4 on purpose: a release that fails verification never gets a
checksum file or an attestation vouching for it, and the draft stays unpublished.

### Then check the draft by hand

```powershell
python scripts\verify_release.py v0.3.0
gh release view v0.3.0 --repo ibuilder/osprey
```

The verifier confirms what the releases page cannot: that each `.sig` was made by
the key this application *actually trusts* — not merely by a valid key — that
`latest.json` names this version, and that every installer has a signature beside
it. A release signed with the wrong key produces installers that look perfect and
an installed base that can never update again. The only symptom is silence.

Then, manually, because nothing in CI does it:

- **Install one.** CI builds installers; it never installs one. Run the Windows
  `.exe` on a machine that has never had Osprey, and confirm the app starts and
  finds its bundled backend. SmartScreen will warn — that is expected and
  unrelated (see [code-signing.md](../code-signing.md)).
- Confirm macOS artifacts are present at all. Their absence is silent.

Publish the draft only after both.

---

## If something is wrong

**Do not delete or rewrite a published release.** Anyone mid-download gets a
signature mismatch, and anyone who already has `latest.json` keeps pointing at
assets that no longer exist.

Roll forward instead: fix the problem, bump the version, tag again. A higher
version containing older code is a valid rollback; a rewritten release is not.

A **draft** is different — nothing has consumed it, so deleting and re-running is
fine. That is why the workflow drafts rather than publishes.

---

## Signing material

| Secret | What it is |
| --- | --- |
| `TAURI_SIGNING_PRIVATE_KEY` | the updater private key, as a string |
| `TAURI_SIGNING_PRIVATE_KEY_PASSWORD` | its password |

It exists in exactly two places: the CI secret store, and an offline backup. Not
on a developer machine. See [signing-keys.md](../signing-keys.md) for setup, and
for why rotating it is not a routine operation.

`release.yml` refuses to build without it rather than discovering the problem
twenty minutes in. Worth knowing the failure mode that guard exists for: a build
that runs without the key writes *new installers* and can leave a previous run's
`.sig` files beside them, so uploading the pair by hand ships a signature over
bytes nobody has any more.

---

## What is still not covered

- **No installer is ever installed by CI.** The manual check above is the only one.
- **Windows Authenticode and Apple notarization.** Updates are signed; the binaries
  are not. SmartScreen and Gatekeeper warn on first run. See
  [code-signing.md](../code-signing.md).
- **Reproducible builds**, and signing the SBOM.
- The verifier checks signature *provenance* (which key), not that the signature is
  cryptographically valid over the installer bytes — that needs the installers and
  the `minisign` binary. The key-id check is what catches the failure modes above.
