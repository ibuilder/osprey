# Osprey Mobile (Tauri 2.0 — iOS / Android)

Mobile is a **viewer + push**, never an always-on monitor: iOS and Android forcibly
kill long-running background processes, so the continuous monitoring stays in the
backend (SPEC §2). The phone shows the hotlist and receives a push when a 🔴 *Act
today* item appears.

## It's the same project as desktop

Tauri 2 targets mobile from the **same** codebase as [`../desktop`](../desktop). Rather
than duplicate the frontend, add the mobile targets to the desktop project:

```bash
cd ../desktop
npm install
npm run tauri android init      # or: npm run tauri ios init
npm run tauri android dev       # or: ios
```

On mobile, the connections `Connect` flow uses the OS in-app browser / ASWebAuthenticationSession
for the same OAuth loopback; the write actions (escalate/done, script authoring) are
hidden — mobile defaults to read-only viewing.

## Push notifications

- **iOS:** APNs. **Android:** FCM. Register the device token on login:
  `POST /devices` `{platform, token}` (add this endpoint when wiring push).
- The backend `notify` worker publishes a push when a new `act_today` item is scored.
  See `backend/osprey/workers/` and `docs/push.md` for the delivery contract.

This directory holds mobile-specific config, store metadata, and push entitlements
once `tauri <android|ios> init` has generated the platform projects.
