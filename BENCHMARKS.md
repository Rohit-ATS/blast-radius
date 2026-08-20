# Benchmarks

The same question, asked of two engines over the same edges: **which packages transitively depend on X, within N hops?**

Numbers are medians of 5 runs, measured by `bench.py` on the graph as it stood at the time below. Reproduce with `py bench.py --runs 5`.

## What was measured

- **When**: 2026-08-20 03:43:50 Pacific Daylight Time
- **Commit**: `ea0f4c5`
- **Machine**: Windows 11, Python 3.14.2
- **HydraDB**: 0.1.0, single node in Docker, HTTP query API on `127.0.0.1:8443`
- **Graph**: 27,076 vertices, 91,544 `REQUIRED_BY` edges
- **Sidecar**: 27,076 package rows, 91,544 prod dependency rows in SQLite (WAL, index on `deps(dst)`)

Both stores hold the same edge set. The crawler writes a graph edge and a sidecar row from the same batch, and only `dependencies` (not `peerDependencies`) become edges in either.

## Headline: at this graph size, SQLite wins

The recursive CTE beat HydraDB on **every row measured**, by between 3× and 13×. That is the opposite of the result this project was built expecting, so it leads the report rather than hiding under it.

| package | depth | packages reached | HydraDB | SQLite CTE | faster | agree |
|---|---|---|---|---|---|---|
| `tslib` | 1 | 892 | 19 ms | 4 ms | SQLite 5.3× | yes |
| `tslib` | 2 | 1,613 | 141 ms | 20 ms | SQLite 7.0× | yes |
| `tslib` | 3 | 2,228 | 311 ms | 47 ms | SQLite 6.6× | yes |
| `tslib` | 4 | 2,389 | 502 ms | 53 ms | SQLite 9.5× | yes |
| `tslib` | 5 | 2,438 | 694 ms | 70 ms | SQLite 9.8× | yes |
| `chalk` | 1 | 827 | 20 ms | 5 ms | SQLite 4.0× | yes |
| `chalk` | 2 | 1,505 | 123 ms | 18 ms | SQLite 7.0× | yes |
| `chalk` | 3 | 1,765 | 260 ms | 30 ms | SQLite 8.7× | yes |
| `chalk` | 4 | 1,871 | 361 ms | 49 ms | SQLite 7.3× | yes |
| `chalk` | 5 | 1,956 | 432 ms | 46 ms | SQLite 9.5× | yes |
| `lodash` | 1 | 783 | 20 ms | 6 ms | SQLite 3.2× | yes |
| `lodash` | 2 | 1,441 | 132 ms | 23 ms | SQLite 5.7× | yes |
| `lodash` | 3 | 1,611 | 241 ms | 23 ms | SQLite 10.6× | yes |
| `lodash` | 4 | 1,637 | 334 ms | 29 ms | SQLite 11.4× | yes |
| `lodash` | 5 | 1,643 | 406 ms | 32 ms | SQLite 12.8× | yes |

`agree` is the column that makes the rest of the table mean anything: both engines return the identical count on every row, so this is two answers to one question rather than two different questions. That agreement is also the strongest correctness check the project has — the reversed-edge graph model and a plain recursive join over the same data reach the same closure.

Transport is not the explanation. The cheapest possible HydraDB round trip — a single-vertex lookup by id — measured **2.5 ms**, so subtracting HTTP and JSON from the traversal numbers leaves the ranking unchanged.

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
| `tslib` | 5 | 2,438 | **853 ms** |
| `chalk` | 5 | 1,956 | **554 ms** |
| `lodash` | 5 | 1,643 | **540 ms** |

## Counting the whole graph

Worth recording because it is the one thing HydraDB 0.1.0 does badly, and it shaped the architecture:

| operation | engine | time |
|---|---|---|
| `MATCH (p:Package) RETURN count(*)` + edge count | HydraDB | **10,706 ms** |
| same two counts | SQLite sidecar | 30.4 ms |

There is no `CREATE INDEX` in HydraDB 0.1.0, so a whole-graph count is a full scan — the server says so itself in its logs (`query plan warrants attention … access_path AllVertexScan`). This is why the live header polls the sidecar and a background thread re-measures the graph on a slow timer: the number is real, but it cannot sit on a request path.

Note the shape of the result: HydraDB is dramatically slower at counting *everything*, and much closer to competitive when traversing from a *known vertex* — three orders of magnitude apart as access patterns go. This project only ever needs the second one, which is why the architecture keeps whole-graph counts off the request path.

## Raw data

```json
{
  "graph": {
    "packages": 27076,
    "edges": 91544,
    "measured_ms": 10706.4
  },
  "sidecar_packages": 27076,
  "sidecar_edges": 91544,
  "graph_scan_ms": 10706.4,
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
      "hydra_ms": 19.41360003547743,
      "hydra_min": 18.59990000957623,
      "hydra_max": 22.126900032162666,
      "sqlite_ms": 3.6974999820813537,
      "sqlite_min": 3.5182000137865543,
      "sqlite_max": 9.80820000404492,
      "speedup": 0.19045926439837788
    },
    {
      "target": "tslib",
      "depth": 2,
      "reached": 1613,
      "sqlite_reached": 1613,
      "agree": true,
      "hydra_ms": 140.84549999097362,
      "hydra_min": 133.3161999937147,
      "hydra_max": 189.74770000204444,
      "sqlite_ms": 20.06690000416711,
      "sqlite_min": 17.215599946212023,
      "sqlite_max": 30.864199972711504,
      "speedup": 0.14247455549132304
    },
    {
      "target": "tslib",
      "depth": 3,
      "reached": 2228,
      "sqlite_reached": 2228,
      "agree": true,
      "hydra_ms": 310.55860000196844,
      "hydra_min": 298.5206000157632,
      "hydra_max": 362.64040000969544,
      "sqlite_ms": 46.76280001876876,
      "sqlite_min": 41.25419998308644,
      "sqlite_max": 60.12459995690733,
      "speedup": 0.15057641301342922
    },
    {
      "target": "tslib",
      "depth": 4,
      "reached": 2389,
      "sqlite_reached": 2389,
      "agree": true,
      "hydra_ms": 502.2190000163391,
      "hydra_min": 479.4760999502614,
      "hydra_max": 534.5239999587648,
      "sqlite_ms": 52.714899997226894,
      "sqlite_min": 51.16689996793866,
      "sqlite_max": 54.51019998872653,
      "speedup": 0.10496396989264022
    },
    {
      "target": "tslib",
      "depth": 5,
      "reached": 2438,
      "sqlite_reached": 2438,
      "agree": true,
      "hydra_ms": 693.8907000003383,
      "hydra_min": 668.9116000197828,
      "hydra_max": 712.7755000256002,
      "sqlite_ms": 70.45270001981407,
      "sqlite_min": 68.42219998361543,
      "sqlite_max": 102.71840001223609,
      "speedup": 0.10153284951041962
    },
    {
      "target": "chalk",
      "depth": 1,
      "reached": 827,
      "sqlite_reached": 827,
      "agree": true,
      "hydra_ms": 19.85370001057163,
      "hydra_min": 7.052700035274029,
      "hydra_max": 24.124099989421666,
      "sqlite_ms": 4.996800038497895,
      "sqlite_min": 4.5405999990180135,
      "sqlite_max": 7.886400038842112,
      "speedup": 0.2516810486628294
    },
    {
      "target": "chalk",
      "depth": 2,
      "reached": 1505,
      "sqlite_reached": 1505,
      "agree": true,
      "hydra_ms": 123.04330000188202,
      "hydra_min": 115.02070003189147,
      "hydra_max": 132.34229997033253,
      "sqlite_ms": 17.6801000488922,
      "sqlite_min": 16.024100012145936,
      "sqlite_max": 27.57400000700727,
      "speedup": 0.143690067225292
    },
    {
      "target": "chalk",
      "depth": 3,
      "reached": 1765,
      "sqlite_reached": 1765,
      "agree": true,
      "hydra_ms": 260.19679999444634,
      "hydra_min": 245.83929998334497,
      "hydra_max": 296.4293000404723,
      "sqlite_ms": 29.980599996633828,
      "sqlite_min": 27.673000004142523,
      "sqlite_max": 35.179799946490675,
      "speedup": 0.1152227852044058
    },
    {
      "target": "chalk",
      "depth": 4,
      "reached": 1871,
      "sqlite_reached": 1871,
      "agree": true,
      "hydra_ms": 361.27219995250925,
      "hydra_min": 352.0118999877013,
      "hydra_max": 370.42759999167174,
      "sqlite_ms": 49.372400040738285,
      "sqlite_min": 37.04220004146919,
      "sqlite_max": 54.568200022913516,
      "speedup": 0.13666260522461593
    },
    {
      "target": "chalk",
      "depth": 5,
      "reached": 1956,
      "sqlite_reached": 1956,
      "agree": true,
      "hydra_ms": 431.6465999581851,
      "hydra_min": 426.40039999969304,
      "hydra_max": 465.1292000198737,
      "sqlite_ms": 45.59640004299581,
      "sqlite_min": 44.83869997784495,
      "sqlite_max": 62.648999970406294,
      "speedup": 0.10563363651517901
    },
    {
      "target": "lodash",
      "depth": 1,
      "reached": 783,
      "sqlite_reached": 783,
      "agree": true,
      "hydra_ms": 19.676099997013807,
      "hydra_min": 17.734499997459352,
      "hydra_max": 21.71010000165552,
      "sqlite_ms": 6.056599959265441,
      "sqlite_min": 4.285999981220812,
      "sqlite_max": 7.6425999868661165,
      "speedup": 0.30781506295376804
    },
    {
      "target": "lodash",
      "depth": 2,
      "reached": 1441,
      "sqlite_reached": 1441,
      "agree": true,
      "hydra_ms": 132.42579996585846,
      "hydra_min": 117.00379999820143,
      "hydra_max": 137.41640001535416,
      "sqlite_ms": 23.361100000329316,
      "sqlite_min": 20.369600038975477,
      "sqlite_max": 28.41119997901842,
      "speedup": 0.176408977754729
    },
    {
      "target": "lodash",
      "depth": 3,
      "reached": 1611,
      "sqlite_reached": 1611,
      "agree": true,
      "hydra_ms": 240.56230002315715,
      "hydra_min": 221.39019996393472,
      "hydra_max": 251.86229997780174,
      "sqlite_ms": 22.730500029865652,
      "sqlite_min": 22.502400039229542,
      "sqlite_max": 24.82249995227903,
      "speedup": 0.09448903684275364
    },
    {
      "target": "lodash",
      "depth": 4,
      "reached": 1637,
      "sqlite_reached": 1637,
      "agree": true,
      "hydra_ms": 334.19189997948706,
      "hydra_min": 328.4535999991931,
      "hydra_max": 346.30830003879964,
      "sqlite_ms": 29.367199982516468,
      "sqlite_min": 28.423500014469028,
      "sqlite_max": 39.66730000684038,
      "speedup": 0.08787525964668517
    },
    {
      "target": "lodash",
      "depth": 5,
      "reached": 1643,
      "sqlite_reached": 1643,
      "agree": true,
      "hydra_ms": 406.18450002511963,
      "hydra_min": 391.6274000075646,
      "hydra_max": 452.0542999962345,
      "sqlite_ms": 31.771200010553002,
      "sqlite_min": 30.330300040077418,
      "sqlite_max": 34.279200015589595,
      "speedup": 0.07821864204219556
    }
  ],
  "endpoint": [
    {
      "target": "tslib",
      "depth": 5,
      "total": 2438,
      "ms": 853.2471000216901,
      "min": 816.2538000033237,
      "max": 904.5830999966711
    },
    {
      "target": "chalk",
      "depth": 5,
      "total": 1956,
      "ms": 554.1301000048406,
      "min": 531.2455999664962,
      "max": 557.0402999874204
    },
    {
      "target": "lodash",
      "depth": 5,
      "total": 1643,
      "ms": 540.1422999566421,
      "min": 531.5632999991067,
      "max": 611.1485000001267
    }
  ]
}
```
