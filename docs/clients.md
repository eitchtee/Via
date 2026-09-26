# Writing a Via client

This guide walks through everything a client needs: signing in, receiving reliably, staying
battery friendly, and sending. The API is plain HTTP + JSON, so any language works. See
[api.md](api.md) for the full endpoint list.

## 1. Connect and register

```
GET  {server}/v1/info                      → check api_version == 1, read limits and features
POST {server}/v1/auth/login                {"username", "password"}          → session token
POST {server}/v1/devices                   {"name", "type"}  (with the session) → device token
```

- Store the **device token** in the platform's secure storage (Android Keystore, macOS
  Keychain, Windows Credential Manager, libsecret). It is the device's identity: it reads
  this device's inbox and nothing else.
- Throw the session away (`POST /v1/auth/logout`). A device can rename itself and change its
  own share setting with its device token (`PATCH /v1/devices/me`). Managing *other* devices,
  contacts or app tokens needs a session: ask for the password when the user opens such a
  screen, and log out afterwards rather than storing the session.
- If any request returns `401 invalid_token`, the device was removed or its token rotated:
  forget it and ask the user to sign in again.

## 2. The receive loop

The core rule: **only ack what you have safely handled**. The server keeps an item until
every target device acked it, so a crash anywhere before the ack just means you'll see the
item again.

```python
def sync():
    cursor = None
    while True:
        page = GET("/v1/inbox", params={"after": cursor, "limit": 50})
        for item in page["items"]:
            if already_handled(item["id"]):        # acks can get lost: be idempotent
                ack(item["id"])
                continue
            if item["kind"] == "file":
                path = download(item)                 # resumable, verified, see below
                if path is None:
                    continue                          # retry on the next sync
            handle(item)                              # open link, show note, save file…
            remember_handled(item["id"])
            ack(item["id"])                           # POST /v1/inbox/{id}/ack
        if not page["items"]:
            return
        cursor = page["cursor"]
```

Downloading a file:

```python
def download(item):
    part = partial_path(item["id"])
    have = part.size() if part.exists() else 0
    headers = {"Range": f"bytes={have}-"} if have else {}
    r = GET(f"/v1/inbox/{item['id']}/file", headers=headers, stream=True)
    if r.status == 410:                               # file already gone (e.g. recalled)
        return None
    part.append(r.body) if r.status == 206 else part.write(r.body)
    if sha256(part) != item["file"]["sha256"]:        # also in Repr-Digest / X-Via-SHA256
        part.delete()
        return None
    return part.move_to_downloads(item["file"]["name"])  # sanitize the name first!
```

Treat everything in an item as untrusted input: sanitize file names before writing them,
don't auto-open non-`https` links, and escape titles and bodies when displaying them. Items
with a non-null `sender` come from a contact: show who sent them, and consider asking before
opening their links.

Use `POST /v1/inbox/ack` with `{"ids": [...]}` to ack many items at once.

## 3. Knowing when to sync

Call `sync()` on start, on network changes, and whenever a wake-up arrives.

| Platform | Recommended | Fallback |
|---|---|---|
| Android (Google Play) | FCM through the relay: `PUT /v1/devices/me/push {"provider": "fcm_relay", "token"}`, and again on every `onNewToken` | periodic work (e.g. WorkManager every 15 min) |
| Android (de-Googled) | UnifiedPush: `{"provider": "unifiedpush", "endpoint"}` | periodic work |
| Desktop, browser extension | SSE: `GET /v1/inbox/events` | long-poll `GET /v1/inbox?wait=30` in a loop |
| Scripts, CLI | long-poll | plain polling |

SSE details:

- Send the device token in the `Authorization` header. Browser `EventSource` can't, so read
  the stream with `fetch()` instead.
- On `ready` (every (re)connect), run `sync()`, because you may have missed events while
  disconnected. On `push`, run `sync()`. On `recalled`, drop that id if you haven't handled
  it. On `revoked`, sign out.
- Reconnect with exponential backoff (1 s up to ~60 s, with jitter). The server sends a
  `: ping` comment every ~25 s: if nothing arrives for ~60 s, assume the connection is dead.

FCM and UnifiedPush messages are `{"t": "wake", "id": "<push id>"}`. They never contain
content: always `sync()` to get the item.

## 4. Sending

```
GET  /v1/devices        → your devices, for a "send to" picker (skip the one with "current": true)
GET  /v1/contacts       → contacts with "status": "accepted" can be targets as "@username"
POST /v1/pushes         {"kind": "link", "url": "…", "to": "all"}
POST /v1/pushes         {"kind": "note", "body": "…", "to": ["<device id>", "@bob"]}
POST /v1/pushes/file?filename=<name>&to=<all | id,@user>      raw body, Content-Type = MIME
```

Check `limits.max_file_size` and `limits.max_text_length` from `/v1/info` before uploading.
On `429`, wait for `Retry-After` seconds.

## 5. Checklist

- [ ] Device token in secure storage; `401` means sign in again
- [ ] Ack only after an item is fully handled; handling is idempotent on `id`
- [ ] File downloads resume with `Range` and are verified against `sha256`
- [ ] File names are sanitized; links and text are treated as untrusted
- [ ] Items from contacts (`sender` set) are marked as such
- [ ] Sync on start, on network change, on every wake-up, and on SSE `ready`
- [ ] Wake-up registration refreshed whenever the push token or endpoint changes
