# BLAST RADIUS — 11-hour battle plan

**Track 2A · Supply chain blast radius.** Submission closes Aug 20, 11:59 PM PT.

The pitch, in one line: *when a package is compromised at 09:00, which of your
services are exposed by 09:06?* HydraDB answers it in one traversal. Nothing
else in the submission pool will answer it at all.

---

## The scope contract

Build these five, in this order. Each one is a demo beat. **Ship 3 working
features over 5 broken ones.**

| # | Feature | Why it scores |
|---|---------|---------------|
| 1 | Transitive reverse-dependency closure with depth histogram | The literal track prompt. Non-negotiable. |
| 2 | Lockfile drop — judge uploads their own `package-lock.json` | Makes it real in 10 seconds. Highest demo-value-per-hour on the list. |
| 3 | Would-resolve semver filter | Separates "lists the dep" from "would have pulled the bad version." Nobody else will do this. |
| 4 | Maintainer pivot — what else the attacker now controls | Pure graph. Impossible with a vector index. This is your *Best Use of HydraDB* argument. |
| 5 | Typosquat ring | Cheap, ~30 min, cut it without hesitation. |

**Cut list, in order, when you fall behind:** 5 → 4 → 3. Never cut 1 or 2.

---

## Timeline (T = hours remaining)

### T-11 → T-10 · Infra up, crawl started
Do these two things *in parallel*. The crawl runs all day in the background;
starting it late is the only unrecoverable mistake on this list.

```bash
git init blast-radius && cd blast-radius   # fresh repo — judges read commit history
mkdir -p .hydradb/store .hydradb/cache
printf '%s\n' 'local-development-token-32-bytes' > .hydradb/auth-token
docker compose up -d
python hydra.py            # must print: hydradb ready and round-tripping writes
python ingest.py --seeds seeds.txt --max-packages 40000 --max-versions 5
```

**Hard cutoff: 45 minutes on the server.** If the Docker image will not come
up, switch to the native `cargo run --features server-runtime --bin graph-node`
path from the HydraDB README. Set `RUST_MIN_STACK=33554432` or the node serves
`/readyz` and then dies on your first query — that failure looks like a
network bug and will eat an hour if you do not know about it.

The crawler checkpoints to `.crawl_state.json` every batch. Ctrl-C is safe;
rerunning resumes. Let it run in `tmux` and stop looking at it.

**Commit now.** First commit timestamped today, inside the window, clean.

### T-10 → T-8 · Query layer against a partial graph
The graph is filling as you work — you do not need to wait for it. Get
`blast.py` returning real rows for `debug`, `chalk`, `ms` (they land in the
first 500 packages). Verify the two-layer model works end to end, then leave
it alone.

Fix `hydra._rows()` first if the result shape does not match — print one raw
response and adjust. That is a five-minute fix that blocks everything.

### T-8 → T-5 · The console
One page. Search box → results. Feed the SPEC.md brief to Claude Code and let
it build; you review, you do not hand-write.

Non-negotiable UI elements:
- **The latency number, rendered large, next to every result.** `depth 5 ·
  1.4M edges · 210 ms`. Judges are scoring "quality of results" and
  "graph-native approaches" — put the evidence on screen, do not narrate it.
- Depth histogram (depth 1: 340 packages, depth 2: 2,100, …). Instantly legible.
- Lockfile drop zone with a real verdict: EXPOSED / SHIELDED BY PIN / CLEAR.

### T-5 → T-3 · Incident mode + the number that closes the pitch
Wire a real incident as a preset button so the demo needs zero typing. Then
run the depth-1-through-5 latency sweep and record actual numbers into
`BENCHMARKS.md`. If you have 20 minutes spare, run the same closure as a
recursive CTE in Postgres or SQLite and put both numbers in one table. A
side-by-side latency comparison is the single most persuasive artifact you can
produce for this track.

**T-3 is a hard freeze.** Whatever is broken at T-3 gets deleted, not fixed.

### T-3 → T-1.5 · README + video
Both matter more than another feature. Missing license = disqualification.
Missing video = disqualification.

- `LICENSE` — MIT for your code. HydraDB is AGPL-3.0 but you run it as a
  separate server over Bolt/HTTP and do not modify or link against it, so your
  repo licenses independently. Say this explicitly in the README.
- README sections: what it does / the graph model and *why two layers* /
  exact setup commands / **how HydraDB is used and what breaks without it** /
  attribution for npm registry data.
- Record the video. Script is in SPEC.md. **Under 3:00 — past that may not be
  reviewed.**

### T-1.5 → T-0 · Submit
Open your own repo in a logged-out incognito window. Open your own video link
the same way. Broken links are the most common way people lose. Submit the
form. Then stop.

---

## Where this actually gets won

Judges score five things: technical execution, use of HydraDB and graph-native
approaches, product completeness, quality of results, originality.

Track 3 will be the crowded one — "make your own mem0" is the sexy prompt and
half the field will pick it, then run out of budget mid-benchmark. Track 1 is
half a million documents; nobody solves entity resolution from scratch in a
day. Track 2A is the one where a solo builder with eleven hours can produce
something *complete* rather than something partial.

And it is the only track where the graph argument is self-evident. A
transitive reverse-dependency closure over tens of millions of versioned nodes
is not a thing a vector index can approximate badly — it is a thing a vector
index cannot do. Say that sentence in the video.

## The three ways this dies

1. **Crawl started late.** Start it in hour one, before anything else works.
2. **Edge blowup.** `--max-versions 5` exists for a reason. All versions of
   40k packages is ~20M+ edges and your ingestion never finishes. Do not
   raise it except for the one package in your incident story.
3. **Polishing past T-3.** A feature that lands at T-1 is a feature that
   breaks on camera.
