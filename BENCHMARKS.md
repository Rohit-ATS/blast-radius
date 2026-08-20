# Benchmarks

The same question, asked of two engines over the same edges: **which packages transitively depend on X, within N hops?**

Numbers are medians of 5 runs, measured by `bench.py` on the graph as it stood at the time below. Reproduce with `py bench.py --runs 5`.

## What was measured

- **When**: 2026-08-20 15:02:18 Pacific Daylight Time
- **Commit**: `6252762`
- **Machine**: Windows 11, Python 3.14.2
- **HydraDB**: 0.1.0, single node in Docker, HTTP query API on `127.0.0.1:8443`
- **Graph**: 27,076 vertices, 91,544 `REQUIRED_BY` edges
- **Sidecar**: 27,076 package rows, 91,544 prod dependency rows in SQLite (WAL, index on `deps(dst)`)

Both stores hold the same edge set. The crawler writes a graph edge and a sidecar row from the same batch, and only `dependencies` (not `peerDependencies`) become edges in either.

## Headline: at this graph size, SQLite wins

The recursive CTE beat HydraDB on **every row measured**, by between 2× and 12×. That is the opposite of the result this project was built expecting, so it leads the report rather than hiding under it.

| package | depth | packages reached | HydraDB | SQLite CTE | faster | agree |
|---|---|---|---|---|---|---|
| `tslib` | 1 | 892 | 21 ms | 2 ms | SQLite 9.5× | yes |
| `tslib` | 2 | 1,613 | 142 ms | 17 ms | SQLite 8.2× | yes |
| `tslib` | 3 | 2,228 | 327 ms | 33 ms | SQLite 9.8× | yes |
| `tslib` | 4 | 2,389 | 592 ms | 50 ms | SQLite 11.8× | yes |
| `tslib` | 5 | 2,438 | 623 ms | 64 ms | SQLite 9.7× | yes |
| `chalk` | 1 | 827 | 8 ms | 5 ms | SQLite 1.7× | yes |
| `chalk` | 2 | 1,505 | 131 ms | 17 ms | SQLite 7.6× | yes |
| `chalk` | 3 | 1,765 | 243 ms | 28 ms | SQLite 8.8× | yes |
| `chalk` | 4 | 1,871 | 336 ms | 50 ms | SQLite 6.8× | yes |
| `chalk` | 5 | 1,956 | 393 ms | 40 ms | SQLite 9.9× | yes |
| `lodash` | 1 | 783 | 19 ms | 4 ms | SQLite 4.8× | yes |
| `lodash` | 2 | 1,441 | 113 ms | 15 ms | SQLite 7.4× | yes |
| `lodash` | 3 | 1,611 | 226 ms | 22 ms | SQLite 10.4× | yes |
| `lodash` | 4 | 1,637 | 348 ms | 35 ms | SQLite 9.9× | yes |
| `lodash` | 5 | 1,643 | 399 ms | 45 ms | SQLite 8.9× | yes |

`agree` is the column that makes the rest of the table mean anything: both engines return the identical count on every row, so this is two answers to one question rather than two different questions. That agreement is also the strongest correctness check the project has — the reversed-edge graph model and a plain recursive join over the same data reach the same closure.

Transport is not the explanation. The cheapest possible HydraDB round trip — a single-vertex lookup by id — measured **2.9 ms**, so subtracting HTTP and JSON from the traversal numbers leaves the ranking unchanged.

### Why, honestly

- **The graph is small and entirely in memory.** 91,544 edges is a few megabytes; SQLite is walking a B-tree in the page cache, in-process, with no serialisation boundary. A graph engine's advantage shows up when the working set stops fitting that comfortably, and this corpus never gets there.
- **HydraDB 0.1.0 is an early build.** It has no index creation, plans whole-graph counts as full scans, and rejects a large part of OpenCypher. Its traversal cost here grows roughly linearly with the reached set — it is doing real work per hop, not paying a fixed overhead.
- **The CTE was given every advantage**: an index on the join column (`deps(dst)`), `UNION` for dedupe, and the same depth bound. That is the fair comparison, not a strawman with `UNION ALL` left to loop forever on a dependency cycle.

### What the graph still buys

Speed is not the only axis, and pretending otherwise would be the same dishonesty as a rigged table:

- The traversal is *one clause* — `-[:REQUIRED_BY*1..5]->` — and stays one clause as the depth bound changes. The CTE is a hand-written fixpoint whose correctness depends on getting `UNION` vs `UNION ALL`, the depth accounting, and cycle termination right. Both are in this repo; compare them.
- The sidecar only holds a closure-ready edge table because the crawler was written to maintain one. The graph accepts the same edges without a schema designed around one query shape.
- The numbers above are for *counting* the reached set. The moment the question becomes shortest path, or reach constrained by node properties, the CTE grows another self-join per constraint.

A fair summary: **on a 27k-vertex npm graph, this workload does not need a graph database.** It would be easy to leave that sentence out. It is the most useful thing in the report.

### Other caveats

- Both engines were measured with the crawler stopped. Under concurrent writes HydraDB's traversals slow down by several times.
- Medians of a small number of runs on one developer laptop. Treat these as the shape of the difference, not a datasheet.

## The full incident query

`/api/blast` is not one query — it is the reachable-name list plus one bounded count per depth level, fired concurrently, which is how the depth histogram is built without `length(path)` (unsupported) or `count(DISTINCT …)` (also unsupported).

| package | depth | packages exposed | wall clock, all queries |
|---|---|---|---|
| `tslib` | 5 | 2,438 | **876 ms** |
| `chalk` | 5 | 1,956 | **536 ms** |
| `lodash` | 5 | 1,643 | **496 ms** |

## Counting the whole graph

Worth recording because it is the one thing HydraDB 0.1.0 does badly, and it shaped the architecture:

| operation | engine | time |
|---|---|---|
| `MATCH (p:Package) RETURN count(*)` + edge count | HydraDB | **11,957 ms** |
| same two counts | SQLite sidecar | 17.4 ms |

There is no `CREATE INDEX` in HydraDB 0.1.0, so a whole-graph count is a full scan — the server says so itself in its logs (`query plan warrants attention … access_path AllVertexScan`). This is why the live header polls the sidecar and a background thread re-measures the graph on a slow timer: the number is real, but it cannot sit on a request path.

Note the shape of the result: HydraDB is dramatically slower at counting *everything*, and much closer to competitive when traversing from a *known vertex* — three orders of magnitude apart as access patterns go. This project only ever needs the second one, which is why the architecture keeps whole-graph counts off the request path.

## Raw data

```json
{
  "graph": {
    "packages": 27076,
    "edges": 91544,
    "measured_ms": 11956.8
  },
  "sidecar_packages": 27076,
  "sidecar_edges": 91544,
  "graph_scan_ms": 11956.8,
  "runs": 5,
  "depths": [
    1,
    2,
    3,
    4,
    5
  ],
  "rows": [
    {
      "target": "tslib",
      "depth": 1,
      "reached": 892,
      "sqlite_reached": 892,
      "agree": true,
      "hydra_ms": 21.225799981039017,
      "hydra_min": 17.25929998792708,
      "hydra_max": 32.83669997472316,
      "sqlite_ms": 2.2396999993361533,
      "sqlite_min": 2.0146999740973115,
      "sqlite_max": 6.807700032368302,
      "speedup": 0.10551781329028233
    },
    {
      "target": "tslib",
      "depth": 2,
      "reached": 1613,
      "sqlite_reached": 1613,
      "agree": true,
      "hydra_ms": 142.41089997813106,
      "hydra_min": 134.99619998037815,
      "hydra_max": 149.11240001674742,
      "sqlite_ms": 17.39280001493171,
      "sqlite_min": 15.484999981708825,
      "sqlite_max": 18.98950000759214,
      "speedup": 0.12213110104354784
    },
    {
      "target": "tslib",
      "depth": 3,
      "reached": 2228,
      "sqlite_reached": 2228,
      "agree": true,
      "hydra_ms": 327.0873000146821,
      "hydra_min": 313.52830003015697,
      "hydra_max": 339.79790000012144,
      "sqlite_ms": 33.20750000420958,
      "sqlite_min": 32.22619998268783,
      "sqlite_max": 34.883800020907074,
      "speedup": 0.10152488342628704
    },
    {
      "target": "tslib",
      "depth": 4,
      "reached": 2389,
      "sqlite_reached": 2389,
      "agree": true,
      "hydra_ms": 592.2309000161476,
      "hydra_min": 532.2144000092521,
      "hydra_max": 598.4445000067353,
      "sqlite_ms": 50.07980001391843,
      "sqlite_min": 45.41530000278726,
      "sqlite_max": 69.89009998505935,
      "speedup": 0.08456127502390194
    },
    {
      "target": "tslib",
      "depth": 5,
      "reached": 2438,
      "sqlite_reached": 2438,
      "agree": true,
      "hydra_ms": 623.3366999658756,
      "hydra_min": 591.2938000401482,
      "hydra_max": 668.0270999786444,
      "sqlite_ms": 64.30399999953806,
      "sqlite_min": 63.333300000522286,
      "sqlite_max": 65.73640002170578,
      "speedup": 0.10316094015170031
    },
    {
      "target": "chalk",
      "depth": 1,
      "reached": 827,
      "sqlite_reached": 827,
      "agree": true,
      "hydra_ms": 8.232499996665865,
      "hydra_min": 6.375099997967482,
      "hydra_max": 14.82639997266233,
      "sqlite_ms": 4.860999993979931,
      "sqlite_min": 4.632099997252226,
      "sqlite_max": 5.150799988768995,
      "speedup": 0.5904646214331758
    },
    {
      "target": "chalk",
      "depth": 2,
      "reached": 1505,
      "sqlite_reached": 1505,
      "agree": true,
      "hydra_ms": 130.93589997151867,
      "hydra_min": 112.65309998998418,
      "hydra_max": 137.21000001532957,
      "sqlite_ms": 17.157799971755594,
      "sqlite_min": 15.831400000024587,
      "sqlite_max": 19.939999969210476,
      "speedup": 0.13103969175365793
    },
    {
      "target": "chalk",
      "depth": 3,
      "reached": 1765,
      "sqlite_reached": 1765,
      "agree": true,
      "hydra_ms": 242.8889000439085,
      "hydra_min": 223.17039995687082,
      "hydra_max": 293.82830002577975,
      "sqlite_ms": 27.52800000598654,
      "sqlite_min": 26.351400010753423,
      "sqlite_max": 28.920200013089925,
      "speedup": 0.11333576792109536
    },
    {
      "target": "chalk",
      "depth": 4,
      "reached": 1871,
      "sqlite_reached": 1871,
      "agree": true,
      "hydra_ms": 336.2999999662861,
      "hydra_min": 316.28060003276914,
      "hydra_max": 380.5813000071794,
      "sqlite_ms": 49.70390000380576,
      "sqlite_min": 41.65570001350716,
      "sqlite_max": 60.13960001291707,
      "speedup": 0.1477963128420712
    },
    {
      "target": "chalk",
      "depth": 5,
      "reached": 1956,
      "sqlite_reached": 1956,
      "agree": true,
      "hydra_ms": 393.27300002332777,
      "hydra_min": 378.9250999689102,
      "hydra_max": 421.2093999958597,
      "sqlite_ms": 39.74610002478585,
      "sqlite_min": 36.76409996114671,
      "sqlite_max": 45.460599998477846,
      "speedup": 0.10106490916596926
    },
    {
      "target": "lodash",
      "depth": 1,
      "reached": 783,
      "sqlite_reached": 783,
      "agree": true,
      "hydra_ms": 18.748700036667287,
      "hydra_min": 14.078299980610609,
      "hydra_max": 22.644200013019145,
      "sqlite_ms": 3.929500002413988,
      "sqlite_min": 3.8446999969892204,
      "sqlite_max": 4.577299987431616,
      "speedup": 0.20958786447748215
    },
    {
      "target": "lodash",
      "depth": 2,
      "reached": 1441,
      "sqlite_reached": 1441,
      "agree": true,
      "hydra_ms": 112.54329996882007,
      "hydra_min": 96.77619999274611,
      "hydra_max": 153.46130001125857,
      "sqlite_ms": 15.172599989455193,
      "sqlite_min": 12.799100019037724,
      "sqlite_max": 19.88159999018535,
      "speedup": 0.13481566644712512
    },
    {
      "target": "lodash",
      "depth": 3,
      "reached": 1611,
      "sqlite_reached": 1611,
      "agree": true,
      "hydra_ms": 226.04310000315309,
      "hydra_min": 214.68120004283264,
      "hydra_max": 262.11230002809316,
      "sqlite_ms": 21.761499985586852,
      "sqlite_min": 20.786300010513514,
      "sqlite_max": 24.873999995179474,
      "speedup": 0.09627146320893361
    },
    {
      "target": "lodash",
      "depth": 4,
      "reached": 1637,
      "sqlite_reached": 1637,
      "agree": true,
      "hydra_ms": 347.76610002154484,
      "hydra_min": 321.5591000043787,
      "hydra_max": 370.8228999748826,
      "sqlite_ms": 35.225800005719066,
      "sqlite_min": 29.51790002407506,
      "sqlite_max": 52.95590002788231,
      "speedup": 0.10129164402032502
    },
    {
      "target": "lodash",
      "depth": 5,
      "reached": 1643,
      "sqlite_reached": 1643,
      "agree": true,
      "hydra_ms": 399.3060000357218,
      "hydra_min": 363.9640000183135,
      "hydra_max": 446.4939999743365,
      "sqlite_ms": 44.8649000027217,
      "sqlite_min": 41.560999990906566,
      "sqlite_max": 64.79540001600981,
      "speedup": 0.11235718972093606
    }
  ],
  "endpoint": [
    {
      "target": "tslib",
      "depth": 5,
      "total": 2438,
      "ms": 876.3413999695331,
      "min": 777.6388999773189,
      "max": 883.2931999932043
    },
    {
      "target": "chalk",
      "depth": 5,
      "total": 1956,
      "ms": 536.1123000038788,
      "min": 514.1388000338338,
      "max": 592.8070000372827
    },
    {
      "target": "lodash",
      "depth": 5,
      "total": 1643,
      "ms": 495.6176000414416,
      "min": 491.93639995064586,
      "max": 519.0688000293449
    }
  ]
}
```
