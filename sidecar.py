"""The predicate store: SQLite on a laptop, Postgres in production.

Topology lives in HydraDB and is not touched by anything here. What lives in
*this* store is everything a traversal cannot answer — the declared semver
range on each edge, which maintainer publishes which package, crawl bookkeeping.
The split is why "would have pulled it" versus "shielded by a pin" is a number
this project can produce at all, and it is unchanged by moving stores.

Why this module exists
----------------------
On Render a disk attaches to exactly one service. The worker writes this store
and the web service reads it on fourteen endpoints, and SQLite has no network
protocol — so the three-service split cannot share a file. Postgres can be
shared, so in production it is.

Rather than rewrite the sixteen `db.execute(...)` call sites in blast.py, this
presents the sqlite3 Connection API over a pooled Postgres connection. blast.py
keeps its SQL literal and reviewable, the dialect differences live in one place,
and the change is reversible by unsetting one environment variable.

    DATABASE_URL unset  ->  sqlite3, exactly as before
    DATABASE_URL set    ->  pooled Postgres, same call sites

Pooling is not optional over the internet. A new connection to Supabase costs a
TCP handshake, a TLS handshake and an auth round trip — comfortably 100ms before
a single row moves. Fourteen endpoints doing that per request would make the
console feel broken.
"""

from __future__ import annotations

import atexit
import os
import re
import sqlite3
import threading
from typing import Any, Iterable, Sequence

import config

DATABASE_URL = config.get("DATABASE_URL")
IS_POSTGRES = bool(DATABASE_URL)

# Supabase gives two endpoints. The direct one (`db.<ref>.supabase.co:5432`)
# holds a real backend per connection and is the wrong thing to point a web
# service at; several of them will exhaust the project's connection limit. The
# pooler (`aws-0-<region>.pooler.supabase.com:6543`, transaction mode) multiplexes
# many clients onto few backends, which is what a request-per-connection
# workload needs. Warn rather than refuse: someone may have a reason.
POOLER_HINT = ".pooler.supabase.com"

_pool = None
_pool_lock = threading.Lock()

# psycopg uses %s; every query in this project is written with sqlite3's ?.
# Translating here keeps the SQL in blast.py in one dialect rather than two.
_PLACEHOLDER = re.compile(r"\?(?=(?:[^']*'[^']*')*[^']*$)")


# The dialect differences that actually appear in this project's SQL, found by
# grepping every module that touches the sidecar rather than by guessing:
#
#   group_concat(x)      -> string_agg(x, ',')     aggregate has a different name
#   INSERT OR IGNORE     -> INSERT ... ON CONFLICT DO NOTHING
#   INSERT OR REPLACE    -> INSERT ... ON CONFLICT DO UPDATE
#
# The first is handled here because it is a pure rename. The other two are not:
# ON CONFLICT needs to name the conflicting columns, which this function cannot
# know, so writes go through sidecar.copy_rows() and upsert_meta() instead and
# an untranslated `INSERT OR` is raised as an error rather than silently sent to
# a server that will reject it with something less obvious.
_GROUP_CONCAT = re.compile(r"\bgroup_concat\s*\(\s*(DISTINCT\s+)?([^)]+?)\s*\)",
                           re.IGNORECASE)
_INSERT_OR = re.compile(r"\bINSERT\s+OR\s+(IGNORE|REPLACE)\b", re.IGNORECASE)


def _translate(sql: str) -> str:
    """SQLite's dialect -> Postgres', for the constructs this project uses.

    `?` -> `%s`, skipping anything inside a quoted string literal: the lookahead
    counts quotes to the end of the statement, and an odd number means the `?`
    sits inside a literal where it is data.
    """
    if _INSERT_OR.search(sql):
        raise ValueError(
            "INSERT OR IGNORE/REPLACE has no direct Postgres equivalent — "
            "ON CONFLICT must name the conflicting columns. Use "
            "sidecar.copy_rows() or sidecar.upsert_meta() for writes.\n"
            f"  {sql.strip()[:120]}")

    sql = _GROUP_CONCAT.sub(
        lambda m: f"string_agg({m.group(1) or ''}{m.group(2)}, ',')", sql)
    return _PLACEHOLDER.sub("%s", sql)


# --------------------------------------------------------------------------
# the adapter
# --------------------------------------------------------------------------

class Row(dict):
    """A row that answers to a column name or to a position, as sqlite3.Row does.

    Call sites written against SQLite use both — `row["email"]` reads better,
    `row[0]` is shorter for a single-column count — and code shared between the
    two backends must not have to care which it is talking to.
    """

    __slots__ = ()

    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return dict.__getitem__(self, key)

    def keys(self):                      # sqlite3.Row exposes this
        return list(dict.keys(self))


class _Cursor:
    """Just enough of sqlite3.Cursor for the call sites that exist."""

    def __init__(self, cur):
        self._cur = cur

    def fetchall(self):
        return self._cur.fetchall()

    def fetchone(self):
        return self._cur.fetchone()

    def __iter__(self):
        return iter(self._cur)

    @property
    def rowcount(self):
        return self._cur.rowcount

    @property
    def lastrowid(self):
        return None


class PgConnection:
    """sqlite3.Connection's surface, backed by a pooled Postgres connection.

    Deliberately narrow: `execute`, `executemany`, `commit`, `close`, and the
    context-manager protocol. If a call site needs something else, it should be
    added here consciously rather than by making this a general proxy — the
    narrowness is what makes the two backends stay equivalent.
    """

    def __init__(self, conn, putback):
        self._conn = conn
        self._putback = putback
        self.row_factory = None

    def _cursor(self):
        # row_factory is set by the caller exactly as it would be on sqlite3.
        # When it is, rows come back keyed by column name instead of position.
        if self.row_factory is not None:
            from psycopg.rows import class_row
            return self._conn.cursor(row_factory=class_row(Row))
        return self._conn.cursor()

    def execute(self, sql: str, params: Sequence[Any] = ()):
        cur = self._cursor()
        cur.execute(_translate(sql), tuple(params))
        return _Cursor(cur)

    def executemany(self, sql: str, rows: Iterable[Sequence[Any]]):
        cur = self._cursor()
        cur.executemany(_translate(sql), [tuple(r) for r in rows])
        return _Cursor(cur)

    def executescript(self, sql: str):
        with self._conn.cursor() as cur:
            cur.execute(sql)
        self._conn.commit()

    def putback_clean(self):
        """Return the connection to the pool with no transaction open.

        psycopg leaves a connection INTRANS after any statement, including a
        read. Handing that back to the pool makes it log a rollback on every
        checkout — noisy, and it holds a snapshot open on the server for as
        long as the connection sits idle."""
        try:
            self._conn.rollback()
        except Exception:
            pass
        self.close()

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        if self._conn is not None:
            try:
                # Never return a connection mid-transaction; see putback_clean.
                if getattr(self._conn, "info", None) and self._conn.info.transaction_status:
                    self._conn.rollback()
            except Exception:
                pass
            self._putback(self._conn)
            self._conn = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        # sqlite3 commits on a clean exit and rolls back on an exception; the
        # call sites were written against that behaviour, so it is preserved.
        try:
            if exc_type is None:
                self._conn.commit()
            else:
                self._conn.rollback()
        finally:
            self.close()
        return False


def pool():
    global _pool
    if _pool is not None:
        return _pool
    with _pool_lock:
        if _pool is not None:
            return _pool
        from psycopg_pool import ConnectionPool

        if POOLER_HINT not in DATABASE_URL and "supabase" in DATABASE_URL:
            import warnings
            warnings.warn(
                "DATABASE_URL points at Supabase's direct endpoint rather than "
                "the pooler. Use aws-0-<region>.pooler.supabase.com:6543 or the "
                "project's connection limit will be exhausted by a few workers.",
                RuntimeWarning, stacklevel=2)

        _pool = ConnectionPool(
            DATABASE_URL,
            min_size=int(config.number("PG_POOL_MIN", 1)),
            max_size=int(config.number("PG_POOL_MAX", 8)),
            timeout=float(config.number("PG_POOL_TIMEOUT", 15)),
            max_idle=300.0,
            kwargs={"autocommit": False,
                    "application_name": "blast-radius",
                    # psycopg3 promotes a statement to a server-side PREPARE
                    # after it has been seen a few times. Supabase's pooler runs
                    # in transaction mode, where consecutive statements can land
                    # on different backends, so the prepare and its reuse do not
                    # share a session:
                    #     DuplicatePreparedStatement: "_pg3_0" already exists
                    # It appears only after a statement crosses the threshold,
                    # which is why a first request succeeds and a later
                    # identical one fails. Disabled outright — the pooler is the
                    # supported way to reach Supabase from several workers, and
                    # the plans this saves are for queries already indexed.
                    "prepare_threshold": None},
            open=True,
        )
        # Without this the pool's finaliser tries to join its worker threads
        # during interpreter shutdown and raises PythonFinalizationError over
        # the top of whatever the process was actually doing.
        atexit.register(close_pool)
        return _pool


def close_pool() -> None:
    global _pool
    with _pool_lock:
        if _pool is not None:
            try:
                _pool.close()
            except Exception:
                pass
            _pool = None


def connect(path: str | None = None, read_only: bool = False):
    """A connection to the predicate store, whichever it is."""
    if IS_POSTGRES:
        p = pool()
        conn = p.getconn()
        return PgConnection(conn, p.putconn)

    conn = sqlite3.connect(path or os.environ.get("DEPS_DB", "deps.db"),
                           timeout=20, check_same_thread=False)
    if read_only:
        conn.execute("PRAGMA query_only=ON")
    return conn


def describe() -> dict:
    """For /api/health. Never contains the password."""
    if not IS_POSTGRES:
        return {"backend": "sqlite", "shared": False}
    host = ""
    m = re.search(r"@([^/:?]+)", DATABASE_URL)
    if m:
        host = m.group(1)
    out = {"backend": "postgres", "shared": True, "host": host,
           "pooled": POOLER_HINT in DATABASE_URL}
    if _pool is not None:
        s = _pool.get_stats()
        out["pool"] = {k: s.get(k) for k in
                       ("pool_size", "pool_available", "requests_waiting")}
    return out


# --------------------------------------------------------------------------
# schema
# --------------------------------------------------------------------------
# The SQLite schema translated, with the indexes made explicit. SQLite gets an
# index for free on an INTEGER PRIMARY KEY and on UNIQUE columns; Postgres does
# too, but the *composite* and *reverse-direction* indexes that make the
# traversal-adjacent lookups fast have to be declared either way — and the
# reverse ones are exactly what this store is asked for.

SCHEMA = """
CREATE TABLE IF NOT EXISTS packages (
  nid          BIGINT PRIMARY KEY,
  name         TEXT UNIQUE NOT NULL,
  latest       TEXT,
  versions_seen INTEGER,
  crawled      SMALLINT NOT NULL DEFAULT 0,
  ecosystem    TEXT NOT NULL DEFAULT 'npm',
  published_at TEXT,
  fetched_at   DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS packages_crawled ON packages(crawled);
CREATE INDEX IF NOT EXISTS packages_ecosystem ON packages(ecosystem);

CREATE TABLE IF NOT EXISTS deps (
  src   TEXT NOT NULL,
  dst   TEXT NOT NULL,
  kind  TEXT NOT NULL DEFAULT 'prod',
  range TEXT,
  PRIMARY KEY (src, dst, kind)
);
-- "who depends on X" is the question this store is asked most, and it reads
-- the edge backwards. Without this it is a sequential scan of 155k rows per
-- lookup; with it, an index seek.
CREATE INDEX IF NOT EXISTS deps_dst ON deps(dst);
CREATE INDEX IF NOT EXISTS deps_dst_kind ON deps(dst, kind);

CREATE TABLE IF NOT EXISTS release_deps (
  name    TEXT NOT NULL,
  version TEXT NOT NULL,
  dep     TEXT NOT NULL,
  range   TEXT,
  kind    TEXT NOT NULL DEFAULT 'prod',
  PRIMARY KEY (name, version, dep, kind)
);
-- The semver question: every release that declares a range on this package.
CREATE INDEX IF NOT EXISTS release_deps_dep ON release_deps(dep);
CREATE INDEX IF NOT EXISTS release_deps_dep_kind ON release_deps(dep, kind);

CREATE TABLE IF NOT EXISTS maintainers (
  maintainer TEXT NOT NULL,
  package    TEXT NOT NULL,
  PRIMARY KEY (maintainer, package)
);
-- Both directions are asked for: "what else does this person publish" and
-- "who publishes this package". The primary key covers the first; this covers
-- the second.
CREATE INDEX IF NOT EXISTS maintainers_pkg ON maintainers(package);
CREATE INDEX IF NOT EXISTS maintainers_name ON maintainers(maintainer);

CREATE TABLE IF NOT EXISTS collisions (
  nid    BIGINT,
  name_a TEXT,
  name_b TEXT
);

CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT
);
"""


# Changes to tables that already exist. CREATE TABLE IF NOT EXISTS does nothing
# to a table that is already there, so a column added after the first deploy
# needs this to reach an existing database.
#
# Every statement must be safe to run on every boot.
MIGRATIONS = """
-- The column was created as `versions` here while the canonical schema in
-- ingest.py has always called it `versions_seen`. Nothing ever read `versions`,
-- but both the crawler and the live ingester write `versions_seen`, so on
-- Postgres every package write failed with
--     UndefinedColumn: column "versions_seen" of relation "packages"
-- and, because a failed statement poisons a Postgres transaction, took the rest
-- of the batch with it. Renamed rather than duplicated: two columns holding the
-- same count is how they drift.
--
-- Guarded because Postgres has no IF EXISTS for RENAME COLUMN. Unguarded, this
-- succeeds once and then fails on every boot afterwards with "column versions
-- does not exist" — aborting the transaction it runs in and taking the whole
-- schema setup down with it. A migration that only works once is a landmine.
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.columns
             WHERE table_name = 'packages' AND column_name = 'versions') THEN
    ALTER TABLE packages RENAME COLUMN versions TO versions_seen;
  END IF;
END
$$;

-- `via` records which manifest field declared the dependency. The crawler and
-- the live ingester both write it and the migration to Postgres dropped it, so
-- every edge insert failed the same way the package insert did. ADD COLUMN does
-- support IF NOT EXISTS, so this one needs no guard.
ALTER TABLE deps ADD COLUMN IF NOT EXISTS via TEXT;
"""


def ensure_schema(conn=None) -> None:
    own = conn is None
    conn = conn or connect()
    try:
        if IS_POSTGRES:
            conn.executescript(SCHEMA)
            conn.executescript(MIGRATIONS)
        else:
            from ingest import SIDECAR_SCHEMA
            conn.executescript(SIDECAR_SCHEMA)
            conn.commit()
    finally:
        if own:
            conn.close()


# --------------------------------------------------------------------------
# batched writes
# --------------------------------------------------------------------------

def copy_rows(conn, table: str, columns: Sequence[str],
              rows: Iterable[Sequence[Any]], on_conflict: str = "DO NOTHING") -> int:
    """Bulk-insert with COPY where possible.

    The crawler flushes thousands of dep rows at a time. Row-by-row INSERTs
    over the internet are one round trip each — at 20ms that is a minute per
    3,000 rows, which is not a slow crawl, it is a stopped one.

    COPY cannot express ON CONFLICT, and these tables have real primary keys
    that a re-crawl will collide with. So: COPY into an UNLOGGED temporary
    table, then a single INSERT ... SELECT ... ON CONFLICT from it. One round
    trip for the data, one for the merge.
    """
    rows = list(rows)
    if not rows:
        return 0

    if not IS_POSTGRES:
        placeholders = ",".join("?" * len(columns))
        conn.executemany(
            f"INSERT OR IGNORE INTO {table} ({','.join(columns)}) "
            f"VALUES ({placeholders})", rows)
        return len(rows)

    cols = ",".join(columns)
    staging = f"_stage_{table}"
    raw = conn._conn
    with raw.cursor() as cur:
        cur.execute(f"CREATE TEMP TABLE IF NOT EXISTS {staging} "
                    f"(LIKE {table} INCLUDING DEFAULTS) ON COMMIT DROP")
        cur.execute(f"TRUNCATE {staging}")
        with cur.copy(f"COPY {staging} ({cols}) FROM STDIN") as copy:
            for row in rows:
                copy.write_row(row)
        cur.execute(
            f"INSERT INTO {table} ({cols}) SELECT {cols} FROM {staging} "
            f"ON CONFLICT {on_conflict}")
    return len(rows)


def upsert_meta(conn, key: str, value: str) -> None:
    if IS_POSTGRES:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?,?) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (key, value))
    else:
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)",
                     (key, value))
