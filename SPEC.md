# SPEC — paste this into Claude Code

## Build brief

> Build a single-page incident-response console called **Blast Radius**.
>
> Backend: FastAPI in `server.py`. It imports the existing `hydra.py` and
> `blast.py` in this repo — do not rewrite them, only add endpoints. Read
> `blast.py` first; the query functions all return `(rows, latency_ms)` and
> that latency must reach the frontend.
>
> Endpoints:
> - `GET  /api/blast?name=&depth=` → depth histogram + victim list + latency_ms
> - `POST /api/lockfile` (body: raw package-lock.json text, `name`, `bad_version`)
>   → exposure verdict + paths + latency_ms
> - `GET  /api/resolve?name=&bad_version=` → exposed vs shielded-by-pin counts
> - `GET  /api/maintainers?name=` → sibling packages by maintainer
> - `GET  /api/typosquats?name=` → near-miss names that exist in the registry
> - `GET  /api/stats` → total Package, Release, REQUIRES, DEPENDS_ON counts
>
> Frontend: one `index.html`, vanilla JS, no build step. Dark terminal
> aesthetic — monospace, near-black background, one accent colour for
> severity. Not a dashboard template; this should look like a security tool
> someone reaches for at 3 AM.
>
> Layout, top to bottom:
> 1. Header strip with live graph stats from `/api/stats`.
> 2. Search: package name + version + depth slider (1–6). Three preset
>    "incident" buttons that fire the whole flow with zero typing.
> 3. **Result headline: the latency, rendered very large** — e.g.
>    `210ms` with `depth 5 · 1.4M edges traversed` beneath it. This is the
>    hero element of the page, not a footnote.
> 4. Depth histogram as horizontal bars: depth 1 → N packages, depth 2 → N.
> 5. Two columns: exposed packages (left, scrollable), and a right rail with
>    the maintainer pivot and typosquat ring.
> 6. Lockfile drop zone. On drop, show a single unambiguous verdict banner:
>    EXPOSED (red) / SHIELDED BY PIN (amber) / CLEAR (green), then the exact
>    dependency path that reaches the compromised package.
>
> Constraints: no localStorage, no frameworks, no CSS framework. Every number
> on screen must come from a real query — no placeholder or mocked values
> anywhere, including empty states.

## Demo video script — 2:50

Record in one take with the console already loaded and the graph already
ingested. No slides. No talking-head intro.

**0:00–0:20 — the problem, concretely**
> In May, the TanStack CI pipeline was breached. Eighty-four malicious
> artifacts across forty-two packages, published in six minutes. The worm went
> on to hit Mistral, UiPath, and a hundred and sixty other packages. If you are
> defending, your entire job in that window is one question: which of my
> services are exposed, right now?

**0:20–0:40 — why existing tools fail**
> That is a transitive reverse-dependency closure over a graph with tens of
> millions of versioned nodes. Every AI dev tool shipping today indexes code
> as embeddings and retrieves by similarity. Similarity cannot answer this.
> Not badly — at all. It is a traversal, so it needs a graph database.

**0:40–1:40 — live demo, no narration of the UI**
Click the preset incident. Let the number land on screen. Then:
> Two hundred and ten milliseconds. Depth five, across [N] million edges.
> Three hundred and forty packages exposed at depth one, twenty-one thousand
> by depth five.

Drop the lockfile. Let the verdict render.
> This is a real `package-lock.json`. Not exposed at depth one — exposed at
> depth four, through this path. That is the part nobody's tooling tells you.

Show the semver panel.
> And of everyone who depends on it, only these resolve to the malicious
> version. The rest pinned. Listing a dependency and pulling it are different
> facts, and conflating them is how you end up paging forty teams who were
> never at risk.

Show the maintainer pivot.
> Same maintainer also controls these eleven packages. That is not where the
> attack was. That is where it goes next.

**1:40–2:30 — HydraDB, specifically**
> Two layers in one graph. A collapsed package-level `REQUIRES` layer so
> traversal is a single variable-length hop and stays flat as depth grows, and
> a version-precise `Release`/`DEPENDS_ON` layer underneath so the forensic
> questions stay answerable. Traversal speed and forensic precision want
> different shapes, so I store both and let the planner pick.
>
> Reverse adjacency and GraphBLAS traversal are what make the incoming-edge
> closure cheap — that direction is the expensive one in most stores. Every
> query runs against one pinned snapshot, so the numbers on screen are
> internally consistent even while the crawler is still writing. Without
> HydraDB this is a recursive CTE that takes [X] seconds, and I measured that.

**2:30–2:50 — close**
> Blast Radius. Full ingestion pipeline, real npm data, five queries a vector
> index cannot express. Repo is public, MIT, README explains the model.

## Submission form answers — draft now, paste later

**Project name:** Blast Radius

**Short description:** Supply-chain incident response over a live npm
dependency graph in HydraDB. Answers who is transitively exposed, whose semver
ranges would actually have resolved the malicious version, and what the
compromised maintainer controls next — in milliseconds, at depth five.

**Problem:** When a package is compromised, defenders have minutes to
determine blast radius. Current tooling indexes code by embedding similarity,
which cannot express a transitive reverse-dependency closure. The question is
a graph traversal and needs a graph database.

**How it uses the HydraDB OSS repo:** HydraDB is the only datastore. A
two-layer graph model — collapsed `Package-[:REQUIRES]->Package` for traversal,
precise `Release-[:DEPENDS_ON]->Package` for version forensics — is ingested
from the live npm registry and queried through the OpenCypher HTTP API, using
variable-length reverse traversal and `algo.SSpaths` for the deep closure.
Remove HydraDB and there is no product: every one of the five queries is a
multi-hop traversal, and the depth-5 reverse closure is the entire pitch.

**Tech stack:** HydraDB (OpenCypher over HTTP), Python, FastAPI, vanilla JS,
npm public registry API, Docker.
