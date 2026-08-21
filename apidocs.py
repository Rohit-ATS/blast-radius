"""The API reference, as data.

One definition, three renderings: JSON for the docs page, Markdown for a
README or an agent's context window, and plain text for a terminal. Keeping
them generated from the same structure is the only way they stay honest —
a prose-only doc drifts from the router within a day.
"""

from __future__ import annotations

BASE = "https://your-host"          # replaced with the live origin at render time

INTRO = (
    "Blast Radius answers one question for the npm ecosystem: when a package "
    "is compromised, who is actually exposed. The public API is the same code "
    "path the console uses — every response carries the latency of the real "
    "query that produced it, and nothing is ever invented. There are no rate "
    "tiers, no usage caps and no billing. Create a key and use it."
)

AUTH = (
    "Send your key as a bearer token:\n\n"
    "    Authorization: Bearer brk_live_...\n\n"
    "or, if a header is inconvenient, as `?api_key=`. Keys are created in the "
    "dashboard, shown once, and stored only as a SHA-256 digest — a copy of "
    "the database yields no working key. Revoking a key takes effect on the "
    "next request."
)

WEBHOOKS = """The watch delivers every alert to each endpoint registered on the \
account, as a POST with a JSON body. Deliveries are signed:

    X-BlastRadius-Signature: t=1787270000,v1=9f86d081...
    X-BlastRadius-Event: alert

The signature is HMAC-SHA256 over `<t>.<raw request body>` using that endpoint's
secret. Verify against the raw bytes rather than a re-serialised object, and
reject a timestamp more than a few minutes old — the timestamp is inside the
signed material precisely so a captured payload cannot be replayed later.

Three attempts are made per delivery with backoff. An endpoint that fails 20
times in a row is disabled rather than retried forever: a delivery queue that
grows without limit because someone's staging server is down is an outage of
its own.

Body:

    {
      "type": "alert",
      "id": "alert_9c1f",
      "level": "high",
      "title": "debug blast radius grew by 141",
      "detail": "Now 3,828 packages transitively exposed, was 3,687.",
      "data": { "package": "debug", "total": 3828, "previous": 3687, "delta": 141 },
      "created_at": 1787270000.0
    }"""

ERRORS = [
    ("200", "ok", "The query ran. `latency_ms` is the measured time."),
    ("401", "missing_key / bad_key", "No key, an unknown key, or a revoked key."),
    ("404", "not_in_graph", "The package has not been crawled yet. The response says so rather than returning an empty result that reads as safety."),
    ("429", "rate_limited", "Only applied to anonymous browser traffic. Keyed API calls are not capped."),
    ("503", "graph_warming", "HydraDB is cold after a restart. Retry in a few seconds; the client in the console does this automatically."),
]

ENDPOINTS = [
    {
        "method": "GET",
        "path": "/api/v1/blast",
        "title": "Blast radius",
        "summary": "Who is transitively exposed if this package is compromised.",
        "params": [
            ("name", "string", "required", "Package name, e.g. `debug`."),
            ("depth", "integer", "1–5, default 5", "How many hops to walk."),
            ("limit", "integer", "default 5000", "Cap on returned victims."),
        ],
        "example": "/api/v1/blast?name=debug&depth=5",
        "response": {
            "total": 3688,
            "depth": 5,
            "queries": 6,
            "histogram": [{"depth": 1, "packages": 739}, {"depth": 2, "packages": 1361}],
            "victims": ["@11ty/eleventy-dev-server", "express"],
            "truncated": False,
            "latency_ms": 1492.0,
            "ok": True,
        },
    },
    {
        "method": "GET",
        "path": "/api/v1/resolve",
        "title": "Semver resolution",
        "summary": "Of the packages that declare a range on this one, how many would "
                   "actually have resolved the bad version, and how many were shielded by a pin.",
        "params": [
            ("name", "string", "required", "Package name."),
            ("bad_version", "string", "required", "The compromised version, e.g. `4.4.2`."),
        ],
        "example": "/api/v1/resolve?name=debug&bad_version=4.4.2",
        "response": {
            "exposed_count": 407,
            "shielded_count": 385,
            "checked": 3659,
            "exposed": [{"name": "@11ty/eleventy-dev-server", "ranges": ["^4.4.0"]}],
            "latency_ms": 88.4,
            "ok": True,
        },
    },
    {
        "method": "POST",
        "path": "/api/v1/lockfile",
        "title": "Lockfile check",
        "summary": "Post a package-lock.json body and find out whether a specific incident "
                   "reaches you, and by which path. Nothing is stored.",
        "params": [
            ("name", "string", "required", "The compromised package."),
            ("bad_version", "string", "optional", "The compromised version."),
            ("depth", "integer", "default 5", "How far to walk."),
        ],
        "body": "The raw package-lock.json (v1, v2 or v3). yarn.lock and pnpm-lock.yaml also parse.",
        "example": "curl -X POST '{BASE}/api/v1/lockfile?name=debug&bad_version=4.4.2' \\\n"
                   "  -H 'Authorization: Bearer brk_live_...' \\\n"
                   "  --data-binary @package-lock.json",
        "response": {
            "verdict": "EXPOSED",
            "installed_version": "4.3.4",
            "paths": [{"path": ["your-app", "express", "debug"]}],
            "latency_ms": 210.5,
            "ok": True,
        },
    },
    {
        "method": "POST",
        "path": "/api/v1/audit",
        "title": "Malware audit",
        "summary": "Check every package in a lockfile against osv.dev, including MAL- "
                   "identifiers for confirmed malicious packages.",
        "params": [("(none)", "", "", "The lockfile is the body.")],
        "body": "The raw package-lock.json.",
        "example": "curl -X POST '{BASE}/api/v1/audit' \\\n"
                   "  -H 'Authorization: Bearer brk_live_...' \\\n"
                   "  --data-binary @package-lock.json",
        "response": {
            "verdict": "VULNERABLE",
            "checked": 812,
            "findings": [{"package": "event-stream", "version": "3.3.6",
                          "advisories": [{"id": "MAL-2018-001", "kind": "malware"}]}],
            "latency_ms": 1840.2,
            "ok": True,
        },
    },
    {
        "method": "GET",
        "path": "/api/v1/maintainers",
        "title": "Maintainer pivot",
        "summary": "What else the people who publish this package control — the attacker's "
                   "next move after one account falls.",
        "params": [("name", "string", "required", "Package name.")],
        "example": "/api/v1/maintainers?name=debug",
        "response": {
            "maintainers": ["qix", "tootallnate"],
            "also_controls": [{"package": "https-proxy-agent", "direct_dependents": 74}],
            "latency_ms": 20.1,
            "ok": True,
        },
    },
    {
        "method": "GET",
        "path": "/api/v1/typosquats",
        "title": "Typosquat ring",
        "summary": "Names one edit away from this one that exist on npm right now.",
        "params": [("name", "string", "required", "Package name.")],
        "example": "/api/v1/typosquats?name=debug",
        "response": {
            "candidates": 9,
            "existing": [{"name": "dbug", "latest": "0.4.2", "in_graph": False}],
            "ok": True,
        },
    },
    {
        "method": "GET",
        "path": "/api/v1/subgraph",
        "title": "Subgraph",
        "summary": "The exposed set with per-package depth and dependent counts, plus the "
                   "edges between them — enough to draw the graph yourself.",
        "params": [
            ("name", "string", "required", "Package name."),
            ("depth", "integer", "1–5, default 2", "How far to walk."),
            ("limit", "integer", "default 60", "Node cap."),
        ],
        "example": "/api/v1/subgraph?name=debug&depth=2&limit=60",
        "response": {
            "root": "debug",
            "nodes": [{"name": "express", "depth": 1, "dependents": 212}],
            "edges": [{"from": "debug", "to": "express"}],
            "ok": True,
        },
    },
    {
        "method": "GET",
        "path": "/api/v1/monitors",
        "title": "Monitors",
        "summary": "List the packages this key's account watches. POST to add one, "
                   "DELETE /api/v1/monitors/{id} to stop. Each is re-measured on a timer "
                   "and any movement raises an alert on the dashboard.",
        "params": [("package", "string", "POST only", "Package to watch.")],
        "example": "/api/v1/monitors",
        "response": {
            "monitors": [{"id": "mon_ab12", "package": "debug", "last_total": 3688,
                          "last_check_at": 1787270000.0}],
            "ok": True,
        },
    },
    {
        "method": "GET",
        "path": "/api/v1/alerts",
        "title": "Alerts",
        "summary": "Everything the watch has raised for this account, newest first.",
        "params": [("limit", "integer", "default 50", "How many to return.")],
        "example": "/api/v1/alerts?limit=20",
        "response": {
            "alerts": [{"level": "high", "title": "debug blast radius grew by 141",
                        "created_at": 1787270000.0}],
            "ok": True,
        },
    },
    {
        "method": "GET",
        "path": "/api/v1/webhooks",
        "title": "Webhooks",
        "summary": "Endpoints this account's alerts are delivered to. POST to add one; "
                   "the signing secret is returned once. Every delivery carries "
                   "`X-BlastRadius-Signature: t=<unix>,v1=<hmac-sha256>` over "
                   "`<timestamp>.<raw body>` — verify it before trusting a payload.",
        "params": [("url", "string", "POST only", "Where to deliver. Must be http(s).")],
        "example": "/api/v1/webhooks",
        "response": {
            "webhooks": [{"id": "wh_ab12", "url": "https://hooks.example.com/br",
                          "deliveries": 42, "failures": 0, "active": 1}],
            "ok": True,
        },
    },
    {
        "method": "GET",
        "path": "/api/v1/whoami",
        "title": "Whoami",
        "summary": "Confirm a key works and see which account and key it belongs to. "
                   "The cheapest possible integration test.",
        "params": [],
        "example": "/api/v1/whoami",
        "response": {
            "account": {"email": "you@example.com"},
            "key": {"name": "Default key", "prefix": "brk_live_a1b2c3"},
            "ok": True,
        },
    },
]

QUICKSTARTS = [
    ("curl", "bash",
     "curl -H 'Authorization: Bearer {KEY}' \\\n"
     "  '{BASE}/api/v1/blast?name=debug&depth=5'"),
    ("JavaScript", "js",
     "const res = await fetch('{BASE}/api/v1/blast?name=debug&depth=5', {\n"
     "  headers: { Authorization: 'Bearer {KEY}' },\n"
     "});\n"
     "const { total, histogram, latency_ms } = await res.json();\n"
     "console.log(`${total} packages exposed in ${latency_ms}ms`);"),
    ("Python", "python",
     "import requests\n\n"
     "r = requests.get(\n"
     "    '{BASE}/api/v1/blast',\n"
     "    params={'name': 'debug', 'depth': 5},\n"
     "    headers={'Authorization': 'Bearer {KEY}'},\n"
     ")\n"
     "r.raise_for_status()\n"
     "print(r.json()['total'], 'packages exposed')"),
    ("CI gate", "bash",
     "# fail the build if anything in the lockfile is known-malicious\n"
     "curl -sS -X POST '{BASE}/api/v1/audit' \\\n"
     "  -H 'Authorization: Bearer {KEY}' \\\n"
     "  --data-binary @package-lock.json \\\n"
     "  | jq -e '.verdict != \"COMPROMISED\"'"),
]


def _fill(text: str, base: str, key: str) -> str:
    return text.replace("{BASE}", base).replace("{KEY}", key)


def as_json(base: str = BASE, key: str = "brk_live_...") -> dict:
    return {
        "name": "Blast Radius API",
        "version": "1",
        "base_url": f"{base}/api/v1",
        "intro": INTRO,
        "auth": AUTH,
        "webhooks": WEBHOOKS,
        "free": True,
        "rate_limited": False,
        "quickstarts": [{"label": l, "lang": lang, "code": _fill(c, base, key)}
                        for l, lang, c in QUICKSTARTS],
        "endpoints": ENDPOINTS,
        "errors": [{"status": s, "code": c, "meaning": m} for s, c, m in ERRORS],
    }


def as_markdown(base: str = BASE, key: str = "brk_live_...") -> str:
    import json as _json
    out = [f"# Blast Radius API\n", INTRO, "",
           f"**Base URL** `{base}/api/v1`  ",
           "**Cost** free, no rate limit, no usage cap  ",
           "**License** MIT\n",
           "## Authentication\n", AUTH, ""]

    out.append("## Quickstart\n")
    for label, lang, code in QUICKSTARTS:
        out += [f"### {label}\n", f"```{lang}", _fill(code, base, key), "```", ""]

    out.append("## Endpoints\n")
    for e in ENDPOINTS:
        out += [f"### {e['method']} `{e['path']}`\n", f"**{e['title']}** — {e['summary']}\n"]
        if e.get("params"):
            out += ["| Parameter | Type | Notes | Description |",
                    "| --- | --- | --- | --- |"]
            for p in e["params"]:
                out.append(f"| `{p[0]}` | {p[1]} | {p[2]} | {p[3]} |")
            out.append("")
        if e.get("body"):
            out += [f"**Body** — {e['body']}\n"]
        out += ["```bash",
                _fill(e["example"], base, key) if e["example"].startswith("curl")
                else f"curl -H 'Authorization: Bearer {key}' '{base}{_fill(e['example'], base, key)}'",
                "```", "",
                "```json", _json.dumps(e["response"], indent=2), "```", ""]

    out.append("## Errors\n")
    out += ["| Status | Code | Meaning |", "| --- | --- | --- |"]
    for s, c, m in ERRORS:
        out.append(f"| {s} | `{c}` | {m} |")
    out.append("")
    return "\n".join(out)


def as_text(base: str = BASE, key: str = "brk_live_...") -> str:
    import json as _json
    W = 78
    rule = "=" * W
    out = ["BLAST RADIUS API", rule, "", INTRO, "",
           f"Base URL : {base}/api/v1",
           "Cost     : free, no rate limit, no usage cap",
           "License  : MIT", "",
           "AUTHENTICATION", "-" * W, AUTH.replace("`", ""), ""]

    out += ["QUICKSTART", "-" * W]
    for label, _lang, code in QUICKSTARTS:
        out += [f"[{label}]", _fill(code, base, key), ""]

    out += ["WEBHOOKS", "-" * W, WEBHOOKS.replace("`", ""), ""]
    out += ["ENDPOINTS", "-" * W]
    for e in ENDPOINTS:
        out += [f"{e['method']} {e['path']}",
                f"  {e['title']} — {e['summary']}"]
        for p in e.get("params") or []:
            if p[0] == "(none)":
                continue
            out.append(f"    {p[0]:<14} {p[1]:<10} {p[2]:<18} {p[3]}")
        if e.get("body"):
            out.append(f"    body: {e['body']}")
        out += ["", "  example:",
                "    " + _fill(e["example"], base, key).replace("\n", "\n    "),
                "", "  returns:",
                "    " + _json.dumps(e["response"], indent=2).replace("\n", "\n    "),
                ""]

    out += ["ERRORS", "-" * W]
    for s, c, m in ERRORS:
        out.append(f"  {s}  {c:<22} {m}")
    out.append("")
    return "\n".join(out)
