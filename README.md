# Via

Send **links, notes and files** between your devices through **your own server**.

Via is store-and-forward: you send something to one, several or all of your devices, and the
server keeps it (encrypted) until each target device has downloaded it **and confirmed
receipt**. Then it is deleted. Offline devices simply get it when they come back.

Inspired by Join and Pushbullet; related to ntfy and Gotify, except that there are no topics to
subscribe to: each device registers itself and has its own inbox.

This repository is the **server, its HTTP API and a small web UI**. Device clients (Android,
desktop, browser extension, CLI) are separate and use only the [documented API](docs/api.md).

**Docs:** [self-hosting](docs/self-hosting.md) · [API](docs/api.md) ·
[writing a client](docs/clients.md)

## Features

- Links, notes and files; send to specific devices, to `all` your other devices, or to a
  contact on the same server (`@username`)
- Acknowledged delivery: fetching never deletes, only an explicit ack does
- Encryption at rest (AES-256-GCM, a data key per item wrapped by a server master key)
- Per-device tokens: only a device can read its own inbox; the account password can't
- Send-only app tokens for scripts, plus a curl-friendly `/v1/send` endpoint
- Real-time delivery: server-sent events, long-polling, UnifiedPush, and FCM via a push relay
- Resumable downloads (`Range`), SHA-256 of every file for verification
- Multi-account, with admin-controlled signup (closed, invite codes, or open), mutual
  contacts, and per-device opt-in for receiving shares
- Web UI to manage devices, contacts and tokens, send items, and administer the server
- One container, one volume, SQLite

## Quick start (Docker)

```sh
docker compose up -d
docker compose exec via via create-user alice --admin
```

Then open `http://localhost:8080` and sign in. Put the server behind a TLS reverse proxy for
anything beyond your LAN; the proxy must not buffer responses, or live updates arrive late.

On first start Via generates a **master key** in `/data/master.key`. **Back it up**; without
it, stored content can't be decrypted. See the [self-hosting guide](docs/self-hosting.md)
for TLS, backups, key rotation and upgrades.

## Try it with curl

```sh
VIA=http://localhost:8080

# Log in (session token: account and device management)
SESSION=$(curl -s $VIA/v1/auth/login -H 'content-type: application/json' \
  -d '{"username":"alice","password":"..."}' | jq -r .token)

# Register two devices; each gets its own token
PHONE=$(curl -s $VIA/v1/devices -H "Authorization: Bearer $SESSION" \
  -H 'content-type: application/json' -d '{"name":"phone","type":"android"}' | jq -r .token)
LAPTOP=$(curl -s $VIA/v1/devices -H "Authorization: Bearer $SESSION" \
  -H 'content-type: application/json' -d '{"name":"laptop","type":"desktop"}' | jq -r .token)

# Send from the phone to all other devices
curl -H "Authorization: Bearer $PHONE" -d "https://example.com" $VIA/v1/send
curl -H "Authorization: Bearer $PHONE" -d "remember the milk" "$VIA/v1/send?title=Todo"
curl -H "Authorization: Bearer $PHONE" -T photo.jpg $VIA/v1/send/

# Receive on the laptop, then acknowledge
curl -H "Authorization: Bearer $LAPTOP" $VIA/v1/inbox
curl -H "Authorization: Bearer $LAPTOP" -X POST $VIA/v1/inbox/<id>/ack
```

Interactive API docs are served at `/docs`, the OpenAPI schema at `/openapi.json`.

## Configuration

All settings are environment variables. Sizes accept `1048576`, `100MiB`, `1GB`; durations
accept seconds or `30m`, `7d`, `1h30m`.

| Variable | Default | |
|---|---|---|
| `VIA_DATA_DIR` | `data` (`/data` in Docker) | Database, blobs, generated master key |
| `VIA_MASTER_KEY` / `VIA_MASTER_KEY_FILE` | generated | Base64 32-byte key for encryption at rest |
| `VIA_HOST` / `VIA_PORT` | `0.0.0.0` / `8080` | |
| `VIA_FORWARDED_ALLOW_IPS` | `127.0.0.1` | Proxies trusted for `X-Forwarded-For` |
| `VIA_SIGNUP` | `invite` | `closed`, `invite` or `open` |
| `VIA_ADMIN_USERNAME` / `VIA_ADMIN_PASSWORD` | | Create an admin on first start |
| `VIA_SESSION_TTL` | `30d` | Session token lifetime |
| `VIA_INVITE_TTL` | `7d` | Default invite code lifetime |
| `VIA_DEFAULT_TTL` / `VIA_MAX_TTL` | `7d` / `30d` | How long undelivered items are kept |
| `VIA_TOMBSTONE_DAYS` | `7` | How long delivery status is kept after content is deleted |
| `VIA_HISTORY_DAYS` | `0` (off) | Keep sent items for the sending device this many days |
| `VIA_HISTORY_KEEP_FILES` | `false` | Also keep file contents in history (counts toward quota) |
| `VIA_MAX_FILE_SIZE` | `100MiB` | |
| `VIA_USER_QUOTA` | `1GiB` | Stored files per user |
| `VIA_MAX_TEXT_LENGTH` | `262144` | Characters per note/title/url |
| `VIA_PUSH_RELAY_URL` | empty (off) | Via FCM relay for Android wake-ups |
| `VIA_PUSH_RELAY_KEY` | | Optional relay instance key (`X-Via-Instance-Key`), for higher relay limits |
| `VIA_LOGIN_RATE_PER_MINUTE` | `10` | Per IP, for login and register |
| `VIA_PUSH_RATE_PER_MINUTE` | `120` | Per user |
| `VIA_DOCS_ENABLED` | `true` | Serve `/docs` |
| `VIA_WEB_UI` | `true` | Serve the web UI at `/` |
| `VIA_LOG_LEVEL` | `info` | |

## Administration

```sh
via create-user bob [--admin]      # prompts for the password (or --password-stdin)
via reset-password bob
via create-invite --expires 1d     # prints a single-use code for POST /v1/auth/register
via gen-key                        # prints a new master key
via rotate-master-key              # re-encrypts stored data under a new key (server stopped)
```

In Docker, prefix with `docker compose exec via` (or `docker compose run --rm via` while the
server is stopped). Admins can do most of this in the web UI's Admin page, or through the
`/v1/admin/*` API.

## Security model

- Content (titles, bodies, URLs, file names and file contents) is encrypted at rest. This
  protects against theft of the disk, database or backups. It does **not** protect against
  whoever operates the server; end-to-end encryption may come later (the schema is ready).
- Every device has its own token and can only read its own inbox. A session (password login)
  can manage devices and send, but cannot read any device's content. The web UI uses a
  session, so it can send but never shows received content.
- Sharing needs a mutual contact, and only reaches the devices the recipient opted in. Senders
  see a contact's username, never their devices.
- Wake-up notifications (UnifiedPush, FCM) never carry content, only an item id.

## Development

```sh
uv sync
uv run pytest
uv run ruff format . && uv run ruff check . && uv run mypy
VIA_DATA_DIR=data uv run via serve
```

Schema changes: edit `src/via/db/models.py`, then
`uv run alembic revision --autogenerate -m "describe the change"`. The server applies
migrations on startup.

## License

[AGPL-3.0-or-later](LICENSE)
