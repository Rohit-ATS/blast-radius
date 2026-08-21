"""Move the predicate store from SQLite into Postgres, without losing the crawl.

    py migrate_sidecar.py --dry-run       # what would move, and how long
    py migrate_sidecar.py                 # do it
    py migrate_sidecar.py --verify        # compare both stores, row for row

The crawl is the expensive thing here — tens of thousands of registry fetches
over hours. Losing it means starting from zero, so this is written to be run
twice safely: every table merges on its primary key, so an interrupted run is
resumed by running it again rather than by cleaning up first.

Transfer is by COPY into an unlogged staging table, then one INSERT ... SELECT
with ON CONFLICT. Row-by-row INSERTs over the internet are one round trip each;
at 20ms that is over five hours for the 906,000 rows this has to move, which is
not a slow migration, it is a failed one.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time

import config          # noqa: F401 — loads .env
import sidecar

# Order matters only for readability; there are no foreign keys between these.
# The counts are from the SQLite side and are printed so the operator can see
# the shape of what is moving before it starts.
TABLES = [
    ("packages", ["nid", "name", "latest", "versions", "crawled",
                  "ecosystem", "published_at", "fetched_at"],
     "(nid) DO NOTHING"),
    ("deps", ["src", "dst", "kind", "range"], "(src, dst, kind) DO NOTHING"),
    ("release_deps", ["name", "version", "dep", "range", "kind"],
     "(name, version, dep, kind) DO NOTHING"),
    ("maintainers", ["maintainer", "package"], "(maintainer, package) DO NOTHING"),
    ("collisions", ["nid", "name_a", "name_b"], None),      # no key; append only
    ("meta", ["key", "value"], "(key) DO UPDATE SET value = EXCLUDED.value"),
]

BATCH = 20_000


def sqlite_conn(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30)
    conn.execute("PRAGMA query_only=ON")
    return conn


def count(conn, table: str) -> int:
    try:
        return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    except Exception:
        return 0


def existing_columns(conn, table: str) -> set[str]:
    """The SQLite schema has grown over time; a column named in TABLES may not
    exist in an older deps.db. Migrate what is there rather than failing."""
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    except Exception:
        return set()


def migrate_table(src: sqlite3.Connection, dst, table: str,
                  columns: list[str], on_conflict: str | None,
                  dry_run: bool = False) -> tuple[int, float]:
    have = existing_columns(src, table)
    if not have:
        print(f"  {table:14} not present in the source; skipped")
        return 0, 0.0

    cols = [c for c in columns if c in have]
    missing = [c for c in columns if c not in have]
    if missing:
        print(f"  {table:14} note: source has no {missing}; those default")

    total = count(src, table)
    if dry_run or total == 0:
        print(f"  {table:14} {total:>9,} rows")
        return total, 0.0

    started = time.perf_counter()
    moved = 0
    cur = src.execute(f"SELECT {','.join(cols)} FROM {table}")

    while True:
        rows = cur.fetchmany(BATCH)
        if not rows:
            break
        sidecar.copy_rows(dst, table, cols, rows,
                          on_conflict=on_conflict or "DO NOTHING")
        dst.commit()
        moved += len(rows)
        pct = moved / total * 100 if total else 100
        print(f"\r  {table:14} {moved:>9,} / {total:,}  ({pct:5.1f}%)",
              end="", flush=True)

    took = time.perf_counter() - started
    rate = moved / took if took else 0
    print(f"\r  {table:14} {moved:>9,} rows in {took:6.1f}s  "
          f"({rate:,.0f} rows/s)      ")
    return moved, took


def verify(src: sqlite3.Connection, dst, moved: dict[str, int] | None = None) -> bool:
    """Row counts on both sides.

    Not a checksum — the point is to catch a table that silently moved nothing,
    which is the failure that looks like success.

    The comparison is against what was *read*, not against the source's count
    now. The crawler keeps writing while this runs, so the source legitimately
    grows mid-migration; an earlier version reported MISMATCH for the handful of
    rows added while it was working, which is a false alarm that would stop a
    correct migration. Rows written after the read are reported as drift and are
    picked up by running this again — every table merges on its key.
    """
    print("\nverifying:")
    ok = True
    for table, _cols, _c in TABLES:
        now = count(src, table)
        there = count(dst, table)
        read = (moved or {}).get(table)

        bad = there < (read if read is not None else now)
        ok = ok and not bad
        mark = "SHORT" if bad else "ok "
        drift = (f"  (+{now - read:,} written since)"
                 if read is not None and now > read else "")
        print(f"  {mark} {table:14} source {now:>9,}   target {there:>9,}{drift}")

    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sqlite", default=config.get("DEPS_DB") or "deps.db")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would move and stop")
    ap.add_argument("--verify", action="store_true",
                    help="compare row counts and stop")
    args = ap.parse_args()

    if not sidecar.IS_POSTGRES:
        print("DATABASE_URL is not set, so there is nothing to migrate into.\n"
              "\n"
              "Set it to the Supabase *pooler* connection string:\n"
              "  Dashboard -> Settings -> Database -> Connection string -> "
              "Transaction pooler\n"
              "  postgresql://postgres.<ref>:<password>"
              "@aws-0-<region>.pooler.supabase.com:6543/postgres",
              file=sys.stderr)
        return 2

    print(f"source: {args.sqlite}")
    print(f"target: {sidecar.describe()}\n")

    src = sqlite_conn(args.sqlite)
    sidecar.ensure_schema()
    dst = sidecar.connect()

    try:
        if args.verify:
            return 0 if verify(src, dst) else 1

        if args.dry_run:
            print("would move:")
            grand = sum(migrate_table(src, dst, t, c, k, dry_run=True)[0]
                        for t, c, k in TABLES)
            print(f"\n  {grand:,} rows total")
            return 0

        print("migrating:")
        started = time.perf_counter()
        grand = 0
        moved_by_table: dict[str, int] = {}
        for table, cols, conflict in TABLES:
            moved, _ = migrate_table(src, dst, table, cols, conflict)
            moved_by_table[table] = moved
            grand += moved

        # ANALYZE after a bulk load, or the planner keeps using the statistics
        # it had when every table was empty and picks sequential scans over the
        # indexes this migration just created.
        print("\n  analyzing...")
        for table, _c, _k in TABLES:
            dst.execute(f"ANALYZE {table}")
        dst.commit()

        took = time.perf_counter() - started
        print(f"\n{grand:,} rows in {took:.1f}s")

        if not verify(src, dst, moved_by_table):
            print("\nthe target has fewer rows than were read — investigate "
                  "before switching over.")
            return 1
        print("\nDone. Set DATABASE_URL on the web and worker services to switch.")
        return 0
    finally:
        src.close()
        dst.close()
        sidecar.close_pool()


if __name__ == "__main__":
    raise SystemExit(main())
