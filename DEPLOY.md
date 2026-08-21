# Deploying Blast Radius to Render

Three services on Render's private network. Copy-pasteable, in order.

> **Read this first:** there is one unresolved architectural issue that affects
> how you should deploy. It is at the bottom under
> [The sidecar problem](#the-sidecar-problem). It does not block a deploy, but
> it decides whether you deploy two services or three.

---

## What gets deployed

| Service | Type | Public? | Why |
| --- | --- | --- | --- |
| `blast-radius-web` | Web | **yes**, HTTPS | the API and the four pages |
| `hydradb` | Private Service | **no** | the graph. 10GB disk at `/data` |
| `blast-radius-worker` | Background Worker | no | crawler, changes tail, monitor sweeps |

Only the web service has a public URL. HydraDB is a Render *private service*,
which means it has no internet-facing route at all — the app reaches it at
`http://hydradb:8443` and nothing outside the account can. That is the mechanism;
no firewall rule is involved and none is needed.

The worker exists because **Render sleeps web services on inactivity and never
sleeps background workers.** A supply-chain alert matters most when nobody is
looking at the site, so the crawl and the 24/7 watch cannot live in a process
that stops when the last visitor closes their tab.

---

## 1 · Generate the secrets

```bash
openssl rand -hex 32        # HYDRA_TOKEN / GRAPH_AUTH_TOKEN — one value, used by all three services
```

Keep it somewhere you can paste from three times. Never commit it.

> The repo contains `local-development-token-32-bytes`. That is HydraDB's
> documented local placeholder, not a leaked credential — an audit of the full
> history (`git log -p --all -S`) found no real secret was ever committed. It is
> nevertheless now impossible to use by accident: `hydra.py` refuses to start if
> `HYDRA_URL` is remote and `HYDRA_TOKEN` is unset or still the dev value.

---

## 2 · Create the Blueprint

Render Dashboard → **Blueprints** → **New Blueprint Instance** → point it at
this repository. It reads `render.yaml` and creates all three services.

Render will prompt for every variable marked `sync: false`:

| Variable | Set it on | Value |
| --- | --- | --- |
| `GRAPH_AUTH_TOKEN` | hydradb | the token from step 1 |
| `HYDRA_TOKEN` | web **and** worker | **the same** token |
| `PUBLIC_URL` | web, worker | `https://<your-service>.onrender.com` |
| `SUPABASE_URL` / `SUPABASE_ANON_KEY` | web, worker | optional — blank uses local password auth |
| `SMTP_*` | web, worker | optional — see [Email](#email) |

Deploy **hydradb first** and wait for it to go live. The other two wait for it
on boot, but starting them against a database that does not exist yet just
burns build minutes.

---

## 3 · Verify from outside the platform

Run these from your laptop, not from a Render shell.

```bash
BASE=https://<your-service>.onrender.com

# HTTPS valid, health green
curl -sS -o /dev/null -w '%{http_code} %{ssl_verify_result}\n' "$BASE/api/health"
# expect: 200 0     (0 = certificate verified)

curl -sS "$BASE/api/health" | jq '{status, restart_recommended, degraded_reason}'
```

**Confirm the graph is not exposed.** A private service has no public hostname,
so the check is that no such host resolves and nothing answers on the graph
ports at the web service's address:

```bash
# there is no public name for it — this must not resolve
getent hosts hydradb.onrender.com || echo "no public DNS for hydradb — correct"

# and the ports are not open on the app's address either
IP=$(getent hosts "${BASE#https://}" | awk '{print $1; exit}')
for p in 7687 8443 9090; do
  timeout 5 bash -c "</dev/tcp/$IP/$p" 2>/dev/null \
    && echo "OPEN $p  <-- investigate" \
    || echo "closed $p"
done
```

Everything except 443 must be closed.

### Load

```bash
# 100 concurrent requests
seq 100 | xargs -P 100 -I{} curl -s -o /dev/null -w '%{http_code}\n' \
  "$BASE/api/stats" | sort | uniq -c
```

Expect a mix of `200` and, past the limit, `429` with a `Retry-After` header.
No `5xx`, and the service still answering afterwards.

---

## 4 · Confirm the disk survives a redeploy

This is the one that matters. A disk that silently is not attached looks fine
until the first redeploy wipes the graph.

```bash
curl -sS "$BASE/api/stats" | jq '.packages, .edges'     # note both numbers
```

Render Dashboard → `hydradb` → **Manual Deploy** → *Deploy latest commit*.
Wait for live, then:

```bash
curl -sS "$BASE/api/stats" | jq '.packages, .edges'     # must be >= the first reading
```

If it came back zero, the disk is not mounted at `/data` — check the service's
Disks tab.

---

## The sidecar problem

**This is unresolved and you need to decide it.**

Blast Radius uses two stores by design: topology lives in HydraDB, and
*predicates* — declared semver ranges, maintainers, crawl state — live in a
SQLite file (`deps.db`, currently 136MB). The split is why "would have pulled
it" versus "shielded by a pin" is a number this can produce at all.

The worker **writes** that file. The web service **reads** it on fourteen
endpoints. On Render a disk attaches to exactly one service, and SQLite has no
network protocol — so **the three-service split cannot share it.** Deployed as
`render.yaml` stands, the web service starts with no sidecar and correctly
reports `graph_empty` for every package query.

Three ways out, in the order I would pick them:

**A · Managed Postgres (recommended, ~$6/mo).** Add a Render Postgres instance,
migrate the sidecar tables to it, and both services share one store with
backups included. This is the only option that also survives horizontal
scaling. It is a real migration: `blast.py` owns the sidecar queries.

**B · Two services instead of three.** Put the crawler threads back in the web
service and attach the disk there. On the **Standard** plan a web service does
not sleep, so the crawl keeps running. You lose the isolation — a crawl that
wedges takes the API with it — and you cannot scale the web service past one
instance.

**C · Ship without the sidecar.** The graph endpoints work; the semver split,
maintainer pivot and lockfile checks do not. The app degrades honestly rather
than crashing, so this is survivable for a demo, but it is half the product.

I have not chosen for you because the answer depends on how long the $45 has to
last and whether you want the semver numbers in the demo.

### Related: who owns accounts?

Same question, smaller stakes. Accounts, API keys, monitors and alerts live in
`accounts.db`, also SQLite, also written by both services. If you take option A,
move these into the same Postgres and delete `accounts.db`. If you take option
B, they are already co-located and fine.

Supabase, when configured, owns **only the credential** — email and password.
Everything else is this instance's. That boundary is deliberate and documented
in [PLATFORM.md](PLATFORM.md); do not split it further.

---

## Email

SMTP from a fresh cloud IP lands in spam — the address has no sending
reputation and Render's ranges are shared. Use a provider that signs for you:

**Postmark** (simplest) — create a server, take the Server API Token, and set
`SMTP_HOST=smtp.postmarkapp.com`, `SMTP_PORT=587`, and both `SMTP_USER` and
`SMTP_PASSWORD` to that token. Verify a Sender Signature or the whole domain.

**Amazon SES** — `email-smtp.<region>.amazonaws.com`, SMTP credentials from IAM,
and request production access or you can only send to verified addresses.

Either way, add these DNS records for the sending domain:

```
SPF     TXT   @      "v=spf1 include:spf.mtasv.net ~all"     # Postmark
                     "v=spf1 include:amazonses.com ~all"     # SES
DKIM    TXT   <selector>._domainkey    <value the provider gives you>
DMARC   TXT   _dmarc  "v=DMARC1; p=none; rua=mailto:you@yourdomain.com"
```

Start DMARC at `p=none`, read the reports for a week, then tighten to
`quarantine`. Verify with:

```bash
py setup_check.py --email you@yourdomain.com    # sends a real message
```

Alerts reach the dashboard and any webhooks whether or not email is configured.
Email is the escalation path, not the only one.

---

## Sizing

`render.yaml` ships `plan: standard` for all three. **That is a starting point,
not a measurement** — measure before you settle:

```bash
# drive it, then read Render's Metrics tab for each service
curl -sS "$BASE/api/v1/blast?name=debug&depth=5" -H "Authorization: Bearer <key>"
curl -sS -X POST "$BASE/api/v1/audit" -H "Authorization: Bearer <key>" \
  --data-binary @package-lock.json
```

The graph is ~130k edges, which is small. If HydraDB's resident set stays under
~400MB through a depth-5 traversal, Starter (512MB) will hold it and the $45
lasts roughly twice as long. Depth-5 traversals are the memory peak; audits are
the CPU peak.

**Disks cannot be shrunk.** 10GB is the floor I would start at, not a ceiling.

---

## Operational notes

**Build minutes are billed.** `.dockerignore` keeps `deps.db`, `tests/`, `docs/`
and `.git/` out of the build context, and `requirements.txt` is installed on its
own layer so a source-only change reuses it.

**The changes cursor.** The worker persists its npm `_changes` sequence to
`WORKER_STATE_DIR`. Without a disk on the worker that directory is ephemeral, so
a redeploy re-anchors to *now* and silently skips everything published during
the gap. If gapless history matters, attach a 1GB disk to the worker and point
`WORKER_STATE_DIR` at it.

**HydraDB `autoDeploy` is off** on purpose. The graph should not restart because
the application code changed.

**Rollback:** Render keeps previous deploys. Dashboard → the service → Events →
*Rollback*. The disk is untouched by a rollback, so the graph survives it.
