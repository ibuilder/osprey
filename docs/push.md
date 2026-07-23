# Push notifications

Osprey pushes when a new **🔴 Act today** item is scored, so the phone/desktop is
alerted without polling. The always-on monitoring stays in the backend; the client
is a viewer that receives pushes (SPEC §2, Phase 5).

## Registration

Clients register their platform token after login:

```
POST /devices   { "platform": "ios" | "android" | "web", "token": "<device-token>" }
GET  /devices
```

Tokens are scoped to the user and org.

## Delivery

`osprey.engine.notify.notify_critical(session, org_id, payload)` fans a snapshot's
critical items out to the org's devices via a pluggable `PushSender`:

| Platform | Sender |
|---|---|
| iOS | APNs (token-based, HTTP/2) |
| Android | FCM |
| Web/Desktop | Web Push (VAPID) |

The default `LoggingPushSender` records intent (safe offline default). Wire a real
sender in production with `osprey.engine.notify.set_sender(...)`. The `refresh_project`
worker calls `notify_critical` after building each snapshot, so pushes ride the normal
poll/score cycle.

## Contract

Each push carries:

```json
{ "title": "🔴 <what>", "body": "<recommended action>", "data": { "item_id": "...", "score": 88 } }
```

Clients deep-link on `data.item_id` to the item detail.
