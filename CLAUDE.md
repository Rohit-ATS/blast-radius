# Blast Radius — Hack Hydra submission

Supply-chain incident response over an npm dependency graph in HydraDB.
Deadline: Aug 20, 11:59 PM PT. Solo build. Ship working over complete.

## What exists already — read before writing anything
- `hydra.py` — HydraDB client over the HTTP query API
- `ingest.py` — npm registry crawler, populates the graph, runs in background
- `blast.py` — the five incident queries + semver logic (tested, do not rewrite)
- `PLAN.md` — hour-by-hour plan
- `SPEC.md` — build brief for the console

## Rules
- HydraDB runs at localhost:8443, token `local-development-token-32-bytes`
- Do not rewrite hydra.py / blast.py / ingest.py — extend them
- No mocked or placeholder data anywhere. Every number comes from a real query.
- No localStorage, no frontend frameworks, no build step.
