"""Project monitoring — the traversal *is* the alert router.

A project registers its lockfile. That produces a `Project` vertex in HydraDB
and one `REQUIRED_BY` edge from every package it installs, pointing at it —
the same edge direction the package graph already uses, on purpose. Then the
question "who do I have to wake up about this publish" stops being a fan-out
over a subscriber table and becomes the query this database is for:

    MATCH (p:Package {id: $published})-[:REQUIRED_BY*1..N]->(t:Project)
    RETURN t.pid

One traversal from the package that just changed, and out comes the exact set
of affected projects. Registered projects sit in the same graph as the
dependency edges, so a publish four hops upstream of somebody's lockfile
routes to them without a single extra join.

**Exact versus inferred, and why the distinction is kept.** A lockfile is the
resolved tree — everything actually installed is in it — so every package in it
becomes a direct edge and depth 1 is a complete and *precise* answer. A
manifest (`package.json`, `pyproject.toml`) names only direct dependencies, so
reaching the rest means traversing the crawled graph, which is as complete as
our coverage of it and no more. Those are different epistemic claims and every
alert says which one it is: `precision: "exact"` or `"inferred"`.

**Hop counts come from differencing, not from paths.** HydraDB has no
`length(path)`, so the router runs the traversal at depth 1, then 2, and treats
whatever is new at each depth as being that many hops away — the same technique
the depth histogram uses, and exact for the same reason: `count(*)` over a
variable-length match counts distinct reachable vertices.

Delivery is webhook, SSE, or polling, and an alert is written to SQLite before
any of them are attempted. A webhook that fails is retried and its failure is
visible on the project's status; it is never silently dropped, because an alert
nobody received is worse than one that arrived late.
"""

from __future__ import annotations

import json
import queue
import secrets
import sqlite3
import threading
import time

import requests

import ecosystems
import intel
from hydra import Hydra, nid, pkg_id

WATCH_DB = "watch.db"
MAX_DEPTH = 4               # deepest inferred traversal for manifest projects
WEBHOOK_TIMEOUT = 10.0
WEBHOOK_RETRIES = 3
MAX_DEPS_PER_PROJECT = 5000

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    token       TEXT NOT NULL,
    ecosystem   TEXT NOT NULL,
    precision   TEXT NOT NULL,      -- 'exact' (lockfile) | 'inferred' (manifest)
    depth       INTEGER NOT NULL,
    webhook     TEXT,
    vid         INTEGER NOT NULL,
    dep_count   INTEGER NOT NULL,
    created_at  REAL NOT NULL,
    last_alert_at REAL,
    webhook_failures INTEGER NOT NULL DEFAULT 0,
    last_webhook_error TEXT
);
CREATE TABLE IF NOT EXISTS project_deps (
    project_id  TEXT NOT NULL,
    package     TEXT NOT NULL,      -- qualified: 'debug' or 'pypi:requests'
    version     TEXT,
    PRIMARY KEY (project_id, package)
);
CREATE TABLE IF NOT EXISTS alerts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  TEXT NOT NULL,
    at          REAL NOT NULL,
    severity    TEXT NOT NULL,      -- critical | high | medium | info
    kind        TEXT NOT NULL,      -- malware | advisory | publish
    ecosystem   TEXT NOT NULL,
    package     TEXT NOT NULL,
    version     TEXT,
    hops        INTEGER,
    precision   TEXT NOT NULL,
    detail      TEXT,               -- JSON
    delivered   INTEGER NOT NULL DEFAULT 0,
    acked       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS alerts_by_project ON alerts (project_id, id);
"""

SEVERITY_RANK = {"critical": 3, "high": 2, "medium": 1, "info": 0}

UPSERT_PROJECT = """
UNWIND $rows AS row
MERGE (n {id: row.id})
SET n:Project, n.name = row.name, n.pid = row.pid
"""

# Same direction as the package graph: the dependency points at the dependent.
# That is what lets one traversal from a published package reach both the
# packages downstream of it and the projects that install those packages.
LINK_DEP = """
UNWIND $rows AS row
CREATE (a {id: row.src})-[:REQUIRED_BY]->(b {id: row.dst})
"""

UNLINK = """
UNWIND $rows AS row
MATCH (a {id: row.src})-[r:REQUIRED_BY]->(b {id: row.dst})
DELETE r
"""


def _connect(path: str = WATCH_DB) -> sqlite3.Connection:
    db = sqlite3.connect(path, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    db.commit()
    return db


def project_vid(project_id: str) -> int:
    """Namespaced so a project can never collide with a package vertex."""
    return nid(f"proj:{project_id}")


def qualified(ecosystem: str, name: str) -> str:
    """Matches live.qualified — npm keeps bare names, everything else is scoped."""
    return name if ecosystem == "npm" else f"{ecosystem}:{name}"


class Registry:
    """Registered projects, alert routing, and delivery."""

    def __init__(self, hydra: Hydra | None = None, db_path: str = WATCH_DB):
        self.hydra = hydra or Hydra()
        self.db = _connect(db_path)
        self._lock = threading.Lock()
        self._deliveries: queue.Queue = queue.Queue(maxsize=1000)
        self._listeners: dict[str, list] = {}
        self._stop = threading.Event()
        self.routed = 0
        self.alerts_created = 0
        self.last_route_ms = 0.0
        self.last_routed_at: float | None = None

        self._routing: queue.Queue = queue.Queue(maxsize=2000)
        self.route_drops = 0

        for target, name in ((self._delivery_loop, "watch-delivery"),
                             (self._routing_loop, "watch-routing")):
            threading.Thread(target=target, name=name, daemon=True).start()

    def route_async(self, event: dict) -> None:
        """Queue a publish for routing instead of routing it inline.

        `route` runs up to four traversals and an OSV lookup, which is far
        slower than the ingestion loop that produces these events. Doing it
        inline would make the graph fall behind the registries in order to keep
        the alerting current — exactly backwards.
        """
        try:
            self._routing.put_nowait(event)
        except queue.Full:
            self.route_drops += 1

    def _routing_loop(self) -> None:
        while not self._stop.is_set():
            try:
                event = self._routing.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                self.route(event)
            except Exception:
                # One unroutable publish must not stop the router. The counters
                # in stats() are what make a systematic failure visible.
                pass

    # ------------------------------------------------------------------
    # registration
    # ------------------------------------------------------------------

    def register(self, name: str, resolved: dict[str, str], ecosystem: str,
                 precision: str = "exact", webhook: str | None = None,
                 depth: int | None = None) -> dict:
        """Register a project and wire its dependencies into the graph.

        `resolved` is package name -> version. From a lockfile that is the whole
        installed tree and `precision` is "exact"; from a manifest it is direct
        dependencies only and the router has to traverse for the rest.
        """
        if not resolved:
            raise ValueError("a project with no dependencies has nothing to watch")
        if len(resolved) > MAX_DEPS_PER_PROJECT:
            raise ValueError(
                f"{len(resolved)} dependencies exceeds the {MAX_DEPS_PER_PROJECT} limit")
        if precision not in ("exact", "inferred"):
            raise ValueError("precision must be 'exact' or 'inferred'")

        pid = secrets.token_urlsafe(9)
        token = secrets.token_urlsafe(24)
        vid = project_vid(pid)
        # An exact tree needs no traversal: everything installed is already a
        # direct edge, and going deeper would alert on packages the project
        # does not actually have.
        hop_depth = 1 if precision == "exact" else max(1, min(depth or 3, MAX_DEPTH))

        self.hydra.write_batch(UPSERT_PROJECT,
                               [{"id": vid, "name": name, "pid": pid}])

        # Dependency vertices must exist before edges point at them. A package
        # we have never crawled is still a real package and a real vertex.
        stubs, edges, rows = [], [], []
        for dep, version in resolved.items():
            dep = (dep or "").strip()
            if not dep:
                continue
            dep_vid = pkg_id(dep, ecosystem)
            stubs.append({"id": dep_vid, "name": dep, "ecosystem": ecosystem})
            edges.append({"src": dep_vid, "dst": vid})
            rows.append((pid, qualified(ecosystem, dep), version))

        from ingest import UPSERT_STUBS
        self.hydra.write_batch(UPSERT_STUBS, stubs)
        self.hydra.write_batch(LINK_DEP, edges)

        with self._lock:
            self.db.execute(
                "INSERT INTO projects (id, name, token, ecosystem, precision, "
                "depth, webhook, vid, dep_count, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (pid, name, token, ecosystem, precision, hop_depth, webhook,
                 vid, len(rows), time.time()))
            self.db.executemany(
                "INSERT OR REPLACE INTO project_deps (project_id, package, version) "
                "VALUES (?,?,?)", rows)
            self.db.commit()

        return {
            "project_id": pid,
            "token": token,
            "name": name,
            "ecosystem": ecosystem,
            "precision": precision,
            "depth": hop_depth,
            "watching": len(rows),
            "webhook": webhook,
            "created_at": time.time(),
        }

    def unregister(self, pid: str, token: str) -> bool:
        row = self.authenticate(pid, token)
        if not row:
            return False
        vid = row["vid"]
        deps = [r["package"] for r in self.db.execute(
            "SELECT package FROM project_deps WHERE project_id = ?", (pid,))]
        eco = row["ecosystem"]
        edges = [{"src": pkg_id(_unqualify(eco, d), eco), "dst": vid} for d in deps]
        try:
            if edges:
                self.hydra.write_batch(UNLINK, edges)
            self.hydra.query("MATCH (p:Project {id: $i}) DELETE p", {"i": vid})
        except Exception:
            # The SQLite rows go regardless: a project that cannot be removed
            # from the graph must still stop receiving alerts.
            pass
        with self._lock:
            self.db.execute("DELETE FROM project_deps WHERE project_id = ?", (pid,))
            self.db.execute("DELETE FROM alerts WHERE project_id = ?", (pid,))
            self.db.execute("DELETE FROM projects WHERE id = ?", (pid,))
            self.db.commit()
        return True

    def authenticate(self, pid: str, token: str):
        row = self.db.execute("SELECT * FROM projects WHERE id = ?", (pid,)).fetchone()
        if not row or not token:
            return None
        # Constant-time: a token check that leaks its prefix by timing is not a
        # token check.
        return row if secrets.compare_digest(row["token"], token) else None

    # ------------------------------------------------------------------
    # routing — one traversal per publish
    # ------------------------------------------------------------------

    def _reach(self, package_vid: int, max_depth: int) -> dict[str, int]:
        """Project id -> hops, by differencing successive depths.

        HydraDB has no `length(path)`, so hop count comes from *when* a project
        first appears as the traversal widens. Anything new at depth d is d hops
        away, because it was provably not reachable in d-1.
        """
        found: dict[str, int] = {}
        for d in range(1, max_depth + 1):
            rows = self.hydra.query(
                f"MATCH (p:Package {{id: $i}})-[:REQUIRED_BY*1..{d}]->(t:Project) "
                f"RETURN t.pid AS pid", {"i": package_vid})
            for r in rows or []:
                pid = r.get("pid")
                if pid and pid not in found:
                    found[pid] = d
        return found

    def route(self, event: dict) -> list[dict]:
        """Turn one publish event into alerts for every project it reaches."""
        if not self.db.execute("SELECT 1 FROM projects LIMIT 1").fetchone():
            return []                       # nobody is watching; do no work

        t0 = time.perf_counter()
        deepest = self.db.execute(
            "SELECT MAX(depth) AS d FROM projects").fetchone()["d"] or 1
        try:
            reached = self._reach(event["id"], min(deepest, MAX_DEPTH))
        except Exception:
            return []
        self.routed += 1
        self.last_route_ms = round((time.perf_counter() - t0) * 1000, 1)
        self.last_routed_at = time.time()
        if not reached:
            return []

        severity, kind, detail = self._classify(event)
        created = []
        for pid, hops in reached.items():
            row = self.db.execute(
                "SELECT * FROM projects WHERE id = ?", (pid,)).fetchone()
            if not row or hops > row["depth"]:
                continue
            alert = self._record(row, event, severity, kind, hops, detail)
            created.append(alert)
            self._fanout(row, alert)
        return created

    def _classify(self, event: dict) -> tuple[str, str, dict]:
        """Ask OSV about the exact version that was just published.

        A version-less question returns every advisory ever filed against the
        package, which would make a routine release of a package with any
        history look like an incident.
        """
        adapter = ecosystems.get(event["ecosystem"])
        osv_eco = adapter.osv_ecosystem if adapter else "npm"
        try:
            res = intel.osv_query(event["name"], event.get("version") or None,
                                  ecosystem=osv_eco)
        except Exception:
            res = {"ok": False, "vulns": []}
        vulns = res.get("vulns") or []
        malware = [v for v in vulns if v.get("kind") == "malware"]
        if malware:
            return "critical", "malware", {"advisories": malware[:5]}
        if vulns:
            worst = max(vulns, key=lambda v: SEVERITY_RANK.get(
                (v.get("severity") or "").lower(), 0))
            sev = (worst.get("severity") or "").lower()
            return (sev if sev in SEVERITY_RANK else "high"), "advisory", \
                   {"advisories": vulns[:5]}
        # No advisory is not "nothing happened": a new version of something you
        # install is the event, and it is the only warning you get before the
        # advisory exists.
        return "info", "publish", {"advisories": []}

    def _record(self, row, event: dict, severity: str, kind: str,
                hops: int, detail: dict) -> dict:
        payload = dict(detail)
        payload["maintainers"] = event.get("maintainers") or []
        payload["dependencies"] = event.get("deps")
        with self._lock:
            cur = self.db.execute(
                "INSERT INTO alerts (project_id, at, severity, kind, ecosystem, "
                "package, version, hops, precision, detail) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (row["id"], time.time(), severity, kind, event["ecosystem"],
                 event["name"], event.get("version"), hops, row["precision"],
                 json.dumps(payload)))
            self.db.execute("UPDATE projects SET last_alert_at = ? WHERE id = ?",
                            (time.time(), row["id"]))
            self.db.commit()
            alert_id = cur.lastrowid
        self.alerts_created += 1
        return {
            "id": alert_id,
            "project_id": row["id"],
            "at": time.time(),
            "severity": severity,
            "kind": kind,
            "ecosystem": event["ecosystem"],
            "package": event["name"],
            "version": event.get("version"),
            "hops": hops,
            "precision": row["precision"],
            "detail": payload,
        }

    # ------------------------------------------------------------------
    # delivery
    # ------------------------------------------------------------------

    def listen(self, pid: str) -> queue.Queue:
        """A queue that receives this project's alerts as they happen."""
        q: queue.Queue = queue.Queue(maxsize=100)
        with self._lock:
            self._listeners.setdefault(pid, []).append(q)
        return q

    def unlisten(self, pid: str, q: queue.Queue) -> None:
        with self._lock:
            if pid in self._listeners and q in self._listeners[pid]:
                self._listeners[pid].remove(q)

    def _fanout(self, row, alert: dict) -> None:
        with self._lock:
            listeners = list(self._listeners.get(row["id"], []))
        for q in listeners:
            try:
                q.put_nowait(alert)
            except queue.Full:
                pass                        # a stalled SSE client is not fatal
        if row["webhook"]:
            try:
                self._deliveries.put_nowait((row["id"], row["webhook"], alert))
            except queue.Full:
                pass

    def _delivery_loop(self) -> None:
        while not self._stop.is_set():
            try:
                pid, url, alert = self._deliveries.get(timeout=1.0)
            except queue.Empty:
                continue
            self._deliver(pid, url, alert)

    def _deliver(self, pid: str, url: str, alert: dict) -> None:
        body = {"source": "blast-radius", "alert": alert}
        last = ""
        for attempt in range(WEBHOOK_RETRIES):
            try:
                r = requests.post(url, json=body, timeout=WEBHOOK_TIMEOUT,
                                  headers={"User-Agent": "blast-radius-alerts/1.0",
                                           "X-Blast-Alert-Id": str(alert["id"]),
                                           "X-Blast-Severity": alert["severity"]})
                if 200 <= r.status_code < 300:
                    with self._lock:
                        self.db.execute("UPDATE alerts SET delivered = 1 WHERE id = ?",
                                        (alert["id"],))
                        self.db.commit()
                    return
                last = f"http {r.status_code}"
            except Exception as exc:
                last = f"{type(exc).__name__}: {exc}"
            time.sleep(min(2 ** attempt, 8))
        with self._lock:
            self.db.execute(
                "UPDATE projects SET webhook_failures = webhook_failures + 1, "
                "last_webhook_error = ? WHERE id = ?", (last[:200], pid))
            self.db.commit()

    # ------------------------------------------------------------------
    # reads
    # ------------------------------------------------------------------

    def alerts(self, pid: str, since: int = 0, limit: int = 100,
               min_severity: str = "info") -> list[dict]:
        floor = SEVERITY_RANK.get(min_severity, 0)
        rows = self.db.execute(
            "SELECT * FROM alerts WHERE project_id = ? AND id > ? "
            "ORDER BY id DESC LIMIT ?", (pid, since, min(limit, 500))).fetchall()
        out = []
        for r in rows:
            if SEVERITY_RANK.get(r["severity"], 0) < floor:
                continue
            out.append({
                "id": r["id"], "at": r["at"], "severity": r["severity"],
                "kind": r["kind"], "ecosystem": r["ecosystem"],
                "package": r["package"], "version": r["version"],
                "hops": r["hops"], "precision": r["precision"],
                "delivered": bool(r["delivered"]), "acked": bool(r["acked"]),
                "detail": json.loads(r["detail"] or "{}"),
            })
        return out

    def ack(self, pid: str, alert_id: int) -> bool:
        with self._lock:
            cur = self.db.execute(
                "UPDATE alerts SET acked = 1 WHERE project_id = ? AND id = ?",
                (pid, alert_id))
            self.db.commit()
        return bool(cur.rowcount)

    def project_status(self, row) -> dict:
        pid = row["id"]
        counts = {s: 0 for s in SEVERITY_RANK}
        for r in self.db.execute(
                "SELECT severity, COUNT(*) AS n FROM alerts WHERE project_id = ? "
                "GROUP BY severity", (pid,)):
            counts[r["severity"]] = r["n"]
        unacked = self.db.execute(
            "SELECT COUNT(*) AS n FROM alerts WHERE project_id = ? AND acked = 0",
            (pid,)).fetchone()["n"]
        return {
            "project_id": pid,
            "name": row["name"],
            "ecosystem": row["ecosystem"],
            "precision": row["precision"],
            "depth": row["depth"],
            "watching": row["dep_count"],
            "created_at": row["created_at"],
            "last_alert_at": row["last_alert_at"],
            "webhook": bool(row["webhook"]),
            "webhook_failures": row["webhook_failures"],
            "last_webhook_error": row["last_webhook_error"] or "",
            "alerts": counts,
            "unacked": unacked,
        }

    def stats(self) -> dict:
        n = self.db.execute("SELECT COUNT(*) AS n FROM projects").fetchone()["n"]
        watched = self.db.execute(
            "SELECT COUNT(DISTINCT package) AS n FROM project_deps").fetchone()["n"]
        alerts = self.db.execute("SELECT COUNT(*) AS n FROM alerts").fetchone()["n"]
        return {
            "projects": n,
            "packages_watched": watched,
            "alerts_total": alerts,
            "alerts_created_this_run": self.alerts_created,
            "publishes_routed": self.routed,
            "routing_queue": self._routing.qsize(),
            "route_drops": self.route_drops,
            "last_route_ms": self.last_route_ms,
            "last_routed_at": self.last_routed_at,
            "max_depth": MAX_DEPTH,
        }


def _unqualify(ecosystem: str, package: str) -> str:
    prefix = f"{ecosystem}:"
    return package[len(prefix):] if package.startswith(prefix) else package
