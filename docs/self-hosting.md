# Self-hosting Via

Via is one container with one data volume. It runs a single process on purpose: SQLite, live
event streams and rate limits all live in that process. The server holds a lock on the data
directory (`via.lock`), so a second server pointed at the same data refuses to start.

## 1. Install

```sh
mkdir via && cd via
curl -O https://raw.githubusercontent.com/eitchtee/Via/main/docker-compose.yml
docker compose up -d
docker compose exec via via create-user alice --admin
```

This runs the published image (see [Images](#images)). To build from source instead, clone
the repository, and in `docker-compose.yml` comment out `image:` and uncomment `build: .`.

Open `http://<host>:8080` and sign in. The web UI manages devices, contacts and app tokens,
sends items, and (for admins) manages users and invites.

Alternatively, set `VIA_ADMIN_USERNAME` and `VIA_ADMIN_PASSWORD` for the first start: the admin
is created if the server has no users yet. Remove them afterwards.

## 2. The master key

Everything users send is encrypted at rest with keys derived from the **master key**. On
first start Via generates one at `/data/master.key` and logs a warning.

- **Back it up separately from the data.** A backup of the data without the key can't be
  decrypted, which is the point, and a lost key means lost data.
- To manage it yourself, generate one with `docker compose run --rm via via gen-key` and pass
  it via `VIA_MASTER_KEY_FILE` (for example a Docker secret) or `VIA_MASTER_KEY`.

### Rotating the key

```sh
docker compose stop via
docker compose run --rm via via rotate-master-key
docker compose start via
```

This re-encrypts every stored data key under a new master key in one transaction. File
contents on disk aren't rewritten, so it's fast. The new key is written to disk *before* the
database changes, so an interruption can't lose it.

The server has to be stopped: a running server would keep using the old key, fail to read
anything, and seal new items with a key that is about to be discarded. The command checks the
data directory lock and refuses to run while the server is up; while it runs, the server
can't start either. (The lock relies on the OS, so the volume must be local: don't put the
data directory on NFS or other network storage.)

- With the generated `/data/master.key`, the file is replaced automatically.
- With `VIA_MASTER_KEY` / `VIA_MASTER_KEY_FILE`, the new key is saved to
  `/data/master.key.new`. Put it where your configuration expects it, delete that file, then
  start the server.

`--new-key-file <path>` rotates to a key you provide instead of a generated one.

## 3. TLS and reverse proxies

Via speaks plain HTTP; terminate TLS in front of it. Two things matter:

1. **Don't buffer responses.** `/v1/inbox/events` is a long-lived server-sent event stream.
2. **Pass the client IP** and set `VIA_FORWARDED_ALLOW_IPS` to the proxy's address. Rate
   limits for login are per IP. Use `*` only when the proxy is the *only* thing that can
   reach Via (with Docker: don't publish Via's port). Otherwise anyone can send a fake
   `X-Forwarded-For` header and dodge the rate limit.

**Caddy** (see `Caddyfile`):

```
via.example.com {
	reverse_proxy via:8080 {
		flush_interval -1
	}
}
```

**nginx:**

```nginx
location / {
    proxy_pass http://127.0.0.1:8080;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_buffering off;             # server-sent events
    proxy_read_timeout 1h;
    proxy_request_buffering off;     # stream uploads instead of spooling them to disk
    client_max_body_size 100m;       # at least VIA_MAX_FILE_SIZE
}
```

**Traefik:** SSE works out of the box. Raise `respondingTimeouts.readTimeout` on the
entrypoint if large uploads time out.

## 4. Android push notifications

The official Android app is woken up through the
**[Via FCM relay](https://github.com/eitchtee/ViaFCMRelay)** (a separate project). Set
`VIA_PUSH_RELAY_URL` to the relay the app's publisher runs, plus `VIA_PUSH_RELAY_KEY` if they
gave you an instance key. The relay only ever receives the device's FCM token and an item id,
never content.

To run your own relay (needed if you build the Android app with your own Firebase project),
[`examples/docker-compose.with-relay.yml`](examples/docker-compose.with-relay.yml) sets up
Via and the relay together: the relay stays private on the compose network, and Via
authenticates to it with an instance key.

Without a relay, Android clients can use UnifiedPush (e.g. with ntfy as a distributor) or
periodic sync.

## 5. Accounts and signup

`VIA_SIGNUP` controls who can create accounts:

- `invite` (default): anyone with a single-use invite code. Admins create codes in the web UI,
  through `POST /v1/admin/invites`, or with `via create-invite --expires 1d`.
- `closed`: only admins create accounts (web UI, `POST /v1/admin/users`, `via create-user`).
- `open`: anyone who can reach the server.

Admins can also disable accounts (data kept, access blocked), reset passwords and delete
accounts from the web UI. From the command line: `via reset-password <user>`.

Users on the same server can become contacts and send each other items. Items shared with
someone are stored against the **sender's** quota.

## 6. Backups and restore

Everything lives in the data volume:

| Path | What |
|---|---|
| `via.db` (+ `-wal`, `-shm`) | SQLite database: accounts, devices, encrypted item metadata |
| `blobs/` | Encrypted file contents |
| `master.key` | The generated master key, if you don't provide your own |

Consistent backup while running:

```sh
docker compose exec via python -c "import sqlite3; s=sqlite3.connect('/data/via.db'); d=sqlite3.connect('/data/backup.db'); s.backup(d)"
# then copy /data/backup.db and /data/blobs/ (and the master key) somewhere safe
```

Or stop the container and copy the whole volume. To restore, put the files back in an empty
volume (rename `backup.db` to `via.db`), provide the same master key, and start the server.
Blobs without a database row are cleaned up automatically, so a slightly newer `blobs/` is
harmless.

Since Via deletes items once they're delivered, backups mostly matter for accounts, devices
and contacts, plus whatever is still waiting to be delivered.

## Images

Prebuilt images for `linux/amd64` and `linux/arm64` are published to GitHub Container
Registry as [`ghcr.io/eitchtee/via`](https://github.com/eitchtee/Via/pkgs/container/via)
by `.github/workflows/docker.yml`:

| Tag | What |
|---|---|
| `latest` | The latest release |
| `v1.2.3` (a release tag) | That release |
| `nightly` | The newest commit on `main`; may be unstable |

`latest` follows new releases on `docker compose pull`. To upgrade only when you choose, pin a
release instead: `image: ghcr.io/eitchtee/via:v0.1.0`.

Maintainers can also run the workflow by hand (Actions → Docker image → Run workflow) with a
branch or release tag, and optionally a custom image tag. `latest` only moves when the build
is the repository's latest release, so publishing a backport release, or building an older tag
by hand, never changes it.

## 7. Upgrades

```sh
docker compose pull && docker compose up -d     # published image
git pull && docker compose up -d --build        # building from source
```

Database migrations run automatically at startup. Take a backup before upgrading across
versions.

## 8. Configuration reference

See the table in the [README](../README.md#configuration). All settings are `VIA_*`
environment variables.

## 9. Troubleshooting

- **Live updates arrive late or in bursts:** the reverse proxy is buffering. See section 3.
- **Everyone gets rate-limited at once:** Via sees the proxy's IP. Set
  `VIA_FORWARDED_ALLOW_IPS` and make the proxy send `X-Forwarded-For`.
- **`DecryptionError` / 500s after a restore:** the master key doesn't match the data.
- **Disk usage:** `GET /v1/admin/stats` (or the Admin page) shows stored bytes; lower
  `VIA_DEFAULT_TTL` or `VIA_USER_QUOTA` if needed.
