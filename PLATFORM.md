# The platform layer

Accounts, API keys, 24-hour monitoring and the public `/api/v1` API. This is
the part of Blast Radius that other projects integrate against; everything it
serves comes from the same graph and the same code path the console uses.

## Pages

| Route | What it is |
| --- | --- |
| `/` | The landing page. Marketing plus the live-ingest rail. No console. |
| `/check` | The dedicated console — blast radius, semver split, lockfile check, OSV audit, graph explorer, publish feed. Deep-linkable as `/check?pkg=debug&ver=4.4.2`. |
| `/developers` | The API: key vault, quickstarts, live playground, full reference, monitoring guide, error table. |
| `/dashboard` | The account: monitors, alerts, keys, security log. Live over SSE. |
| `/signin` | Sign in / create an account. |

## Files

| File | Responsibility |
| --- | --- |
| `accounts.py` | Storage and logic: accounts, sessions, keys, monitors, alerts, audit log, and the watch worker. Knows nothing about HTTP or about the graph. |
| `platform_api.py` | The routes. Graph handlers are injected by `server.py`, so this module never imports back into it. |
| `apidocs.py` | The API reference as data, rendered to JSON, Markdown and plain text. |
| `config.py` | Reads `.env` into the environment and describes what it found. |
| `notify.py` | Webhook signing and delivery, SMTP email, the delivery worker. |
| `setup_check.py` | Validates every credential against the real service. |
| `web/shell.js` | Header, footer, icon sprite and session state shared by every page. |
| `web/app.css` | The application surfaces, on the same tokens as `style.css`. |

## Configuration

Everything is read from `.env` in the project root — copy `.env.example` and
fill it in. **[SETUP.md](SETUP.md) says exactly which line each credential goes
on.** Real environment variables always win over the file.

Validate it against the live services with:

    py setup_check.py [--email you@x] [--webhook https://...]

Every check opens a connection rather than reading a setting, so a pass means
that thing worked just now. `/api/platform-stats` exposes the same picture at
runtime, reporting whether a secret is *set and valid*, never the secret.

## Authentication

Two backends behind one interface.

**Local (default).** PBKDF2-HMAC-SHA256, 310,000 iterations, a per-account
salt, in `accounts.db`. No third-party dependency, works offline.

**Supabase.** Put the project URL and anon key in `.env` and sign-up, sign-in
and password reset go to Supabase's GoTrue instead:

```bash
SUPABASE_URL=https://xxxx.supabase.co
SUPABASE_ANON_KEY=eyJhbGciOi...
```

The access token GoTrue returns is verified against `/auth/v1/user` rather than
decoded and trusted — it is a value this process did not mint. A local mirror
row keyed on the Supabase user id keeps keys, monitors and alerts owned.
Both email-confirmation modes are handled: with confirmation on, sign-up
returns `202 confirm_email` and does not create a session.

`/api/auth/me` reports which backend is live, and the sign-in page says so out
loud rather than implying one.

Sessions are opaque 32-byte tokens in an `HttpOnly`, `SameSite=Lax` cookie.
Set `SECURE_COOKIES=1` behind TLS.

## API keys

* Format `brk_live_` + 40 URL-safe characters.
* **Only a SHA-256 digest is stored**, alongside a short non-secret prefix for
  the vault UI. A copy of `accounts.db` yields no working key.
* The plaintext is returned exactly once, from `POST /api/keys`, and is held
  only in the creating tab's memory — never in localStorage or sessionStorage.
* Revocation is a single `UPDATE`; the next request with that key 401s.
* Every call a key makes is written to the owner's security log with the
  address and user agent that made it.

There are no rate limits, quotas or tiers on keyed calls. The anonymous
browser rate limit does not apply to them.

## 24-hour monitoring

A monitor is a package an account cares about. A worker thread re-measures
each one on a timer, compares the result with the last observation and writes
an alert when it moves.

```
MONITOR_INTERVAL=300   # seconds between sweeps
MONITOR_STALE=3600     # a monitor is due again this long after its last check
```

Alert levels are derived from the size of the movement — `info`, `notable`,
`high`, `critical` — and every alert carries the before and after counts, so a
consumer can gate on the delta rather than the absolute. A new monitor is
measured immediately rather than waiting for the next sweep, and its first
alert is the baseline.

Alerts reach the dashboard over `GET /api/account/events` (SSE, per account).
Nothing on the dashboard polls.

## Alert delivery

The dashboard is where alerts live, but a monitoring system whose only output
is a web page you have to be looking at is not monitoring anything.

**Webhooks.** Each account registers endpoints at `/dashboard#delivery` or via
`POST /api/webhooks`. Every alert is POSTed as signed JSON:

    X-BlastRadius-Signature: t=<unix>,v1=<hmac-sha256>
    X-BlastRadius-Event: alert

HMAC-SHA256 over `<t>.<raw body>` with the endpoint's secret. The timestamp is
inside the signed material so a captured payload cannot be replayed. Three
attempts with backoff; 20 consecutive failures disables the endpoint rather
than growing a queue forever. `notify.verify()` is the reference receiver
implementation and is what the tests assert against.

**Email.** Optional, plain SMTP, `high` and `critical` only.

Delivery never blocks or fails the traversal that raised the alert: it runs on
its own worker behind a bounded queue.

## Endpoints

Session-authenticated (cookie), used by the dashboard:

```
POST   /api/auth/signup           POST   /api/auth/login
POST   /api/auth/logout           GET    /api/auth/me
GET    /api/keys                  POST   /api/keys
DELETE /api/keys/{id}
GET    /api/monitors              POST   /api/monitors
DELETE /api/monitors/{id}
GET    /api/alerts                POST   /api/alerts/read
GET    /api/webhooks              POST   /api/webhooks
DELETE /api/webhooks/{id}         POST   /api/webhooks/{id}/test
GET    /api/security-log          GET    /api/account/events   (SSE)
POST   /api/auth/reset            (password reset; Supabase only)
```

Key-authenticated (`Authorization: Bearer brk_live_...`), the public contract:

```
GET  /api/v1/whoami        GET  /api/v1/blast         GET  /api/v1/resolve
GET  /api/v1/maintainers   GET  /api/v1/typosquats    GET  /api/v1/subgraph
POST /api/v1/lockfile      POST /api/v1/audit
GET  /api/v1/monitors      POST /api/v1/monitors      DELETE /api/v1/monitors/{id}
GET  /api/v1/alerts        GET  /api/v1/webhooks
```

Unauthenticated, for tooling and agents:

```
GET /api/docs.json    the reference as data
GET /api/docs.md      the reference as Markdown  (paste into an agent)
GET /api/docs.txt     the reference as plain text
GET /api/docs         OpenAPI / Swagger UI
GET /api/platform-stats
```

## Integrating

```bash
# 1. create an account and a key at /developers
# 2. confirm it works
curl -H 'Authorization: Bearer brk_live_...' http://localhost:8000/api/v1/whoami

# 3. measure something
curl -H 'Authorization: Bearer brk_live_...' \
  'http://localhost:8000/api/v1/blast?name=debug&depth=5'

# 4. hand the watch a package and stop polling
curl -X POST http://localhost:8000/api/v1/monitors \
  -H 'Authorization: Bearer brk_live_...' \
  -H 'Content-Type: application/json' \
  -d '{"package":"debug"}'
```

## Data

`accounts.db` (SQLite, WAL) holds `accounts`, `sessions`, `api_keys`,
`monitors`, `alerts`, `webhooks` and `audit`. It is separate from `deps.db`, which the
crawler owns and the API opens read-only. Delete `accounts.db` to reset the
platform without touching the graph.
