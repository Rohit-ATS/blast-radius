# Blast Radius

**npm supply-chain incident response over a graph database.**
Built in one day for Hack Hydra, on HydraDB 0.1.0.

When a package is compromised, three questions matter and none of them is
"which packages are similar to this one":

1. **Who is transitively exposed?** Not just direct dependents — everything that
   pulls it five levels down, with a breakdown of how deep the damage goes.
2. **Whose semver range would *actually* have pulled the poison?** Listing a
   dependency is not the same as resolving to the bad version. The difference
   between those two numbers is the difference between a scary headline and a
   true one.
3. **Is *my* app affected?** Drop in a real `package-lock.json` and get one
   word — EXPOSED, SHIELDED, or CLEAR — plus the exact dependency path.

Everything on the page is a real query against a real crawl of the npm
registry. There is no seeded data, no fixture, and no placeholder anywhere in
the UI, including in loading and empty states.

```
py server.py     →     http://127.0.0.1:8000
```

---

## Why this is a graph problem

The question in an incident is **reachability**: who depends on the
compromised package, and who depends on them, and so on. That is a transitive
closure over a directed graph.

A vector index cannot express it at all — "similar to `event-stream`" is not a
question anyone asks while their build is shipping a crypto miner. A relational
database can express it, as a recursive CTE, and we measured exactly that. See
[BENCHMARKS.md](BENCHMARKS.md), which reports that **SQLite beat HydraDB by
3–13× at this graph size** and explains why. That result is in this repo
because it is true, not because it flatters the submission.

What the graph model buys is not raw speed at 27k vertices. It is that the
query is one clause — `-[:REQUIRED_BY*1..5]->` — that stays one clause as the
depth bound changes, against a hand-written fixpoint whose correctness depends
on getting `UNION` vs `UNION ALL`, depth accounting, and cycle termination
right. Both implementations are in this repo. Compare them.

## The reversed-edge design decision

Edges are stored **backwards**:

```cypher
(dependency)-[:REQUIRED_BY]->(dependent)
```

That reads wrong. `express` depends on `debug`, so the natural edge is
`express -> debug`. We store `debug -> express`.

The reason is a hard constraint in HydraDB 0.1.0: **a variable-length `MATCH`
only executes when the source id is fixed.**

```
"variable-length MATCH requires a fixed source id"
```

With natural edges, "who transitively depends on `debug`" means starting from
every possible dependent and walking *towards* `debug` — an unbounded set of
sources, which the engine refuses. With reversed edges, the traversal starts at
`debug` itself, and in an incident the compromised package is the one thing you
know for certain.

So the direction is chosen to put the known quantity where the engine requires
a constant. The write path pays a trivial cost (swap two fields) and the read
path becomes a single legal traversal instead of an illegal scan.

## The two-layer design

| layer | store | holds | why |
|---|---|---|---|
| topology | HydraDB | `(:Package {id, name, latest})`, `REQUIRED_BY` edges | reachability is the graph's job |
| predicates | SQLite (`deps.db`) | declared semver ranges, per-release deps, maintainers | HydraDB 0.1.0 cannot filter on edge properties mid-traversal |

The second row is not a design preference, it is a constraint. Attempting to
filter an edge property during a variable-length traversal returns:

```
"variable-length relationship bindings are not executable in Query engine"
```

A declared range like `^4.1.0` would therefore be unreachable at exactly the
moment it matters. So ranges live in SQLite, where "whose range admits 4.4.2"
is an indexed scan, and the graph holds only what it is good at.

Both stores are written from the same batch in the same crawler pass, so their
counts track each other — which is also how the live header stays fast while
HydraDB's own `count(*)` takes twelve seconds (see below).

## HydraDB 0.1.0 constraints we hit and engineered around

The organizers asked people to surface what does not work. Every line here was
found empirically and is reproducible by running
[`probe_constraints.py`](probe_constraints.py), which prints a PASS/FAIL table
and flags any row where the constraint no longer holds.

### Rejected outright

| what we tried | what HydraDB said | what we did instead |
|---|---|---|
| string vertex ids | `UNWIND row 0 field id must be a non-negative integer` | `nid()` derives a 51-bit integer id from the package name (`hydra.py`) |
| `CREATE INDEX ON :Package(name)` | `expected query, got CREATE INDEX` | no index needed — ids are derived, so lookup is a pure function |
| `MATCH … MERGE` inside `UNWIND` | `UNWIND MATCH must end in RETURN or DELETE` | `CREATE` with explicit ids, which binds to the existing vertex |
| bare `MERGE … SET` outside `UNWIND` | `MERGE with following clauses is not executable` | every write goes through `UNWIND $rows AS row MERGE …` |
| `length(path)` in `RETURN` | `RETURN currently supports <binding>.<property> or count(*)` | difference `count(*)` at each depth bound to build the histogram |
| variable-length `MATCH` with no fixed source | `variable-length MATCH requires a fixed source id` | reversed edges (above) |
| filtering edge properties while traversing | `variable-length relationship bindings are not executable` | ranges live in the SQLite sidecar |
| bare `MATCH (n)` with no predicate | `node-only MATCH requires an id, label, or property predicate` | always scope by label or id |
| `count(DISTINCT x)` | `DISTINCT aggregate arguments are not executable` | `RETURN DISTINCT x` and count client-side |
| `*1..$depth` as a parameter | `unbounded variable-length MATCH requires an explicit max hop` | interpolate a clamped integer into the query string |
| result limit above 100,000 | `query_result_limit rejected by admission control` | clamp to `RESULT_LIMIT` in `hydra.py` |

### Traps that fail silently — the expensive ones

These cost the most time, because nothing errors. They just quietly return a
wrong answer.

**1. An id-only `MATCH` can never say "no".**

```cypher
MATCH (p {id: $id}) RETURN p.name     -- returns a row of nulls for an id
                                      -- that was never written
```

Addressing a vertex by id materialises the slot whether or not anything is
there, so this is not an existence test. Scoping to `:Package` makes it one.
Until we found this, every unknown package reported an empty blast radius —
which reads as *safe*, the worst possible way to be wrong.

**2. `{"type": "null"}` is not the same shape as other typed values.**

HydraDB returns typed values as `{"type": "string", "value": "debug"}`, but a
null comes back as `{"type": "null"}` with **no `value` key**. A client that
unwraps only two-key dicts leaves it as a truthy dict, so an absent property
reads as present. This compounded trap 1 exactly.

**3. Results are paged at 1024 rows.**

Every response carries `next_cursor`. Ignoring it silently truncates every
large answer — our victim lists were capped at 1024 while `count(*)` correctly
reported thousands. Continuing a page needs **both** the cursor and the
originating `query_id`; the cursor alone is rejected with `result cursor does
not belong to this query request`.

**4. `count(*)` on a variable-length match counts *reachable vertices*, not paths.**

This one is good news, and worth knowing. Verified against a deliberately
diamond-shaped graph in [`probe_counts.py`](probe_counts.py): a vertex
reachable by two distinct paths is counted once. That is what makes the depth
histogram cheap — ask for the cumulative reach at each bound and difference the
series — and it is why the histogram is exact rather than an estimate.

**5. Whole-graph counts are full scans, and there is no index to fix it.**

`MATCH (p:Package) RETURN count(*)` plus the edge count took **12.7 seconds**
on an idle 27k-vertex graph, and **97 seconds** on the same graph while the
crawler was still writing to it. HydraDB says so itself in its logs:

```json
{"level":"WARN","message":"query plan warrants attention",
 "reason":"full_scan","hydradb.query.access_path":"AllVertexScan"}
```

The live header polls every four seconds, so this cannot sit on a request path.
The server now serves graph size from the sidecar and re-measures the real
graph on a background thread, reporting the measurement's age. The number stays
real; it just stops blocking.

**6. A restarted store is permanently read-only — and only a write reveals it.**

This is the most operationally severe one, and it is completely silent from the
read path:

```
object store error: Operation `put_opts` with mode `PutMode::Update`
not yet implemented by LocalFileSystem(file:///data/store)
```

HydraDB's SlateDB backend creates its manifest on first boot (`PutMode::Create`,
fine) and must *update* it on every boot after that (`PutMode::Update`, not
implemented for the local filesystem). The result: after the first container
restart, every write returns `500 internal query execution error`, forever,
while reads keep answering perfectly and at full speed.

Serving is read-only, so the console is unaffected — but the crawler cannot
write another row, and you find out 20 minutes into a re-crawl. So:

- `/api/health` round-trips a real write on a timer and reports `writable`.
  Nothing else can detect this.
- `ingest.py` refuses to start against a read-only store and says what to do.
- [`rebuild.py`](rebuild.py) replays the whole graph out of the sidecar in
  about a minute, with no network access, because `deps.db` already holds every
  vertex and edge that was written. Recovering does not require re-crawling npm.

**7. `/readyz` is not a readiness signal for queries.**

Measured after a container restart: `/readyz` returns 200 at **t+0.6s**, but a
depth-5 traversal does not complete until **t+93s**, failing HydraDB's own
30-second query timeout (`408 query_timeout`, `client_query_runtime exceeded`)
on every attempt in between. There is a server-side 30s ceiling on any single
query, and a cold store cannot beat it on a deep traversal.

The server therefore warms the cache itself — walking depths 1→5 in order,
because each depth caches the pages the next one needs — and a supervisor
re-warms whenever a probe fails, so a restart heals without anyone noticing.
While it is warming, requests get a `graph_warming` 503 that says so, and the
console retries rather than flashing an error.

### Also worth knowing

- `consistency` accepts only `causal` and `strong`. `strong` measured ~30×
  slower on the same traversal; `causal` is the right default.
- `algo.SSpaths` exists and requires `sourceNode` plus a non-empty `relTypes`,
  but returns a single path regardless of `resultLimit` — it is not a
  reachability primitive.
- `DISTINCT`, `LIMIT`, `ORDER BY`, `WHERE` on node properties, and
  `DETACH DELETE` by id all work fine.
- `RUST_MIN_STACK: 33554432` is required in the container environment, or the
  node serves `/readyz` and then aborts on the first real query.
- `408` is a *retryable* status here, unlike most 4xx: it is HydraDB's own query
  timeout, and the identical query succeeds in about a second once the working
  set is cached. Treating it as a client error (as a naive `< 500` check does)
  turns a warm-up into a hard failure.

## Setup (Windows)

Tested on Windows 11 with Python 3.14 and Docker Desktop. Use `py`, not
`python3`.

```powershell
# 1. HydraDB
mkdir .hydradb\store, .hydradb\cache
"local-development-token-32-bytes" | Out-File -Encoding ascii .hydradb\auth-token
docker compose up -d
py hydra.py                       # waits for readiness, round-trips a write

# 2. Dependencies
py -m pip install -r requirements.txt

# 3. Crawl the registry (runs for ~20 min, resumable, safe to re-run)
py expand_seeds.py --out seeds_expanded.txt --target 80000
py ingest.py --seeds seeds_expanded.txt --max-packages 40000 --max-versions 5

# 4. Serve the API and the console on one port
py server.py                      # http://127.0.0.1:8000
```

The crawl is resumable: progress is checkpointed to `.crawl_state.json` every
batch, and re-running merges any new seeds into the queue without losing work.
**The console works while the crawl is still running** — a package that has not
been reached yet returns an explicit `not_in_graph` with the current crawl
progress, rather than an empty result that looks like safety.

### Verifying a running system

```powershell
py -m pytest tests -q            # 108 tests
py verify.py                     # drives the live stack end to end
py verify.py --soak 300          # sustained load, reports the measured rate
py chaos.py                      # kills HydraDB and checks the recovery
py rebuild.py --verify           # is the graph still writable?
```

`verify.py` is not a unit test: it exercises the actually-running system with
real package names read out of the live graph, and reports a per-endpoint
success rate and latency distribution. `chaos.py` stops the database underneath
the server and asserts that the outage is clean, that sidecar-backed endpoints
keep serving, and that the API recovers on its own without a restart.

Layered so a partial environment still reports usefully: pure semver and
lockfile tests always run; graph, API, and browser layers skip with a reason if
HydraDB, the server, or Chrome is unavailable. The browser layer drives the
real page with Playwright and asserts zero console errors.

### Benchmarks

```powershell
py bench.py --runs 5             # writes BENCHMARKS.md
```

Computes the identical closure in both engines and **cross-checks the counts
before comparing times** — a speed comparison between two systems computing
different numbers would be worthless. They agree on every row.

## What HydraDB is actually doing here

Strip out the sidecar and the console and the load-bearing query is this:

```cypher
MATCH (t {id: $id})-[:REQUIRED_BY*1..5]->(v) RETURN DISTINCT v.name
```

One bounded traversal from one known vertex, returning every package that
transitively depends on the compromised one. `/api/blast` runs that plus one
`count(*)` per depth level, concurrently, to build the histogram.

**Without HydraDB** the topology layer is gone: no reachability query, no depth
histogram, and the lockfile check loses the graph-side verdict it intersects
against. The sidecar alone can answer "who declares a dependency on X" but the
transitive closure is precisely the part that is not a single indexed lookup.

## Layout

```
hydra.py               HydraDB client: nid(), cursor paging, retry policy, budgets
ingest.py              npm crawler -> HydraDB + deps.db sidecar
blast.py               the five incident queries + npm semver range logic
server.py              FastAPI: six endpoints, serves the console on the same port
web/                   the console — vanilla HTML/CSS/JS, no build step
bench.py               HydraDB vs SQLite recursive CTE -> BENCHMARKS.md
expand_seeds.py        widens the crawl frontier from the npm search API
probe_constraints.py   the constraint table above, as a runnable PASS/FAIL check
probe_counts.py        proves count(*) counts vertices, not paths
rebuild.py             replay the graph from deps.db when the store goes read-only
verify.py              end-to-end verification of the running system
chaos.py               fault injection: kill HydraDB, prove the recovery
tests/test_all.py      108 tests
```

## API

| endpoint | answers |
|---|---|
| `GET /api/health` | per-component liveness: hydradb, sidecar, warm-up, writability |
| `GET /api/stats` | graph size, crawl progress |
| `GET /api/blast?name=&depth=` | victims + depth histogram + latency |
| `GET /api/resolve?name=&bad_version=` | exposed vs shielded by pin |
| `POST /api/lockfile?name=&bad_version=` | EXPOSED / SHIELDED / CLEAR + path |
| `GET /api/maintainers?name=` | what else those maintainers publish |
| `GET /api/search?q=` | package name autocomplete |

Every response carries `latency_ms` measured around the real query. Interactive
docs at `/api/docs`.

## Data

Package metadata comes from the [npm registry](https://registry.npmjs.org)
(`registry.npmjs.org` and its search API), fetched live at crawl time. npm
registry data is provided by npm, Inc. This project stores only package names,
versions, declared dependency ranges, and maintainer handles.

The incidents referenced on the landing page (`debug`/`chalk` via the qix
account takeover in September 2025, `event-stream` in November 2018,
`ua-parser-js` in October 2021) are real, publicly documented supply-chain
attacks. The blast radius numbers shown for them are computed live from the
crawled graph, not from any published incident report.

## Reliability, measured

Uptime is not a claim worth making about the future, so what is claimed here is
what was measured. `verify.py` and `chaos.py` reproduce all of it.

**Under sustained load** the API answers every request. `verify.py --soak`
drives real package names from the live graph across `/api/blast`, `/api/stats`
and `/api/search` continuously and reports the rate it actually achieved.

**When HydraDB is stopped mid-flight** (`chaos.py`, which really does stop the
container), the system degrades honestly rather than breaking:

| behaviour | result |
|---|---|
| graph endpoints during the outage | `503` with an explanation and a hint, in ~18s — never a 500 or a hang |
| `/api/stats`, `/api/search`, `/api/maintainers`, the console | keep serving from the sidecar throughout |
| after the database returns | recovers on its own, no server restart, no intervention |
| during the ~93s cold window | `503 graph_warming`, and the console retries instead of showing an error |

Three fixes came out of running that test rather than reasoning about it:

- **Retry classification.** Retrying a rejected query wasted seconds on every
  syntax error, while a genuinely transient failure — a dropped connection, a
  `408` query timeout — was not retried patiently enough to survive a restart.
  Both directions were wrong; both are now classified.
- **Dead pooled connections.** A keep-alive socket pointing at a process that no
  longer exists produced errors after the database was back. The session is
  dropped on a transport failure so the retry dials fresh.
- **A wall-clock budget.** Five patient retries against a 30-second server-side
  query timeout meant a single request could take over two minutes to admit
  defeat — long after the browser gave up. Request paths now get 20 seconds
  total; the crawler and the warm-up keep the patient behaviour.

## Known limitations

- The crawl reaches ~27k packages, not all 4.3M on npm. BFS from a seed set
  converges quickly; `expand_seeds.py` widens the frontier but the corpus is
  still a popularity-weighted sample.
- Graph edges come from each package's **latest** version's `dependencies`.
  Older releases are kept in the sidecar (`release_deps`) and drive the semver
  check, but the topology is a snapshot of current npm, not history.
- `peerDependencies` are recorded but are **not** edges — npm does not install
  them transitively, so counting them would overstate exposure.
- Semver support covers `^ ~ >= > <= < = * x ||` and hyphen ranges. Git URLs,
  `file:`, `npm:` aliases, and workspace protocols return `False` rather than
  guessing; under-reporting exposure is the safer error.
- **A restarted HydraDB store cannot be written to again** (see constraint 6).
  Serving is unaffected, but re-crawling requires `py rebuild.py` first. This is
  a HydraDB 0.1.0 limitation on the local-filesystem object store, not something
  this project can work around in code.
- After a container restart the graph needs ~93 seconds before deep traversals
  succeed. The server warms itself and reports `graph_warming` meanwhile, but
  the first ~1.5 minutes after a cold start are genuinely degraded.
- `nid()` collisions are theoretically possible (~2e-6 over 100k names). The
  crawler records the name→id map and logs a collision rather than silently
  merging two packages; the test suite asserts zero collisions across the
  entire crawled corpus.

## License

MIT — see [LICENSE](LICENSE).
