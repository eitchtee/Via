# Via API (v1)

The complete, always-current reference is the OpenAPI schema served by every server at
`/openapi.json` (browsable at `/docs`). This page explains the concepts; for a step-by-step
guide to building a client, see [clients.md](clients.md).

## Basics

- Base path: `/v1`. JSON in and out, except file uploads and downloads (raw bytes).
- Auth: `Authorization: Bearer <token>`. Token types, recognizable by prefix:

  | Prefix | Token | Obtained from | Can |
  |---|---|---|---|
  | `via_s_` | session | `POST /v1/auth/login` | manage the account, devices, contacts and app tokens; send |
  | `via_d_` | device | `POST /v1/devices` (shown once) | read and ack **its own** inbox; send; change its own name, share setting and push registration |
  | `via_a_` | app | `POST /v1/app-tokens` (shown once) | send only |

  No token can read another device's inbox, and a session (password) can't read any.
- Errors always look like `{"error": {"code": "not_found", "message": "Push not found"}}`.
  Switch on `code`, not on `message`. Validation errors add `details`.
- Ids are ULIDs (26 chars, sortable by creation time). Timestamps are ISO 8601 UTC.
- `GET /v1/info` returns the server version, limits and enabled `features` (`sse`,
  `long_poll`, `unifiedpush`, `fcm_relay`, `history`, `contacts`, `web_ui`). Check it at startup.

## Receiving

The contract: **fetch → (download) → ack**. Items stay in the inbox until acked, so a crash
or a failed download never loses anything. Be idempotent on item `id`: if an ack gets lost,
you'll see the item again.

```
GET  /v1/inbox?limit=50&after=<cursor>&wait=<0-60>
GET  /v1/inbox/{id}
GET  /v1/inbox/{id}/file          (supports Range; verify with Repr-Digest or X-Via-SHA256)
POST /v1/inbox/{id}/ack
POST /v1/inbox/ack                {"ids": [...]}
```

An inbox item:

```json
{
  "id": "01J9Z3K2Q8V6T4R2N0M8K6H4G2",
  "kind": "file",
  "created_at": "2026-09-26T06:16:35.764955Z",
  "expires_at": "2026-10-03T06:16:35.764955Z",
  "source_device_id": "01J9Z3JX0A2B4C6D8E0F2G4H6J",
  "sender": null,
  "title": "Q3 report",
  "body": null,
  "url": null,
  "file": {"name": "report.pdf", "mime": "application/pdf", "size": 300000,
           "sha256": "c2dc…", "available": true}
}
```

- `kind` is `link` (has `url`; clients may open it right away), `note` (has `body` and/or
  `title`) or `file` (has `file`; download it from `/v1/inbox/{id}/file`).
- `sender` is the username of the **contact** who shared the item, or `null` for your own.
  Clients should make shared items visually distinct and may choose not to auto-open links
  from contacts.
- `source_device_id` is your own device that sent it (`null` when sent from the web UI, an
  app token, or a contact).

### Knowing when to fetch

Pick what suits the platform. All of these only tell you to fetch; none carries content.

- **Server-sent events** (desktop, browser extension, foreground mobile):
  `GET /v1/inbox/events` with the device token. Events:
  - `ready`: connected. Fetch the inbox now to catch up.
  - `push` `{"id", "kind"}`: something arrived.
  - `recalled` `{"id"}`: the sender took an item back. Drop it if you haven't handled it.
  - `revoked`: this device was removed. The token no longer works.

  A `: ping` comment arrives every ~25 s. Reconnect with backoff when the stream drops.
  Browsers' `EventSource` can't send headers, so use `fetch()` with a streaming body instead.
- **Long-poll**: `GET /v1/inbox?wait=30` returns as soon as something arrives, or empty after
  30 s. Loop.
- **UnifiedPush** (Android without Google services): register with a distributor, then
  `PUT /v1/devices/me/push` `{"provider": "unifiedpush", "endpoint": "<url>"}`. Via POSTs
  `{"t": "wake", "id": "<push id>"}` to the endpoint.
- **FCM** (official Android app): `PUT /v1/devices/me/push`
  `{"provider": "fcm_relay", "token": "<fcm token>"}`. The server asks the Via FCM relay to
  send a data-only message `{"t": "wake", "id": "<push id>"}`. Check `features.fcm_relay`
  first.
- **Polling**: always works. Also fetch on app start and on network changes.

`{"provider": "none"}` turns wake-ups off. If an endpoint or FCM token is reported gone, the
server resets the device to `none`, so re-register when the app gets a new token.

## Sending

```
POST /v1/pushes        {"kind": "link", "url": "https://…", "title": "…", "to": "all"}
POST /v1/pushes        {"kind": "note", "body": "…", "title": "…", "to": ["<device id>", "@bob"]}
POST /v1/pushes/file?filename=report.pdf&to=all&title=…     (body = raw file bytes)
```

- `to`:
  - `"all"`: every device of your account except the sending device;
  - or a list mixing your device ids (from `GET /v1/devices`) and `@username` entries for
    contacts (from `GET /v1/contacts`). In query strings: `to=all` or `to=<id>,@bob`.

  Targets are fixed at send time. Errors: `unknown_device`, `unknown_contact`,
  `contact_unreachable` (the contact has no device accepting shares), `no_targets` (422).
- `ttl` (optional): seconds or `"1h"`, `"2d"`. Capped at the server's `max_ttl`.
- File uploads stream the raw body; `Content-Type` is stored as the MIME type. Errors:
  `file_too_large`, `quota_exceeded` (413).
- The response is the push's status (below).

Shortcut for scripts (`/v1/send`, POST or PUT, any token):

```sh
curl -H "Authorization: Bearer $TOKEN" -d "https://example.com" $VIA/v1/send          # link
curl -H "Authorization: Bearer $TOKEN" -d "text" "$VIA/v1/send?title=Hi&to=@bob"      # note
curl -H "Authorization: Bearer $TOKEN" -T file.pdf $VIA/v1/send/                       # file
```

### Status and recall

```
GET    /v1/pushes/sent?limit=50&before=<id>
GET    /v1/pushes/{id}
DELETE /v1/pushes/{id}             recall: pending deliveries expire, content is deleted
```

```json
{
  "id": "…", "kind": "note", "created_at": "…", "expires_at": "…",
  "purged_at": null, "source_device_id": "…",
  "deliveries": [
    {"device_id": "…", "recipient": null, "state": "acked", "delivered_at": "…", "acked_at": "…"},
    {"device_id": null, "recipient": "bob", "state": "pending", "delivered_at": null, "acked_at": null}
  ]
}
```

Sessions and device tokens see everything sent from the account; an app token only sees (and
can only recall) the pushes it sent itself.

Delivery `state`: `pending` → `delivered` (seen in an inbox listing) → `acked`, or
`expired` (TTL passed, recalled, or device removed). When every delivery is acked or expired
the content is deleted (`purged_at`). The status stays visible for `VIA_TOMBSTONE_DAYS`.
Deliveries to a contact show their username in `recipient`, never their device ids.

### Sent history (optional)

When the server sets `VIA_HISTORY_DAYS` (`features.history`), the sending device can read what
it sent: items still waiting for delivery, plus delivered ones for `VIA_HISTORY_DAYS`. Only
device tokens can: sessions and app tokens never read content.

```
GET    /v1/pushes/history
GET    /v1/pushes/history/{id}/file      (only if VIA_HISTORY_KEEP_FILES)
DELETE /v1/pushes/history/{id}
```

## Contacts

Contacts let two accounts on the same server send to each other. It takes a request and an
acceptance; either side can remove the contact at any time. Items shared with you go only to
your devices with `accepts_shares` on (default: android, ios and desktop devices).

```
GET    /v1/contacts                   (any token) accepted contacts and pending requests
POST   /v1/contacts                   {"username"}  request; accepts theirs if they asked first
POST   /v1/contacts/{id}/accept       (the requested user)
DELETE /v1/contacts/{id}              decline, cancel, or remove
PATCH  /v1/devices/{id}               {"accepts_shares": false}
```

A contact: `{"id", "username", "status": "pending" | "accepted",
"direction": "incoming" | "outgoing", "created_at", "accepted_at"}`. Managing contacts needs a
session token; any token can list them, to offer contacts as targets.

The sender's storage quota pays for shared files.

## Account and device management (session token)

```
GET    /v1/account
PATCH  /v1/account/password      {"current_password", "new_password"}  (signs out other sessions)
POST   /v1/devices               {"name", "type", "accepts_shares"?}   → device token, shown once
GET    /v1/devices               (any token)
PATCH  /v1/devices/{id}          {"name"?, "accepts_shares"?}
DELETE /v1/devices/{id}          revoke: token stops working, pending items are dropped
GET    /v1/devices/me            (device token)
PATCH  /v1/devices/me            (device token) {"name"?, "accepts_shares"?}  this device only
POST   /v1/devices/me/rotate-token   (device token)
GET|POST /v1/app-tokens, DELETE /v1/app-tokens/{id}
POST   /v1/auth/register         {"username", "password", "invite"?}
POST   /v1/auth/logout
```

## Administration (admin session)

```
GET    /v1/admin/stats                  users, devices, stored items and bytes, waiting deliveries
GET    /v1/admin/users                  every account with devices, stored bytes, last seen
POST   /v1/admin/users                  {"username", "password", "is_admin"?}
PATCH  /v1/admin/users/{id}             {"is_admin"?, "disabled"?}
POST   /v1/admin/users/{id}/password    {"password"}  (signs the user out everywhere)
DELETE /v1/admin/users/{id}             deletes devices, contacts, pushes and files
POST   /v1/admin/invites                {"expires_in"?}  → single-use code, shown once
GET    /v1/admin/invites, DELETE /v1/admin/invites/{id}
```

A disabled account keeps its data, but its tokens stop working and contacts can't share with
it. Admins can't disable, demote or delete themselves (`cannot_modify_self`).

## Rate limits

`429 rate_limited` with a `Retry-After` header. Login and register are limited per IP; sending
is limited per user.
