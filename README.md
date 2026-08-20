# Blast Radius

**npm supply-chain incident response, built on a graph.**
Hack Hydra submission — HydraDB 0.1.0.

When a package is compromised, four questions matter, and none of them is
"which packages are similar to this one":

1. **Who is transitively exposed?** Everything that pulls it, five levels down.
2. **Whose semver range would *actually* have pulled the poison?** Listing a
   dependency is not the same as resolving to the bad version.
3. **Is anything in my project already malicious?** Checked live against the
   advisory database, for any project — no crawl coverage required.
4. **How do I fix it?** The safe version, the `overrides` block, and a brief a
   coding agent can act on.

```bash
docker compose up -d          # HydraDB
py server.py                  # http://127.0.0.1:8000
py cli.py audit ./package-lock.json
```

---

## Why this is a graph problem

The question in an incident is **reachability**: who depends on the compromised
package, who depends on them, and so on. That is a transitive closure over a
directed graph.

A vector index cannot express it at all. "Packages similar to `event-stream`" is
not a question anyone asks while their build is shipping a crypto miner.

But the argument is not really about one traversal. It is about **chaining
across relationship types**. "Which packages does `qix` maintain" is a join.
"How many packages transitively depend on anything `qix` maintains" crosses two
relationship types and changes answer the moment any edge anywhere in the graph
moves. That is a graph question, and it is the one that matters after a
maintainer gets phished.

## Everything is a node

```
(:Package    {id, name, latest})
(:Maintainer {id, name})
(:Advisory   {id, osv_id, severity, is_malware, summary})

(dependency)-[:REQUIRED_BY]->(dependent)      91,544
(:Maintainer)-[:MAINTAINS]->(:Package)         5,184
(:Package)-[:MAINTAINED_BY]->(:Maintainer)     5,184
(:Advisory)-[:AFFECTS]->(:Package)               138
(:Package)-[:HAS_ADVISORY]->(:Advisory)          138
(:Package)-[:SIMILAR_TO]->(:Package)             270
```

27,076 packages · 1,617 maintainers · 136 advisories · ~102,000 edges.

Ids are namespaced through `nid()` — `nid("maint:qix")`,
`nid("adv:MAL-2025-46974")` — so three entity kinds share one integer id space
without colliding.

Every relationship is stored **in both directions**. That is not redundancy: a
variable-length `MATCH` requires a fixed source id, so without the reverse edge
there is no way to ask "who maintains this package" starting from the package.

## The chained traversals

Each stage is anchored at a known vertex because the engine requires it, and the
stages run concurrently.

### 1. What one compromised account can reach

```cypher
MATCH (m {id: $maintainer_id})-[:MAINTAINS]->(p) RETURN p.name
-- then, per package, concurrently:
MATCH (t {id: $package_id})-[:REQUIRED_BY*1..4]->(v) RETURN count(*)
```

> **qix controls 2 packages. 3,484 packages depend on them.** — 803 ms

The union is counted, not the sum: packages by one author share most of their
downstream, and adding the counts double-counts it. The September 2025
chalk/debug attack began with exactly one account.

### 2. Why am I exposed — the actual chain

`GET /api/why-exposed?from=debug&to=express`

The depth at which the target becomes reachable is computed in the graph and is
authoritative. The concrete path cannot be — HydraDB 0.1.0 returns no path
binding — so it is rebuilt from the sidecar edge table and then **every hop is
re-confirmed against the graph**, with `graph_verified` reported honestly.

### 3. Blast radius of a CVE, not a package

```cypher
MATCH (a {id: $advisory_id})-[:AFFECTS]->(p) RETURN p.name
-- then REQUIRED_BY outward from each affected package
```

An advisory usually names several packages. "How far does GHSA-xxxx reach" is a
different question from asking about any one of them.

### 4. Typosquats that already have victims

```cypher
MATCH (p {id: $package_id})-[:SIMILAR_TO]->(q) RETURN q.name
-- then, per neighbour: how many packages already depend on it
```

A near-miss name nobody uses is trivia. One with real dependents means somebody
has already installed the wrong thing.

## The reversed-edge decision

Edges are stored **backwards**: `(dependency)-[:REQUIRED_BY]->(dependent)`.

`express` depends on `debug`, so the natural edge is `express -> debug`. We
store `debug -> express`. The reason is a hard constraint:

```
"variable-length MATCH requires a fixed source id"
```

With natural edges, "who transitively depends on `debug`" means starting from
every possible dependent — an unbounded set of sources, which the engine
refuses. Reversed, the traversal starts at `debug`, and in an incident the
compromised package is the one thing you know for certain. The write path pays a
trivial cost (swap two fields); the read path becomes one legal traversal
instead of an illegal scan.

## Two layers, on purpose

| layer | store | holds |
|---|---|---|
| topology | HydraDB | packages, maintainers, advisories, every edge between them |
| predicates | SQLite (`deps.db`) | declared semver ranges |

Only semver ranges stay out of the graph, and that is forced — HydraDB 0.1.0
cannot filter on edge properties during a traversal:

```
"variable-length relationship bindings are not executable in Query engine"
```

A range stored on an edge would be unreadable at exactly the moment it matters.

## What is live, and what is crawled

Being precise about this matters more than sounding impressive.

| capability | coverage | source |
|---|---|---|
| **Lockfile audit** — is anything in my tree malicious | **all of npm** | osv.dev, live |
| **Package intel** — real, current, compromised | **all of npm** | registry + osv.dev, live |
| **Remediation** — safe version, overrides, agent brief | **all of npm** | osv.dev, live |
| **Typosquat existence check** | **all of npm** | registry, live |
| **Live publish feed** | **all of npm** | replicate.npmjs.com, polled |
| **Blast radius / chained traversals** | 27,076 packages (0.63%) | HydraDB graph |

The universal features need no crawl — your lockfile *is* your tree. The graph
powers the differentiated layer on top. Every API response carries
`graph_coverage`, so the caveat travels with the data instead of living in a
footnote.

## HydraDB 0.1.0: constraints we hit and engineered around

The organizers asked people to surface what does not work. Everything here was
found empirically; [`probe_constraints.py`](probe_constraints.py) prints a
PASS/FAIL table and flags any row where a constraint no longer holds.

### Rejected outright

| what we tried | what HydraDB said | what we did instead |
|---|---|---|
| string vertex ids | `field id must be a non-negative integer` | `nid()` derives a 51-bit int from the name |
| `CREATE INDEX` | `expected query, got CREATE INDEX` | no index needed — ids are derived |
| `MATCH … MERGE` in `UNWIND` | `UNWIND MATCH must end in RETURN or DELETE` | `CREATE` with explicit ids |
| bare `MERGE … SET` | `MERGE with following clauses is not executable` | every write goes through `UNWIND` |
| `length(path)` | `RETURN supports <binding>.<property> or count(*)` | difference `count(*)` per depth |
| var-length `MATCH`, no fixed source | `requires a fixed source id` | reversed edges |
| edge property filters mid-traversal | `relationship bindings are not executable` | ranges live in SQLite |
| bare `MATCH (n)` | `requires an id, label, or property predicate` | always scope by label or id |
| `count(DISTINCT x)` | `DISTINCT aggregate arguments are not executable` | `RETURN DISTINCT` + count client-side |
| `*1..$depth` as a parameter | `requires an explicit max hop` | interpolate a clamped integer |
| result limit > 100,000 | `query_result_limit rejected by admission control` | clamp to `RESULT_LIMIT` |
| `feed=continuous` (npm, not HydraDB) | `400 Bad Request` | poll `_changes?since=<seq>` |

### The ones that fail silently — far more expensive

**1. An id-only `MATCH` can never say "no".**
`MATCH (p {id: $id}) RETURN p.name` returns a row of nulls for an id never
written. Scoping to `:Package` makes it an existence test. Until we found this,
every unknown package reported an empty blast radius — which reads as *safe*,
the worst possible way to be wrong.

**2. `{"type": "null"}` has no `value` key.**
Other typed values are `{"type": "string", "value": "debug"}`. A client that
unwraps only two-key dicts leaves a null as a truthy dict, so an absent property
reads as present. This compounded trap 1 exactly.

**3. Results page at 1024 rows.**
Every response carries `next_cursor`; ignoring it silently truncates every large
answer. Continuing a page needs **both** the cursor and the originating
`query_id`.

**4. `count(*)` on a variable-length match counts reachable vertices, not paths.**
Good news, and load-bearing: it makes the depth histogram exact and cheap.
Verified against a deliberately diamond-shaped graph in
[`probe_counts.py`](probe_counts.py).

**5. A restarted store is permanently read-only.**
SlateDB cannot update an existing manifest on the local filesystem backend:
`Operation put_opts with mode PutMode::Update not yet implemented`. Reads stay
perfect; every write returns 500 forever. Only a write reveals it, so
`/api/health` round-trips one on a timer and [`rebuild.py`](rebuild.py) replays
the whole graph from the sidecar in about a minute.

**6. `/readyz` is not a readiness signal for queries.**
It returns 200 within a second of a restart while deep traversals still fail the
engine's own 30-second query ceiling. The server warms itself, walking depths
1→5 in order because each depth caches the pages the next needs.

**7. Traversal cost scales with *total store size*, not the edges walked.**
This one nearly sank the project. Loading all 77,232 maintainer links (plus the
reverse) tripled the store to ~246,000 edges, and depth-4 and depth-5
`REQUIRED_BY` traversals — which never touch a maintainer edge — stopped
completing at all. After capping maintainer edges to packages with ≥10
dependents (~102,000 edges total), depth 5 completes in **860 ms on a cold
store, first attempt**. There is a practical ceiling here, and it is a property
of the store, not of the query.

## Two accuracy bugs, both erring toward alarm

The alarming direction is the dangerous one for a security tool.

**Classifying malware from advisory prose.** Scanning summaries and details for
words like "malicious" labelled **`express` as malware**, because ordinary
vulnerability write-ups describe attacker behaviour in exactly those words.
Classification now reads structured fields only: a `MAL-` id, a
`malicious-packages-origins` record, or **CWE-506 (Embedded Malicious Code)** —
which both the 2018 event-stream backdoor and the 2025 debug takeover carry.

**Querying OSV without a version.** A version-less query returns every advisory
ever filed against a package across its whole history, so every popular package
looked compromised. `assess()` now resolves `latest` first and asks about that.
**`debug@4.4.2` is malicious; `debug@4.4.3` is clean.**

The same lesson hit the tarball scanner, which rated **`esbuild`** as
`malicious_pattern` — a compiler legitimately runs a postinstall and spawns
processes. Capability and intent are now separate: the scan reports what a
package *can do*, and only the diff against the previous release reports what
this version *added*.

## The advisory outlives the artifact

`debug@4.4.2` and `ua-parser-js@0.7.29` have both been **unpublished from npm**.
The publish timestamp survives in `time` while the release is gone from
`versions` — itself a strong signal, and detected and reported as such.

- OSV is the **durable** layer; the bytes are not.
- Tarball analysis is a **pre-disclosure** tool, not a forensic one.
- A lockfile still pinning that version will now *fail to install*, which is
  often how a team first notices.

## Benchmarks

Full numbers in [BENCHMARKS.md](BENCHMARKS.md). The short version: the identical
transitive closure is computed in HydraDB and in a SQLite recursive CTE, the
counts are **cross-checked before the times are compared**, and both engines
agree on every row. **SQLite is 7–10× faster at this graph size**, and that
result leads the document rather than hiding in it.

A rigged table would be worthless in a pitch. What the graph model buys here is
not raw speed at 27k vertices — it is that `-[:REQUIRED_BY*1..5]->` stays one
clause as the depth bound changes, against a hand-written fixpoint whose
correctness depends on getting `UNION` vs `UNION ALL`, depth accounting and
cycle termination right. Both implementations are in this repo; compare them.

## Setup (Windows)

Tested on Windows 11, Python 3.14, Docker Desktop. Use `py`, not `python3`.

```powershell
mkdir .hydradb\store, .hydradb\cache
"local-development-token-32-bytes" | Out-File -Encoding ascii .hydradb\auth-token
docker compose up -d
py -m pip install -r requirements.txt
py hydra.py                       # waits for readiness, round-trips a write

# Build the graph: crawl npm (~20 min), or replay a deps.db you already have
py expand_seeds.py --out seeds_expanded.txt --target 80000
py ingest.py --seeds seeds_expanded.txt --max-packages 40000 --max-versions 5
py graphify.py                    # maintainers, advisories, similarity
#   ...or, with deps.db in hand:  py rebuild.py --yes

py server.py                      # http://127.0.0.1:8000
```

The console works while the crawl is still running: a package not yet reached
returns an explicit `not_in_graph` with current progress, never an empty result
that looks like safety.

## Verifying it

```powershell
py -m pytest tests -q      # 117 tests
py verify.py               # drives the live stack, per-endpoint success + latency
py verify.py --soak 300    # sustained load, measured success rate
py web_audit.py            # clicks all 39 controls, asserts each did something
py chaos.py                # stops HydraDB, proves the recovery
py demo_check.py           # pre-recording gate: 21 checks
```

`web_audit.py` earned its place by finding a real defect: the autocomplete
dropdown was painting *underneath* the preset chips, so a click in the overlap
selected a chip instead of the suggestion. It now asserts against
`elementFromPoint`, so "visible to a selector but unclickable to a person"
cannot pass again.

## Use it in CI

The CLI exits non-zero when your tree contains malware, which is what makes it
able to fail a build.

```bash
py cli.py audit ./package-lock.json     # also yarn.lock and pnpm-lock.yaml
py cli.py audit ./pnpm-lock.yaml --json
py cli.py blast debug --depth 5
py cli.py intel debug 4.4.2
py cli.py fix debug 4.4.2
```

| exit | meaning |
|---|---|
| `0` | clean |
| `1` | something in the tree is **confirmed malicious** |
| `2` | known vulnerabilities (only with `--fail-on vuln`) |
| `3` | the scan could not be completed |

Malware and vulnerabilities are separate codes on purpose: one means somebody
attacked you, the other should not wake anyone at 2am.

```yaml
# .github/workflows/supply-chain.yml
name: supply chain
on: [push, pull_request]
jobs:
  blast-radius:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - run: pip install -r requirements.txt
      - run: python cli.py audit ./package-lock.json
```

`audit` talks only to osv.dev and the npm registry — no HydraDB, no crawl — so
it runs in a bare CI container.

## API

| endpoint | answers |
|---|---|
| `GET /api/health` | per-component liveness, uptime, cache, OSV reachability |
| `GET /api/stats` | graph size, crawl progress |
| `GET /api/events` | SSE: live system state **and** npm publishes |
| `GET /api/feed` | recent npm publishes, enriched with reach |
| `GET /api/blast` | victims + depth histogram |
| `GET /api/subgraph` | drawable slice: nodes by depth + edges |
| `GET /api/expand` | one node and its neighbours, every edge type |
| `GET /api/attack-surface` | Maintainer → Package → blast radius |
| `GET /api/why-exposed` | the verified chain between two packages |
| `GET /api/blast-advisory` | Advisory → Package → blast radius |
| `GET /api/typosquat-risk` | similar names ranked by dependents |
| `GET /api/intel` | is a package real, current, compromised |
| `POST /api/audit` | scan a lockfile against osv.dev (4 formats) |
| `GET /api/fix` | safe version, overrides, agent brief |
| `GET /api/scan` | read the published tarball, diff releases |
| `GET /api/resolve` | exposed vs shielded by pin |
| `GET /api/maintainers` | what else those maintainers publish |
| `GET /api/search` | package name autocomplete |

Every response carries `latency_ms` measured around the real query, plus `ok`,
`source`, `graph_coverage`, `cached` and `request_id`. Interactive docs at
`/api/docs`.

## Demo safety

`DEMO_MODE=1` serves responses captured from real runs. Independently of that
flag, a capture is used as a **fallback** whenever a live call fails, so a
dropped connection shows the real answer from ten minutes ago instead of a stack
trace. Anything served that way is flagged `demo: true` — a fixture presented as
a live query would be a lie. Verified by stopping HydraDB entirely: the console
kept answering.

## Layout

```
hydra.py               HydraDB client: nid(), cursor paging, retry policy, budgets
ingest.py              npm crawler -> HydraDB + deps.db sidecar
graphify.py            maintainers, advisories, similarity -> graph nodes
blast.py               blast radius, lockfiles, npm semver
chains.py              the traversals that cross edge types
intel.py               live registry + OSV: real, current, compromised
scan.py                tarball static analysis + version diffing
lockfiles.py           npm / yarn v1 / yarn berry / pnpm
feed.py                live npm publish poller
server.py              FastAPI: 18 endpoints, serves the console on one port
cli.py                 CI-usable CLI with meaningful exit codes
web/                   the console — vanilla HTML/CSS/JS, no build step
bench.py               HydraDB vs SQLite recursive CTE -> BENCHMARKS.md
rebuild.py             replay the graph from deps.db
verify.py / web_audit.py / chaos.py / demo_check.py
probe_constraints.py / probe_counts.py
tests/test_all.py      117 tests
```

The console has **no build step and no JavaScript dependencies** — the radial
map, the force-directed explorer and the live ticker are all hand-rolled. Partly
taste, partly demo safety: a CDN script tag is the one dependency that can fail
while you are recording.

## Data

Package metadata from the [npm registry](https://registry.npmjs.org), fetched
live. Advisories from [OSV.dev](https://osv.dev). Publish feed from
`replicate.npmjs.com`. npm registry data is provided by npm, Inc.

The incidents referenced (`debug`/`chalk` via the qix account takeover,
September 2025; `event-stream`, November 2018; `ua-parser-js`, October 2021) are
real, publicly documented attacks. The blast radius figures shown for them are
computed live from the crawled graph.

## Known limitations

- The graph holds 27,076 packages, not all 4.3M. Traversal features are limited
  to that corpus; audit/intel/fix are not.
- There is a practical store-size ceiling around ~100k edges before depth-5
  traversals stop completing (constraint 7 above).
- Graph edges come from each package's **latest** version's `dependencies`.
  `peerDependencies` are recorded but are not edges — npm does not install them
  transitively, so counting them would overstate exposure.
- Semver covers `^ ~ >= > <= < = * x ||` and hyphen ranges. Git URLs, `file:`,
  `npm:` aliases and workspace protocols return `False` rather than guessing.
- The live feed polls; npm rejects `feed=continuous`. It runs seconds behind.
- Maintainer edges exist only for packages with ≥10 dependents.
- `nid()` collisions are theoretically possible (~2e-6 over 100k names); the
  crawler records the name→id map and the suite asserts zero collisions across
  the entire corpus.

## License

MIT — see [LICENSE](LICENSE).
