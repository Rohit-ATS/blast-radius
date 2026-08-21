# v1.0.0 — Know your blast radius

**When an npm package is compromised, find out who is actually exposed —
before anyone opens an advisory.**

Blast Radius answers the four questions that matter during a supply-chain
incident, against a real dependency graph, with every number coming back from a
query that was actually run. Nothing on any surface is a stored figure, an
estimate, or a mock.

---

## What is in this release

### The console — `/check`

Name a package and a bad version. It walks the graph five hops out, resolves
every declared semver range against that version, and tells you which of the
two numbers actually matters — the scary one (everyone who lists it) or the
true one (everyone whose range would have resolved it).

- Transitive closure with a per-depth histogram
- Semver resolution: **would have pulled it** vs **shielded by a pin**
- Lockfile check — `package-lock.json` v1/v2/v3, `yarn.lock`, `pnpm-lock.yaml`
- Malware audit against [osv.dev](https://osv.dev), including `MAL-` identifiers
- Maintainer pivot: what the attacker owns next
- Typosquat ring: names one edit away that exist on npm right now
- An interactive graph explorer and a live npm publish feed

Deep-linkable as `/check?pkg=debug&ver=4.4.2`, because a check is something you
paste into Slack at 2am.

### The API — `/developers`

A public, versioned contract at `/api/v1`. **Free, no rate limit, no quota, no
card.** It is a façade over the same handlers the console calls, so the two can
never disagree about a number.

```bash
curl -H 'Authorization: Bearer brk_live_...' \
  'https://your-host/api/v1/blast?name=debug&depth=5'
```

Thirteen endpoints covering blast radius, semver resolution, lockfile checks,
malware audits, maintainer pivots, typosquats, subgraphs, monitors, alerts and
webhooks.

The reference is generated from the same table the router reads, so it cannot
drift from the code — and it is served four ways, including Markdown written to
be pasted straight into an AI agent's context:

| | |
| --- | --- |
| `/api/docs.json` | the reference as data |
| `/api/docs.md` | Markdown, for agents and READMEs |
| `/api/docs.txt` | plain text |
| `/api/docs` | OpenAPI / Swagger UI |

The page carries a playground that sends **real authenticated requests** against
the running instance — what you see there is what your code will get.

### Accounts and API keys

- Local password auth (PBKDF2-HMAC-SHA256, 310,000 rounds, per-account salt),
  or **Supabase** by setting two lines in `.env`. Nothing else changes.
- **Keys are never stored.** What is persisted is a SHA-256 digest and a short
  non-secret prefix. A copy of the database yields nothing usable.
- The plaintext secret is shown exactly once, at creation, and never touches
  localStorage or sessionStorage.
- Revocation takes effect on the very next request.
- A per-account security log records every sign-in, key creation, revocation
  and API call, with the address and agent that made it.

### 24/7 monitoring — `/dashboard`

Register a package and the instance keeps measuring it, around the clock. When
its blast radius moves, an alert is raised carrying the before and after counts
so you can gate on the delta rather than the absolute.

Alerts reach you three ways:

- **The dashboard**, over server-sent events — nothing polls
- **Webhooks**, signed the way Stripe and GitHub sign theirs:
  `X-BlastRadius-Signature: t=<unix>,v1=<hmac-sha256>` over `<t>.<raw body>`.
  The timestamp is inside the signed material, so a captured payload cannot be
  replayed. Three attempts with backoff; an endpoint that fails twenty times in
  a row is disabled rather than retried forever.
- **Email** over SMTP, for `high` and `critical` only — a baseline measurement
  is not worth an inbox.

Delivery runs on its own worker behind a bounded queue and can never fail the
traversal that raised the alert.

---

## Getting started

```bash
docker compose up -d          # HydraDB
cp .env.example .env          # optional — Supabase, SMTP
py setup_check.py             # opens a connection to every service
py server.py                  # http://127.0.0.1:8000
```

`setup_check.py` never reads a setting and declares victory. It talks to
Supabase, to the graph, to your SMTP host, and to a webhook endpoint if you give
it one. A green line means that thing worked, just now.

**[SETUP.md](SETUP.md)** says which line each credential goes on.
**[PLATFORM.md](PLATFORM.md)** documents the platform layer.

---

## Engineering notes

**The reversed edge.** Edges are stored backwards on purpose —
`(dependency)-[:REQUIRED_BY]->(dependent)`. HydraDB 0.1.0 will only run a
variable-length `MATCH` when the source id is fixed, and in an incident the one
thing you know for certain is which package was compromised. Reversing the edge
at write time makes that package the source, so the blast radius is one
traversal from a known id instead of a scan.

**Two stores, two shapes.** Topology — who depends on whom — lives in the graph.
Predicates — does `^4.1.0` admit `4.4.2`, who else does this maintainer publish —
live in a SQLite sidecar. That split is why "would have pulled it" versus
"shielded by a pin" is a number this can actually produce.

**An honest benchmark.** At this graph size a recursive CTE over the sidecar
beats the graph engine by 3–13×, and both return the identical count on every
row. That result is in [BENCHMARKS.md](BENCHMARKS.md), including the row that
says a 37k-vertex graph does not need a graph database.

**No build step.** `web/` is plain HTML, CSS and JavaScript. No framework, no
bundler, no `node_modules`.

---

## Bugs this release fixed

Five of these were found by the new test suite rather than by a person:

- `/api/v1` was rate limited despite the documented promise of no limits, which
  would have punished an entire office behind one NAT address together
- CORS blocked `DELETE`, so a cross-origin integrator could create keys,
  monitors and webhooks but never remove them
- `/api/v1/webhooks` was documented but never routed
- `/api/v1` did not inherit the console's response envelope, so successes
  lacked the `ok` field that failures carried — one uniform check was impossible
- `/api/platform-stats` enumerated every configured environment variable name
  and the server's filesystem path
- A `.field` class collision made every form row on the app pages 300px tall
- A `.live` collision inflated the header's 7px status dot into a 40×120 blob

---

## Testing

**381 tests, 1 skipped.**

`tests/test_integration.py` is the one that matters most. It walks the whole
product over real HTTP: sign up, mint a key, call the public API, confirm the
API and the console return identical numbers, register a monitor and a webhook,
wait for the watch to measure it, verify the HMAC on the delivered alert, read
that same alert back through the dashboard *and* the API, then revoke the key
and confirm the door shut. It caught three of the bugs listed above.

```bash
py -m pytest tests -q      # 381 tests
py setup_check.py          # every credential, against the real service
py verify.py --soak 300    # sustained load, measured success rate
py chaos.py                # stops HydraDB, proves the recovery
```

---

**MIT licensed.** Clone it, run it against your own registry mirror, lift the
semver resolver into your own tooling, ship it inside a commercial product.

Built on [HydraDB](https://github.com/hydra-db/hydradb) 0.1.0 for Hack Hydra.
Package data from the [npm registry](https://registry.npmjs.org), advisories
from [osv.dev](https://osv.dev).
