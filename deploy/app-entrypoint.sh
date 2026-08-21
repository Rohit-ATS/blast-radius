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
    i=0
    while [ "$i" -lt "$tries" ]; do
        # Any HTTP status means the listener is up, which is the only question
        # being asked here. An unauthenticated probe of the query port is
        # *supposed* to be refused — demanding a 2xx (curl -f) made a correctly
        # secured graph look like a dead one, and the app waited out its whole
        # timeout beside a perfectly healthy database.
        code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "${HYDRA_URL}/" 2>/dev/null || echo 000)"
        if [ "$code" != "000" ]; then
            log "graph listening at ${HYDRA_URL} (HTTP ${code})"
            return 0
        fi
        i=$((i + 1))
        log "waiting for ${HYDRA_URL} (${i}/${tries})"
        sleep 4
    done
    log "graph still unreachable after ${tries} attempts; starting anyway so the"
    log "health endpoint can report the outage rather than the container hiding it"
}

case "$ROLE" in
  web)
    wait_for_graph
    # Two workers, not four: the box is small and each one holds its own
    # HydraDB connection pool and SQLite handles. The timeout is generous
    # because a depth-5 traversal over this graph legitimately takes seconds.
    exec gunicorn server:app \
        --worker-class uvicorn.workers.UvicornWorker \
        --workers "${WEB_CONCURRENCY:-2}" \
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
