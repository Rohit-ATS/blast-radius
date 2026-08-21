# Deploying Blast Radius to Render

One service on Render. Copy-pasteable, in order.

---

## What gets deployed

| Service | Type | Public? | Why |
| --- | --- | --- | --- |
| `blast-radius-web` | Web | **yes**, HTTPS | the graph, the API and the four pages |

It used to be three — the app, HydraDB as a private service with a 10GB disk,
and a background worker. That layout was correct and it was also most of the
bill, because a private service and a disk are both paid tiers. When the credit
ran out the graph service went away and every traversal on the site went with
it: blast radius, chains and paths all reporting "the dependency graph is
unavailable" while the OSV-backed half kept working.

So the graph runs inside the web container now, and that is sound rather than a
shortcut for one specific reason: **the graph is derived data.** Postgres holds
every dependency edge the crawler has ever written, so the graph node can live
on an ephemeral filesystem and be rebuilt from it at boot — six seconds, no npm
traffic. See `rehydrate.py`. What would be reckless for a system of record is
routine for a cache.

Three consequences worth knowing before you deploy:

* **The graph binds `127.0.0.1` inside the container.** Nothing outside can
  reach it. That is a stronger boundary than the private service had — that one
  was reachable by anything else on the account's private network.
* **The graph is bounded** to what the instance can hold (`REHYDRATE_MAX_EDGES`,
  55,000 on a 512MB Starter). The API reports coverage on every response, so a
  partial graph tells the caller it is partial instead of quietly answering with
  a smaller number.
* **There is no worker,** so `LIVE_INGEST` and `LIVE_FEED` are off. Nothing that
  is already crawled is lost — the sidecar holds all of it — but discovery of
  packages not yet seen stops. See [Sizing](#sizing) for turning it back on.

---

## 1 · Generate the secret

```bash
openssl rand -hex 32        # HYDRA_TOKEN
```

The entrypoint writes it into the file HydraDB authenticates against. Never
commit it.

> The repo contains `local-development-token-32-bytes`. That is HydraDB's
> documented local placeholder, not a leaked credential — an audit of the full
> history (`git log -p --all -S`) found no real secret was ever committed. It is
> nevertheless now impossible to use by accident: `hydra.py` refuses to start if
> `HYDRA_URL` is remote and `HYDRA_TOKEN` is unset or still the dev value.

---

## 2 · Create the Blueprint

Render Dashboard → **Blueprints** → **New Blueprint Instance** → point it at
this repository. It reads `render.yaml` and creates the one service.

Render will prompt for every variable marked `sync: false`:

| Variable | Value |
| --- | --- |
| `HYDRA_TOKEN` | the token from step 1 |
| `DATABASE_URL` | Supabase **pooler** URI — `aws-0-<region>.pooler.supabase.com:6543` |
| `PUBLIC_URL` | `https://<your-service>.onrender.com` |
| `SUPABASE_URL` / `SUPABASE_ANON_KEY` / `SUPABASE_SERVICE_ROLE_KEY` | optional — blank uses local password auth |
| `SMTP_*` | optional — see [Email](#email) |

`DATABASE_URL` is the one that is not optional in practice. Without it the
process falls back to a local SQLite file no crawler has ever written to, the
boot rebuild finds no edges, and the site comes up with an empty graph. Use the
pooler endpoint and keep its region matched to the blueprint's.

The first boot takes about two minutes: the graph node starts, then
`rehydrate.py` replays the sidecar into it before gunicorn binds the port. Watch
for these lines in the deploy log —

```
[entrypoint] graph ready: http://127.0.0.1:9090/readyz -> 200
[rehydrate] pin budget of 1000 edges reached at 'debug'; the popular pins past it are left to the main budget
[rehydrate] +1000 pinned edges so the incident packages the popularity cut drops answer completely
[rehydrate] 56000 edges touching 15049 packages
[rehydrate] 15049 vertices, 56000 edges in 9.2s
[entrypoint] watching graph-node (pid 10)
```

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

**Confirm the graph is not exposed.** It binds `127.0.0.1` inside the
container, so nothing outside that process namespace can reach it. The check is
that no graph port answers at the app's address:

```bash
IP=$(getent hosts "${BASE#https://}" | awk '{print $1; exit}')
for p in 7687 8443 9090; do
  timeout 5 bash -c "</dev/tcp/$IP/$p" 2>/dev/null \
    && echo "OPEN $p  <-- investigate" \
    || echo "closed $p"
done
```

Everything except 443 must be closed.

**Confirm the graph actually answers,** which is the failure this layout exists
to prevent. `event-stream` is the useful probe: it has 11 dependents, which puts
it far below any popularity cutoff, so it is the first thing a bounded rebuild
gets wrong.

```bash
curl -sS "$BASE/api/blast?name=event-stream&depth=5" | jq '{total, coverage}'
# expect: total 11, and no coverage object (its absence means complete)
```

A `total` of 0 means the rebuild did not load its edges — see `PINNED` in
`rehydrate.py`. A `graph_unavailable` error means the graph node is not running,
and the deploy log will say why.

### Load

```bash
# 100 concurrent requests
seq 100 | xargs -P 100 -I{} curl -s -o /dev/null -w '%{http_code}\n' \
  "$BASE/api/stats" | sort | uniq -c
```

Expect a mix of `200` and, past the limit, `429` with a `Retry-After` header.
No `5xx`, and the service still answering afterwards.

---

## 4 · Confirm the graph survives a redeploy

There is no disk to verify any more; that is the point. What is worth verifying
is that the rebuild runs, because a redeploy is when it would stop.

```bash
curl -sS "$BASE/api/stats" | jq '.packages, .edges'      # the crawl, in Postgres
curl -sS "$BASE/api/blast?name=debug&depth=5" | jq '.total'
```

Render Dashboard → **Manual Deploy** → *Deploy latest commit*. Wait for live,
then run both again. `/api/stats` reads the sidecar and must be unchanged;
`/api/blast` reads the graph and must come back non-zero. Zero means the rebuild
failed — the deploy log carries the reason, and the most common one is a
`DATABASE_URL` the service cannot reach.

---

## The sidecar: resolved

This section used to open "**this is unresolved and you need to decide it**" and
set out three ways to deal with two stores that could not be shared. It is
settled, so what follows is a record of what was chosen rather than a decision
left to the reader.

Blast Radius uses two stores by design. Topology — which package requires which
— lives in HydraDB, because a depth-5 reverse traversal over a million edges is
what a graph engine is extraordinarily good at. The predicates that decorate it
— declared semver ranges, maintainers, versions, crawl state — live in Postgres,
because those are joins and counts, which a graph engine has no reason to be
good at.

The problem was that the predicates lived in a **SQLite file**: the worker wrote
it, the web service read it on fourteen endpoints, and a Render disk attaches to
exactly one service. The three-service split could not share it.

**Chosen: option A, managed Postgres — Supabase.** Both stores are now reachable
over the network, which is what made the single-service layout possible at all,
and it is what lets the graph be thrown away and rebuilt at boot. Accounts, API
keys, monitors and alerts moved to the same Postgres and so now survive a
deploy; they previously did not, which is why nobody stayed logged in.

Use the **pooler** endpoint (`aws-0-<region>.pooler.supabase.com:6543`), not the
direct `db.<ref>.supabase.co` one — the direct endpoint holds a real backend per
connection and resolves on fewer networks. The pooler runs in transaction mode,
where consecutive statements can land on different backends, so psycopg's
server-side prepared statements are disabled outright in `sidecar.py`. Without
that, the first request of each kind succeeds and a later identical one fails
with `DuplicatePreparedStatement`.

Supabase, when configured for auth, owns **only the credential** — email and
password. Everything else is this instance's. That boundary is deliberate and
documented in [PLATFORM.md](PLATFORM.md); do not split it further.

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

`render.yaml` ships `plan: starter` — 512MB — and the numbers below are measured
in the real container at that size, not estimated.

| | resident |
| --- | --- |
| graph node, 137,688 edges (the whole crawl) | ~812MB |
| graph node, 55,000 edges | ~303MB |
| everything: graph + gunicorn + rebuild peak | ~330MB of 512MB |

Which is why `REHYDRATE_MAX_EDGES` is 55,000 here. Three settings work together
to make a 512MB instance viable, and all three are worth raising together on a
larger plan:

* **`REHYDRATE_MAX_EDGES`** — how much graph to rebuild. 2GB fits the whole
  137,688-edge set with room to spare, so set it to `0`/unset there and every
  answer becomes complete.
* **`GRAPH_MEM_BUDGET_MB`** — sizes the graph node's caches. Its own defaults
  assume it owns the machine and allocate a 1GB object-store cache; inside a
  512MB container that walked to 508MB and the cgroup killed it.
* **`LIVE_INGEST` / `LIVE_FEED`** — off here. The crawler holds its visited set
  and frontier in memory and reached 496MB on its own; the changes tail reached
  378MB. Either one leaves no room for the graph, and the kernel then takes
  graph-node, because it is the largest process. Turn both on at 2GB.

**Edges are loaded most-depended-upon first**, so a bounded graph keeps the
packages anyone actually asks about during an incident — plus an explicitly
pinned set of historical compromises that the popularity ranking would
otherwise drop. That pinning is not a nicety. At 55,000 edges the cutoff lands
at 28 dependents, and `event-stream` (11), `ua-parser-js` (16), `rc` (12),
`coa` (1) and `node-ipc` (1) all fall below it — a list of real npm
supply-chain attacks, every one of them evicted by ranking on popularity.
Attackers pick small packages buried in the tree precisely because nobody
watches them, which is the same property that puts them under any cutoff — and
the same property that makes them cheap to keep. Pinning each one's whole
reverse closure costs 1,000 edges, under 2% of the budget, and it is what makes
`event-stream` answer 26 here instead of 0. See `PINNED` in `rehydrate.py`.

**What is exact and what is a floor.** The rebuild writes `/data/rehydrate.json`
naming the packages whose closure it held complete, and `/api/blast` reads it.
Those answer with no caveat. Everything else on a bounded instance carries a
`coverage` object on the response saying depth 1 is exact and the rest is a
floor — `debug` reports 2,382 against a true 3,900, and says so. An unbounded
rebuild has no cutoff, so nothing carries the caveat.

Drive it and read Render's Metrics tab before settling:

```bash
curl -sS "$BASE/api/v1/blast?name=debug&depth=5" -H "Authorization: Bearer <key>"
curl -sS -X POST "$BASE/api/v1/audit" -H "Authorization: Bearer <key>"   --data-binary @package-lock.json
```

Depth-5 traversals are the memory peak; audits are the CPU peak.

---

## Operational notes

**Build minutes are billed.** `.dockerignore` keeps `deps.db`, `tests/`, `docs/`
and `.git/` out of the build context, and `requirements.txt` is installed on its
own layer so a source-only change reuses it.

**The graph is watched.** graph-node is the largest process in the container, so
it is the one the cgroup OOM killer takes, and it dies without logging anything.
Before there was a watchdog, gunicorn simply carried on: `/api/health` answered
200, Render kept the instance in rotation, and the console reported "the
dependency graph is unavailable" indefinitely because nothing was going to
change it. `deploy/app-entrypoint.sh` now polls `/proc` for the process and
sends `SIGTERM` to PID 1 when it goes, so the container exits and Render
restarts it with a freshly rebuilt graph. Expect this in the log:

```
[entrypoint] graph-node (pid 10) is gone — most likely the cgroup OOM
[entrypoint] killer, which leaves no message of its own. Stopping the container
```

If that appears repeatedly, the instance is too small for
`REHYDRATE_MAX_EDGES`; lower it or move up a plan. Restarting is still the right
response — it is the difference between a minute of downtime and an afternoon of
a site that is up and cannot answer.

**`/api/health` returns 200 even when degraded**, deliberately. It reports each
component separately in the body. A health check wired to *dependency* health
restarts the app whenever OSV, Supabase or the network hiccups, which repairs
none of them and takes the working half of the product down too. That has
already cost this deployment one outage. Read `status` and `degraded_reason`,
not the status code.

**The changes cursor.** With `LIVE_FEED` off there is no `_changes` tail to
persist. If you turn it back on at a larger plan, `WORKER_STATE_DIR` is
ephemeral without a disk, so a redeploy re-anchors to *now* and silently skips
everything published during the gap.

**Rollback:** Render keeps previous deploys. Dashboard → the service → Events →
*Rollback*. The graph is rebuilt from Postgres on the way up, so it comes back
with it.
