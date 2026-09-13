# Signing keys — one-time setup

Everything here is done **once**, by a human, on a machine you trust. After that
the keys live in GitHub's secret store and an offline backup, and never on a
developer machine again.

Commands are given for **PowerShell on Windows** first, since that is where Osprey
is developed, with bash/macOS equivalents after. The two are not interchangeable:
PowerShell has no `<` input redirection, so the `gh secret set NAME < file` form
you will find in most guides fails outright here.

Two independent things, in priority order:

1. **The updater key** — required. Without it a release cannot be built, and a
   release signed with the *wrong* key breaks updates for everyone who already
   installed. Free, five minutes.
2. **Apple Developer certificate** — optional, $99/year, removes the macOS
   Gatekeeper warning.

Windows Authenticode is a third thing, and it is *not* a key you generate — it
comes from SignPath or a certificate authority. See
[code-signing.md](code-signing.md).

---

## 1. The updater key (required)

### Generate

```powershell
New-Item -ItemType Directory -Force "$env:USERPROFILE\.osprey" | Out-Null
cd C:\Server\osprey\clients\desktop
npm run tauri signer generate -- -w "$env:USERPROFILE\.osprey\updater.key"
```

<details><summary>bash / macOS</summary>

```bash
mkdir -p ~/.osprey
cd clients/desktop
npm run tauri signer generate -- -w ~/.osprey/updater.key
```
</details>

It asks for a password. **Use one** — the private key file alone is otherwise
enough to sign an update that every installed copy will accept and run. Put the
password in your password manager *now*, before continuing. There is no recovery.

Two files are written:

- `updater.key` — private. Never commit it, never paste it into a chat, never put
  it on a build machine except through the secret store.
- `updater.key.pub` — public. This one is meant to be published.

### Check the public key against the app

```powershell
Get-Content -Raw "$env:USERPROFILE\.osprey\updater.key.pub"
```

Compare with `plugins.updater.pubkey` in
`clients\desktop\src-tauri\tauri.conf.json`. If you are setting this up for the
first time, replace it with the new public key.

> **If Osprey has already published a release, do not replace it.** Installed
> copies verify against the key compiled into them. Changing it means every
> existing install stops accepting updates — silently — and the only fix is each
> user downloading a fresh installer by hand. If you have lost the private key,
> that is already your situation: say so in the release notes rather than letting
> people discover it.

Check before you change anything:

```powershell
gh release list --repo ibuilder/osprey --limit 5
```

### Put it in GitHub

Settings → Secrets and variables → Actions → **New repository secret**.

| Secret | Value |
| --- | --- |
| `TAURI_SIGNING_PRIVATE_KEY` | the **contents** of `updater.key` (the whole file, not the path) |
| `TAURI_SIGNING_PRIVATE_KEY_PASSWORD` | the password you chose |

From PowerShell — note `--body`, not `<`:

```powershell
gh secret set TAURI_SIGNING_PRIVATE_KEY `
  --body (Get-Content -Raw "$env:USERPROFILE\.osprey\updater.key")

gh secret set TAURI_SIGNING_PRIVATE_KEY_PASSWORD   # prompts; does not echo
```

`Get-Content -Raw` reads the file as-is and does not introduce carriage returns,
which matters: a CR smuggled into the secret makes the bundler reject the key with
an error that looks nothing like the cause. If you would rather not have the key
pass through a shell at all, paste it into the web UI instead — that is the safer
option and costs nothing but a browser tab.

<details><summary>bash / macOS</summary>

```bash
gh secret set TAURI_SIGNING_PRIVATE_KEY < ~/.osprey/updater.key
gh secret set TAURI_SIGNING_PRIVATE_KEY_PASSWORD
```
</details>

### Back it up offline

Two copies, two locations — printed, or on an encrypted USB stick in a drawer.
GitHub secrets are **write-only**: you cannot read one back. If the repository is
lost or the secret is overwritten and you have no backup, see the warning above
for what that costs.

### Verify

```powershell
gh workflow run release.yml --ref main
gh run watch
```

This is a genuine dry run: it builds every platform, signs nothing, publishes
nothing. Confirm the step **"The updater signing key is present"** passes. If the
secret is missing, the run fails in the first minute with a clear message instead
of twenty minutes in, inside the bundler, on every platform at once.

---

## 2. Apple Developer certificate (optional, $99/year)

macOS only. Without it the `.dmg` still builds and still works — the user
right-clicks → *Open* the first time instead of double-clicking.

**This section needs a Mac.** Generating and exporting the certificate goes
through Keychain Access; there is no Windows path. Everything after step 4 can be
done from anywhere.

You need a **Developer ID Application** certificate — *not* "Mac App
Distribution", which is for the App Store and will not work for a direct download.

1. Enrol at <https://developer.apple.com/programs/>.
2. Xcode → Settings → Accounts → Manage Certificates → **+** → Developer ID
   Application.
3. Export from Keychain Access as a `.p12` with a password.
4. Base64-encode it:

   ```bash
   base64 -i DeveloperID.p12 | pbcopy          # macOS
   ```

   ```powershell
   # Windows, if the .p12 was sent to you
   Set-Clipboard ([Convert]::ToBase64String([IO.File]::ReadAllBytes("DeveloperID.p12")))
   ```

5. Create an **app-specific password** at <https://appleid.apple.com> → Sign-In
   and Security → App-Specific Passwords. Notarization needs this; your real
   Apple ID password will not work.

| Secret | Value |
| --- | --- |
| `APPLE_CERTIFICATE` | the base64 from step 4 |
| `APPLE_CERTIFICATE_PASSWORD` | the `.p12` export password |
| `APPLE_SIGNING_IDENTITY` | e.g. `Developer ID Application: Your Name (TEAMID)` |
| `APPLE_ID` | your Apple ID email |
| `APPLE_PASSWORD` | the **app-specific** password from step 5 |
| `APPLE_TEAM_ID` | the 10-character team ID |

**Set all six or none, and leave them absent rather than empty.** The Tauri
bundler checks whether `APPLE_CERTIFICATE` *exists*, not whether it has content,
so an empty string still sends the build down the codesign path and fails there.
The probe step in `release.yml` guards a *malformed* certificate — it imports into
a throwaway keychain first and degrades to an unsigned macOS build with a warning
rather than failing the release — but it cannot rescue a half-configured set.

---

## Rotating or revoking

**Updater key** — not a routine operation. Rotating it strands every existing
install. If the key is compromised that is still the right move, but plan the
communication: publish a new installer and say plainly in the release notes that a
manual reinstall is required, and why.

**Apple certificate** — routine. Revoke in the Apple Developer portal, issue a new
one, update the secrets. Already-notarized builds keep working; Apple's
notarization ticket outlives the certificate.

## Checking the keys are actually working

You cannot read a GitHub secret back, so the only way to know the right key is in
there is to look at what it signed:

```powershell
python scripts\verify_release.py v0.2.1
```

That confirms every `.sig` was made by the key compiled into the app — not merely
by a valid key — which is the distinction that matters. Cutting a release runs it
automatically; see [runbooks/release.md](runbooks/release.md).

## What is not a key

`SHA256SUMS.txt` and build provenance need no secret at all. Provenance is signed
with the workflow's own OIDC identity, which is why it works on a fork and on a
first release with nothing configured. The `release-integrity` job publishes both
automatically — there is nothing to set up.
