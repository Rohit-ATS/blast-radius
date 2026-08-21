#!/bin/sh
# One image, two roles. `web` serves; `worker` crawls and sweeps.
#
# Splitting these is not cosmetic. Render puts web services to sleep on
# inactivity and does not sleep background workers, so the crawler and the
# 24/7 monitor sweeps have to live outside the web process or they stop the
# moment nobody is looking at the site — which is exactly when a supply-chain
# alert matters most.

set -eu

ROLE="${1:-web}"
PORT="${PORT:-8000}"

log() { echo "[entrypoint] $*"; }

# The graph is a separate service and may still be starting when we are. Wait
# for it rather than crash-looping: Render counts restarts, and a worker that
# dies three times in its first minute gets held down.
wait_for_graph() {
    [ -n "${HYDRA_URL:-}" ] || return 0
    tries="${GRAPH_WAIT_TRIES:-30}"
    admin="${HYDRA_ADMIN_URL:-}"
    i=0

    log "graph query API : ${HYDRA_URL}"
    log "graph admin API : ${admin:-<unset — falling back to a query-port probe>}"

    while [ "$i" -lt "$tries" ]; do
        # Readiness lives on the ADMIN port (/readyz), not the query port. They
        # are different ports and on Render different numbers entirely: the
        # query API takes $PORT (10000) while admin stays 9090. Probing /readyz
        # on the query port returns 404 forever against a healthy database.
        if [ -n "$admin" ]; then
            code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "${admin}/readyz" 2>/dev/null || echo 000)"
            if [ "$code" -ge 200 ] 2>/dev/null && [ "$code" -lt 400 ] 2>/dev/null; then
                log "graph ready: ${admin}/readyz -> ${code}"
                return 0
            fi
        else
            # No admin URL configured. Fall back to asking whether anything at
            # all answers on the query port — any status proves a listener,
            # since an unauthenticated probe is *supposed* to be refused.
            code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "${HYDRA_URL}/" 2>/dev/null || echo 000)"
            if [ "$code" != "000" ]; then
                log "graph listening at ${HYDRA_URL} (HTTP ${code})"
                return 0
            fi
        fi

        i=$((i + 1))
        log "waiting for the graph (${i}/${tries}) last status: ${code}"
        sleep 4
    done
    log "graph still unreachable after ${tries} attempts; starting anyway so the"
    log "health endpoint can report the outage rather than the container hiding it"
}

case "$ROLE" in
  web)
    wait_for_graph
    # ONE worker by default. Each gunicorn worker is a full copy of the app —
    # interpreter, fixtures, Supabase client, connection pool — and two of them
    # exceeded a 512Mi instance and were OOM-killed in a loop. This work is
    # I/O-bound on the graph and on Postgres, which a single uvicorn worker
    # already overlaps through async; the second process bought concurrency
    # this workload was not short of, at twice the memory. The timeout is
    # generous because a depth-5 traversal legitimately takes seconds.
    exec gunicorn server:app \
        --worker-class uvicorn.workers.UvicornWorker \
        --workers "${WEB_CONCURRENCY:-1}" \
        --bind "0.0.0.0:${PORT}" \
        --timeout "${WEB_TIMEOUT:-120}" \
        --graceful-timeout 30 \
        --keep-alive 15 \
        --access-logfile - \
        --error-logfile - \
        --log-level "${LOG_LEVEL:-info}"
    ;;

  worker)
    wait_for_graph
    exec python worker.py
    ;;

  *)
    log "unknown role '${ROLE}' (expected: web | worker)"
    exit 64
    ;;
esac
