# Setup

Where every credential goes, and how to prove it works.

## The short version

```bash
cp .env.example .env      # Windows: copy .env.example .env
#  ...edit .env  (section 1 is Supabase)
py setup_check.py         # talks to every service and tells you what is broken
py server.py
```

`setup_check.py` does not read settings and declare victory — it opens a
connection to each service. A green line means that thing worked, just now.

---

## 1 · Supabase — where your URL and keys go

**File:** `.env` in the project root. **Lines 1–2 of section 1.**

```bash
SUPABASE_URL=https://abcdefghijklmnop.supabase.co
SUPABASE_ANON_KEY=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9....
```

Find both at **Supabase dashboard → your project → Settings → API**:

| Supabase calls it | Put it in |
| --- | --- |
| Project URL | `SUPABASE_URL` |
| `anon` `public` | `SUPABASE_ANON_KEY` |
| `service_role` `secret` | `SUPABASE_SERVICE_ROLE_KEY` (optional) |

That is the whole integration. Set those two and every sign-up and sign-in goes
through Supabase Auth. Leave them blank and the local password store is used
instead — same product, no external dependency.

### Two settings in the Supabase dashboard

**Authentication → Providers → Email** — make sure Email is enabled, or nobody
can register. `setup_check.py` fails loudly if it is off.

**Authentication → URL Configuration** — set *Site URL* to the same value as
`PUBLIC_URL`, and add `<PUBLIC_URL>/signin` to *Redirect URLs*. Without this,
confirmation and password-reset links bounce.

If you leave *Confirm email* on (the default), sign-up tells the user to check
their inbox and does **not** sign them in — that path is handled and says so.
Turn it off and sign-up signs them straight in. Both work; `setup_check.py`
reports which one you have.

### Verify it

```bash
py setup_check.py
```

```
3 · authentication
  PASS  Supabase reachable at https://abcdefghijklmnop.supabase.co
  PASS  SUPABASE_ANON_KEY accepted by the project
  PASS  email sign-up is enabled on the project
  WARN  email confirmation required
```

A wrong URL or key fails here rather than at 2am:

```
  FAIL  Supabase is unreachable
        could not reach Supabase at https://typo.supabase.co (ConnectionError).
```

**Nothing else changes when you switch.** API keys, monitors, alerts, webhooks
and the security log stay in this instance's `accounts.db`, keyed on the
Supabase user id. Existing local accounts keep working; new ones go to Supabase.

---

## 2 · This deployment

```bash
PUBLIC_URL=https://blastradius.yourdomain.com
SECURE_COOKIES=1
```

`PUBLIC_URL` is used in emails, webhook payloads and the copy-paste examples on
`/developers`. Leave it blank locally and the request's own origin is used.

Set `SECURE_COOKIES=1` the moment you are behind HTTPS — it puts `Secure` on
the session cookie.

---

## 3 · Alert delivery

Alerts always reach the dashboard. These make them reach a person.

### Webhooks — no configuration needed here

Each account adds its own endpoint at **/dashboard → Notifications**. Blast
Radius POSTs a signed JSON body the moment the watch sees something move.

```
X-BlastRadius-Signature: t=1787270000,v1=9f86d081884c7d65...
X-BlastRadius-Event: alert
```

HMAC-SHA256 over `<t>.<raw body>` with that endpoint's secret. Verify against
the **raw bytes**, and reject a timestamp older than a few minutes — the
timestamp is inside the signed material so a captured payload cannot be
replayed. A worked receiver is on the Notifications tab.

Three attempts with backoff per delivery; an endpoint that fails 20 times in a
row is disabled rather than retried forever.

Prove one without waiting for an incident:

```bash
py setup_check.py --webhook https://your-endpoint.example.com/hook
```

…or press the ⚡ button next to any endpoint on the dashboard, which sends a
real signed delivery and shows you the response.

### Email — optional

```bash
SMTP_HOST=smtp.postmarkapp.com
SMTP_PORT=587
SMTP_USER=your-token
SMTP_PASSWORD=your-token
SMTP_FROM=alerts@yourdomain.com
SMTP_STARTTLS=1
```

Gmail needs an **App Password**, not your account password. Only `high` and
`critical` alerts are emailed — a baseline measurement is not worth an inbox.

```bash
py setup_check.py --email you@yourdomain.com     # sends a real message
```

---

## 4 · The watch

```bash
MONITOR_INTERVAL=300     # seconds between sweeps for due monitors
MONITOR_STALE=3600       # a monitor is due again this long after its last check
```

Defaults re-measure every watched package about once an hour, continuously.
Tighten `MONITOR_STALE` for a faster watch at the cost of more traversals.

The worker starts with the server and its state lives in SQLite, so a restart
resumes rather than forgetting. `/api/platform-stats` reports
`worker_running`; `setup_check.py` fails if it is stopped.

---

## 5 · Running it for real

```bash
docker compose up -d                    # HydraDB
py ingest.py                            # populate the graph (long-running)
py server.py --host 0.0.0.0 --port 8000
```

Behind a reverse proxy, forward `X-Forwarded-For` so the security log records
real client addresses, and do not buffer `text/event-stream` — the dashboard's
live updates and `/api/events` are SSE. For nginx:

```nginx
location / {
    proxy_pass http://127.0.0.1:8000;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header Host $host;
    proxy_buffering off;          # SSE
    proxy_read_timeout 3600s;
}
```

Then set `SECURE_COOKIES=1` and `PUBLIC_URL=https://…` and re-run
`py setup_check.py`.

---

## What is stored where

| File | Contents | Tracked by git |
| --- | --- | --- |
| `.env` | your credentials | **no** |
| `accounts.db` | accounts, sessions, key digests, monitors, alerts, webhooks, audit log | **no** |
| `deps.db` | the npm graph sidecar, crawler-owned | no |

API keys are stored **only** as SHA-256 digests. Webhook secrets are stored in
plaintext because a signature has to be computed from them — treat
`accounts.db` accordingly.
