"""The platform surface: accounts, keys, monitors, alerts, and the keyed v1 API.

Mounted onto the existing FastAPI app by `server.mount_platform()`. The graph
handlers are injected rather than imported so this module never reaches back
into server.py — the import graph stays one-directional and this file can be
tested with fakes.

The v1 API is a thin, key-authenticated façade over the same handlers the
console calls. It exists so that an integrator gets a stable contract
(`/api/v1/...`) that can evolve separately from the console's internal routes.
"""

from __future__ import annotations

import json
import os
import time

from fastapi import APIRouter, Request, Query
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse

import accounts
import apidocs

COOKIE = "br_session"
SECURE_COOKIES = os.environ.get("SECURE_COOKIES", "").lower() in ("1", "true", "yes")

router = APIRouter()

# Injected by mount(). Keys are the v1 route names.
HANDLERS: dict = {}


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _fail(message: str, status: int = 400, code: str = "bad_request", **extra):
    return JSONResponse({"ok": False, "error": code, "message": message, **extra},
                        status_code=status)


def _client(request: Request) -> tuple[str, str]:
    ip = request.client.host if request.client else "?"
    return ip, request.headers.get("user-agent", "")


def _origin(request: Request) -> str:
    return str(request.base_url).rstrip("/")


def _account(request: Request) -> dict | None:
    return accounts.session_account(request.cookies.get(COOKIE))


def _require_account(request: Request) -> dict:
    acct = _account(request)
    if not acct:
        raise accounts.AuthError("sign in to continue.", 401, "not_signed_in")
    return acct


def _bearer(request: Request) -> str | None:
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return (request.query_params.get("api_key")
            or request.headers.get("x-api-key") or None)


def _require_key(request: Request) -> dict:
    """Resolve the presented API key, or raise. Also writes the security log
    line — an integrator can see every call their key made."""
    secret = _bearer(request)
    if not secret:
        raise accounts.AuthError(
            "this endpoint needs an API key. Create one at /developers, then send "
            "it as `Authorization: Bearer brk_live_...`.", 401, "missing_key")
    key = accounts.resolve_key(secret)
    if not key:
        raise accounts.AuthError(
            "that key is unknown or has been revoked.", 401, "bad_key")
    ip, agent = _client(request)
    accounts.log_event(key["account_id"], "api.call",
                       f"{request.method} {request.url.path}", ip, agent, key["id"])
    return key


def _guard(fn):
    """Turn AuthError into the project's standard failure envelope."""
    async def wrapper(*a, **kw):
        try:
            result = fn(*a, **kw)
            if hasattr(result, "__await__"):
                result = await result
            return result
        except accounts.AuthError as exc:
            return _fail(exc.message, exc.status, exc.code)
    wrapper.__name__ = fn.__name__
    return wrapper


def _cookie(response: JSONResponse, token: str) -> JSONResponse:
    response.set_cookie(
        COOKIE, token,
        max_age=accounts.SESSION_DAYS * 86400,
        httponly=True, samesite="lax", secure=SECURE_COOKIES, path="/")
    return response


# --------------------------------------------------------------------------
# auth
# --------------------------------------------------------------------------

@router.post("/api/auth/signup")
@_guard
async def auth_signup(request: Request):
    body = await request.json()
    ip, agent = _client(request)
    out = accounts.signup(body.get("email", ""), body.get("password", ""),
                          body.get("name", ""), ip, agent)
    token = accounts.start_session(out["account"]["id"], ip, agent)
    return _cookie(JSONResponse({"ok": True, **out}), token)


@router.post("/api/auth/login")
@_guard
async def auth_login(request: Request):
    body = await request.json()
    ip, agent = _client(request)
    out = accounts.login(body.get("email", ""), body.get("password", ""), ip, agent)
    token = accounts.start_session(out["account"]["id"], ip, agent)
    return _cookie(JSONResponse({"ok": True, **out}), token)


@router.post("/api/auth/logout")
async def auth_logout(request: Request):
    accounts.end_session(request.cookies.get(COOKIE))
    res = JSONResponse({"ok": True})
    res.delete_cookie(COOKIE, path="/")
    return res


@router.get("/api/auth/me")
async def auth_me(request: Request):
    acct = _account(request)
    if not acct:
        return JSONResponse({"ok": True, "account": None,
                             "provider": "supabase" if accounts.SUPABASE_ON else "local"})
    return {"ok": True, "account": acct, "usage": accounts.key_usage(acct["id"]),
            "provider": "supabase" if accounts.SUPABASE_ON else "local"}


# --------------------------------------------------------------------------
# the vault
# --------------------------------------------------------------------------

@router.get("/api/keys")
@_guard
async def keys_list(request: Request):
    acct = _require_account(request)
    return {"ok": True, "keys": accounts.list_keys(acct["id"])}


@router.post("/api/keys")
@_guard
async def keys_create(request: Request):
    acct = _require_account(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    ip, agent = _client(request)
    key = accounts.create_key(acct["id"], body.get("name") or "New key", ip, agent)
    return {"ok": True, "key": key,
            "warning": "this is the only time the secret is shown."}


@router.delete("/api/keys/{key_id}")
@_guard
async def keys_revoke(request: Request, key_id: str):
    acct = _require_account(request)
    ip, agent = _client(request)
    if not accounts.revoke_key(acct["id"], key_id, ip, agent):
        return _fail("no such key on this account.", 404, "not_found")
    return {"ok": True}


# --------------------------------------------------------------------------
# monitors, alerts, security log — session-authenticated (the dashboard)
# --------------------------------------------------------------------------

@router.get("/api/monitors")
@_guard
async def monitors_list(request: Request):
    acct = _require_account(request)
    return {"ok": True, "monitors": accounts.list_monitors(acct["id"]),
            "interval_s": accounts.MONITOR_INTERVAL}


@router.post("/api/monitors")
@_guard
async def monitors_add(request: Request):
    acct = _require_account(request)
    body = await request.json()
    ip, agent = _client(request)
    monitor = accounts.add_monitor(acct["id"], body.get("package", ""),
                                   body.get("label", ""), ip, agent)
    _check_now(monitor)
    return {"ok": True, "monitor": accounts.get_monitor(monitor["id"])}


@router.delete("/api/monitors/{monitor_id}")
@_guard
async def monitors_remove(request: Request, monitor_id: str):
    acct = _require_account(request)
    ip, agent = _client(request)
    if not accounts.remove_monitor(acct["id"], monitor_id, ip, agent):
        return _fail("no such monitor on this account.", 404, "not_found")
    return {"ok": True}


@router.get("/api/alerts")
@_guard
async def alerts_list(request: Request, limit: int = Query(50, ge=1, le=200)):
    acct = _require_account(request)
    return {"ok": True, "alerts": accounts.list_alerts(acct["id"], limit)}


@router.post("/api/alerts/read")
@_guard
async def alerts_read(request: Request):
    acct = _require_account(request)
    return {"ok": True, "marked": accounts.mark_alerts_read(acct["id"])}


@router.get("/api/security-log")
@_guard
async def security_log(request: Request, limit: int = Query(80, ge=1, le=400)):
    acct = _require_account(request)
    return {"ok": True, "events": accounts.security_log(acct["id"], limit)}


@router.get("/api/account/events")
async def account_events(request: Request):
    """Server-sent events for one account: alerts and monitor observations as
    they happen, so the dashboard never polls."""
    acct = _account(request)
    if not acct:
        return _fail("sign in to subscribe.", 401, "not_signed_in")

    q = accounts.subscribe(acct["id"])

    def stream():
        try:
            yield f"data: {json.dumps({'type': 'hello', 'account': acct['email']})}\n\n"
            last = time.time()
            while True:
                try:
                    event = q.get(timeout=15)
                    yield f"data: {json.dumps(event)}\n\n"
                except Exception:
                    pass
                if time.time() - last > 15:
                    last = time.time()
                    yield ": keepalive\n\n"
        finally:
            accounts.unsubscribe(acct["id"], q)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


def _check_now(monitor: dict) -> None:
    """Give a new monitor its baseline immediately instead of making the owner
    wait for the next sweep."""
    import threading
    measure = HANDLERS.get("measure")
    if not measure:
        return
    threading.Thread(
        target=lambda: accounts.check_monitor(accounts.get_monitor(monitor["id"]), measure),
        daemon=True).start()


# --------------------------------------------------------------------------
# docs
# --------------------------------------------------------------------------

@router.get("/api/docs.json")
async def docs_json(request: Request):
    return apidocs.as_json(_origin(request))


@router.get("/api/docs.md")
async def docs_md(request: Request):
    return PlainTextResponse(apidocs.as_markdown(_origin(request)),
                             media_type="text/markdown; charset=utf-8")


@router.get("/api/docs.txt")
async def docs_txt(request: Request):
    return PlainTextResponse(apidocs.as_text(_origin(request)),
                             media_type="text/plain; charset=utf-8")


@router.get("/api/platform-stats")
async def platform_stats():
    return {"ok": True, **accounts.stats()}


# --------------------------------------------------------------------------
# v1 — the keyed public API
# --------------------------------------------------------------------------

def _v1(request: Request):
    return _require_key(request)


@router.get("/api/v1/whoami")
@_guard
async def v1_whoami(request: Request):
    key = _v1(request)
    acct = accounts.get_account(key["account_id"])
    return {"ok": True, "account": {"id": acct["id"], "email": acct["email"]},
            "key": {"id": key["id"], "name": key["name"], "prefix": key["prefix"]},
            "limits": {"rate_limited": False, "monthly_quota": None,
                       "note": "there are no usage limits on this API."}}


@router.get("/api/v1/blast")
@_guard
async def v1_blast(request: Request,
                   name: str = Query(..., min_length=1, max_length=214),
                   depth: int = Query(5, ge=1, le=5),
                   limit: int = Query(5000, ge=1, le=200_000)):
    _v1(request)
    return HANDLERS["blast"](name=name, depth=depth, limit=limit)


@router.get("/api/v1/resolve")
@_guard
async def v1_resolve(request: Request,
                     name: str = Query(..., min_length=1, max_length=214),
                     bad_version: str = Query(..., min_length=1, max_length=64)):
    _v1(request)
    return HANDLERS["resolve"](name=name, bad_version=bad_version)


@router.get("/api/v1/maintainers")
@_guard
async def v1_maintainers(request: Request,
                         name: str = Query(..., min_length=1, max_length=214),
                         limit: int = Query(200, ge=1, le=2000)):
    _v1(request)
    return HANDLERS["maintainers"](name=name, limit=limit)


@router.get("/api/v1/typosquats")
@_guard
async def v1_typosquats(request: Request,
                        name: str = Query(..., min_length=1, max_length=214)):
    _v1(request)
    return HANDLERS["typosquats"](name=name)


@router.get("/api/v1/subgraph")
@_guard
async def v1_subgraph(request: Request,
                      name: str = Query(..., min_length=1, max_length=214),
                      depth: int = Query(2, ge=1, le=5),
                      per_level: int = Query(28, ge=1, le=120),
                      max_nodes: int = Query(160, ge=2, le=600)):
    _v1(request)
    return HANDLERS["subgraph"](name=name, depth=depth, per_level=per_level,
                                max_nodes=max_nodes)


@router.post("/api/v1/lockfile")
@_guard
async def v1_lockfile(request: Request,
                      name: str = Query(..., min_length=1, max_length=214),
                      bad_version: str | None = Query(None, max_length=64),
                      depth: int = Query(5, ge=1, le=5)):
    _v1(request)
    return await HANDLERS["lockfile"](request=request, name=name,
                                      bad_version=bad_version, depth=depth)


@router.post("/api/v1/audit")
@_guard
async def v1_audit(request: Request,
                   max_detail: int = Query(60, ge=1, le=300)):
    _v1(request)
    return await HANDLERS["audit"](request=request, max_detail=max_detail,
                                   filename="")


@router.get("/api/v1/monitors")
@_guard
async def v1_monitors(request: Request):
    key = _v1(request)
    return {"ok": True, "monitors": accounts.list_monitors(key["account_id"])}


@router.post("/api/v1/monitors")
@_guard
async def v1_monitor_add(request: Request):
    key = _v1(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    ip, agent = _client(request)
    monitor = accounts.add_monitor(key["account_id"], body.get("package", ""),
                                   body.get("label", ""), ip, agent)
    _check_now(monitor)
    return {"ok": True, "monitor": monitor}


@router.delete("/api/v1/monitors/{monitor_id}")
@_guard
async def v1_monitor_remove(request: Request, monitor_id: str):
    key = _v1(request)
    ip, agent = _client(request)
    if not accounts.remove_monitor(key["account_id"], monitor_id, ip, agent):
        return _fail("no such monitor on this account.", 404, "not_found")
    return {"ok": True}


@router.get("/api/v1/alerts")
@_guard
async def v1_alerts(request: Request, limit: int = Query(50, ge=1, le=200)):
    key = _v1(request)
    return {"ok": True, "alerts": accounts.list_alerts(key["account_id"], limit)}


def mount(app, handlers: dict) -> None:
    HANDLERS.update(handlers)
    app.include_router(router)
