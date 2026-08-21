"""The predicate store, on either backend.

The dialect translation is the risky part of this change: it sits between every
query and the database, and a mistake there is silent — a query that returns the
wrong rows rather than an error. These tests pin the translations that exist and
fail on a construct that has none, so the next SQLite-ism someone writes is
caught here rather than in production against Postgres.

The Postgres tests need a live database and skip without one:

    docker run -d -p 15432:5432 -e POSTGRES_PASSWORD=probe -e POSTGRES_DB=blast postgres:16-alpine
    DATABASE_URL=postgresql://postgres:probe@127.0.0.1:15432/blast py -m pytest tests/test_sidecar.py
"""

from __future__ import annotations

import importlib
import os
import sqlite3

import pytest


@pytest.fixture()
def sc(monkeypatch):
    """sidecar with no DATABASE_URL — the SQLite path."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("BLAST_ENV_FILE", "/nonexistent/.env")
    import config
    importlib.reload(config)
    import sidecar
    importlib.reload(sidecar)
    yield sidecar
    importlib.reload(config)
    importlib.reload(sidecar)


# ------------------------------------------------------------- translation

def test_placeholders_are_translated(sc):
    assert sc._translate("SELECT * FROM t WHERE a = ?") == \
        "SELECT * FROM t WHERE a = %s"
    assert sc._translate("SELECT * FROM t WHERE a = ? AND b = ?") == \
        "SELECT * FROM t WHERE a = %s AND b = %s"


def test_a_question_mark_inside_a_string_literal_is_left_alone(sc):
    """It is data there, not a placeholder. Translating it would change the
    meaning of the query rather than its dialect."""
    out = sc._translate("SELECT * FROM t WHERE name = 'what?' AND b = ?")
    assert out.count("%s") == 1
    assert "'what?'" in out


def test_group_concat_becomes_string_agg(sc):
    assert sc._translate("SELECT group_concat(x) FROM t") == \
        "SELECT string_agg(x, ',') FROM t"
    # the DISTINCT has to survive, or the result changes
    assert sc._translate("SELECT group_concat(DISTINCT m.maintainer) FROM m") == \
        "SELECT string_agg(DISTINCT m.maintainer, ',') FROM m"


def test_insert_or_ignore_is_refused_rather_than_silently_wrong(sc):
    """ON CONFLICT has to name the conflicting columns, which a text
    substitution cannot know. Better a loud error at the call site than a
    statement the server rejects with something less obvious."""
    for sql in ("INSERT OR IGNORE INTO t VALUES (?)",
                "insert or replace into t values (?)"):
        with pytest.raises(ValueError, match="ON CONFLICT"):
            sc._translate(sql)


def test_the_translation_is_not_applied_to_sqlite(sc):
    """The SQLite path must see its own SQL untouched — the whole point is that
    one set of call sites serves both."""
    assert sc.IS_POSTGRES is False
    conn = sc.connect(":memory:")
    conn.executescript("CREATE TABLE t (a TEXT, b TEXT);")
    conn.execute("INSERT INTO t VALUES (?, ?)", ("x", "y"))
    assert conn.execute("SELECT b FROM t WHERE a = ?", ("x",)).fetchone()[0] == "y"
    conn.close()


def test_describe_never_contains_the_password(monkeypatch):
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql://user:hunter2@aws-0-us-west-2.pooler.supabase.com:6543/postgres")
    monkeypatch.setenv("BLAST_ENV_FILE", "/nonexistent/.env")
    import config
    importlib.reload(config)
    import sidecar
    importlib.reload(sidecar)
    try:
        d = sidecar.describe()
        assert "hunter2" not in str(d)
        assert d["backend"] == "postgres"
        assert d["pooled"] is True, "the pooler endpoint should be recognised"
    finally:
        importlib.reload(config)
        importlib.reload(sidecar)


def test_the_direct_endpoint_is_flagged(monkeypatch):
    """Supabase's direct endpoint holds a real backend per connection. Pointing
    a pool of web workers at it exhausts the project's connection limit."""
    monkeypatch.setenv("DATABASE_URL",
                       "postgresql://u:p@db.abcdef.supabase.co:5432/postgres")
    monkeypatch.setenv("BLAST_ENV_FILE", "/nonexistent/.env")
    import config
    importlib.reload(config)
    import sidecar
    importlib.reload(sidecar)
    try:
        assert sidecar.describe()["pooled"] is False
    finally:
        importlib.reload(config)
        importlib.reload(sidecar)


# ---------------------------------------------------------------- postgres

# config loads .env; without importing it first this reads a bare environment
# and skips the Postgres tests on a machine that is perfectly well configured.
import config  # noqa: E402

pg = pytest.mark.skipif(not config.get("DATABASE_URL"),
                        reason="no DATABASE_URL; see this module's docstring")


@pg
def test_schema_creates_the_indexes_the_lookups_need():
    """SQLite gives an index free on INTEGER PRIMARY KEY and UNIQUE. The ones
    that matter here are the reverse-direction lookups, which have to be
    declared on either backend — and are the whole reason this store exists."""
    import sidecar
    importlib.reload(sidecar)
    sidecar.ensure_schema()
    conn = sidecar.connect()
    names = {r[0] for r in conn.execute(
        "SELECT indexname FROM pg_indexes WHERE schemaname = 'public'").fetchall()}
    conn.close()

    for required in ("deps_dst",              # who depends on X
                     "release_deps_dep",      # which releases declare a range on X
                     "maintainers_pkg",       # who publishes X
                     "maintainers_name"):     # what else does this person publish
        assert required in names, f"{required} is missing; that lookup is a scan"


@pg
def test_copy_rows_round_trips_and_is_idempotent():
    """The crawler re-crawls. Running the same batch twice must not double the
    rows or raise on the primary key."""
    import sidecar
    importlib.reload(sidecar)
    sidecar.ensure_schema()
    conn = sidecar.connect()
    try:
        conn.execute("DELETE FROM deps WHERE src = 'itest-pkg'")
        conn.commit()
        rows = [("itest-pkg", f"dep-{i}", "prod", "^1.0.0") for i in range(500)]

        sidecar.copy_rows(conn, "deps", ["src", "dst", "kind", "range"], rows,
                          on_conflict="(src, dst, kind) DO NOTHING")
        conn.commit()
        first = conn.execute(
            "SELECT count(*) FROM deps WHERE src = ?", ("itest-pkg",)).fetchone()[0]
        assert first == 500

        sidecar.copy_rows(conn, "deps", ["src", "dst", "kind", "range"], rows,
                          on_conflict="(src, dst, kind) DO NOTHING")
        conn.commit()
        again = conn.execute(
            "SELECT count(*) FROM deps WHERE src = ?", ("itest-pkg",)).fetchone()[0]
        assert again == 500, "a second identical batch changed the row count"

        conn.execute("DELETE FROM deps WHERE src = 'itest-pkg'")
        conn.commit()
    finally:
        conn.close()


@pg
def test_upsert_meta_updates_rather_than_duplicating():
    import sidecar
    importlib.reload(sidecar)
    sidecar.ensure_schema()
    conn = sidecar.connect()
    try:
        sidecar.upsert_meta(conn, "itest", "one")
        sidecar.upsert_meta(conn, "itest", "two")
        conn.commit()
        rows = conn.execute("SELECT value FROM meta WHERE key = ?",
                            ("itest",)).fetchall()
        assert len(rows) == 1 and rows[0][0] == "two"
        conn.execute("DELETE FROM meta WHERE key = ?", ("itest",))
        conn.commit()
    finally:
        conn.close()


@pg
def test_a_connection_is_returned_to_the_pool_without_an_open_transaction():
    """psycopg leaves a connection INTRANS after any statement, including a
    read. Returning that to the pool holds a snapshot open on the server for as
    long as the connection sits idle."""
    import sidecar
    importlib.reload(sidecar)
    conn = sidecar.connect()
    conn.execute("SELECT 1")
    raw = conn._conn
    conn.close()
    assert raw.info.transaction_status == 0, "connection returned mid-transaction"
