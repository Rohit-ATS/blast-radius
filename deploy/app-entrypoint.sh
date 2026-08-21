#!/bin/sh
# One container: the graph node, the API, and the crawler.
#
# The graph is started here rather than run as its own Render service. That is
# not a shortcut around a limitation — it is what the data model allows. Every
# edge the crawler has written is in Postgres, so the graph is derived and can
# be rebuilt from the sidecar in seconds without touching npm. A service that
# can be rebuilt does not need a disk, and a service without a disk does not
# need to be a paid private service.
#
# Process layout, and why the shell supervises rather than an init system:
# there are exactly two long-lived processes and one strict dependency between
# them. Adding supervisord to express that would be more moving parts than the
# thing it manages.

set -eu

ROLE="${1:-web}"
PORT="${PORT:-8000}"
GRAPH_PORT="${GRAPH_PORT:-8443}"
GRAPH_ADMIN_PORT="${GRAPH_ADMIN_PORT:-9090}"
GRAPH_DIR="${GRAPH_DIR:-/data}"

log() { echo "[entrypoint] $*"; }

# --------------------------------------------------------------------------
# the graph
# --------------------------------------------------------------------------

start_graph() {
    [ "${RUN_GRAPH:-1}" = "1" ] || { log "RUN_GRAPH=0 — not starting the graph"; return 0; }

    # HydraDB does not read $PORT. It reads GRAPH_HTTP_ADDR — verified by
    # starting the upstream image with GRAPH_HTTP_ADDR=0.0.0.0:9999 and reading
    # back {"msg":"graph node listeners started","http_addr":"0.0.0.0:9999"}.
    #
    # It binds loopback only. Nothing outside this container has any business
    # reaching the graph directly, and on the previous split deployment its
    # query port was reachable by anything on the Render private network.
    export GRAPH_HTTP_ADDR="127.0.0.1:${GRAPH_PORT}"
    export GRAPH_ADMIN_ADDR="127.0.0.1:${GRAPH_ADMIN_PORT}"

    # A single-cell, single-node local cluster. These were spread across the
    # blueprint when the graph was its own service; they belong with the
    # process that needs them, so the container is startable on its own and a
    # missing entry cannot be introduced by editing YAML somewhere else.
    export CLOUD_PROVIDER="${CLOUD_PROVIDER:-local}"
    export LOCAL_PATH="${LOCAL_PATH:-${GRAPH_DIR}/store}"
    export GRAPH_DATA_CACHE_DIR="${GRAPH_DATA_CACHE_DIR:-${GRAPH_DIR}/cache}"
    export GRAPH_NAMESPACE="${GRAPH_NAMESPACE:-default}"
    export GRAPH_ID="${GRAPH_ID:-default}"
    export GRAPH_CELL_ID="${GRAPH_CELL_ID:-cell-0}"
    export GRAPH_CELLS="${GRAPH_CELLS:-cell-0}"
    export GRAPH_NODE_ID="${GRAPH_NODE_ID:-node-0}"
    export GRAPH_BOLT_NODE_ADDRESSES="${GRAPH_BOLT_NODE_ADDRESSES:-node-0=127.0.0.1:7687}"
    export GRAPH_ADVERTISED_BOLT_ADDR="${GRAPH_ADVERTISED_BOLT_ADDR:-127.0.0.1:7687}"
    export GRAPH_LOG_FORMAT="${GRAPH_LOG_FORMAT:-json}"

    # Sized for the instance it actually runs on.
    #
    # The defaults assume the graph owns the machine: a 1GiB object-store cache
    # (max_cache_size_bytes=1073741824 in its own startup log), 64MiB of
    # unflushed writes, 16MiB L0 files. Inside a 512MiB container sharing space
    # with Python, the bulk load walked 4 -> 206 -> 327 -> 425 -> 508MiB and the
    # cgroup killed graph-node outright. Nothing logged a crash, because nothing
    # crashed — the kernel simply took the largest process, and the site then
    # reported the graph unreachable while sitting right next to it.
    #
    # These are ~1/8th of the defaults and are what makes a 512MiB instance
    # viable. GRAPH_MEM_BUDGET_MB scales them for a larger one.
    _mb="${GRAPH_MEM_BUDGET_MB:-512}"
    export GRAPH_DATA_CACHE_BYTES="${GRAPH_DATA_CACHE_BYTES:-$((_mb * 262144))}"
    export GRAPH_MAX_GRAPHBLAS_BYTES="${GRAPH_MAX_GRAPHBLAS_BYTES:-$((_mb * 131072))}"
    export GRAPH_MAX_MATRIX_ADJACENCY_BYTES="${GRAPH_MAX_MATRIX_ADJACENCY_BYTES:-$((_mb * 131072))}"
    export GRAPH_MAX_UNFLUSHED_BYTES="${GRAPH_MAX_UNFLUSHED_BYTES:-16777216}"
    export GRAPH_L0_SST_SIZE_BYTES="${GRAPH_L0_SST_SIZE_BYTES:-8388608}"
    export GRAPH_L0_FLUSH_PARALLELISM="${GRAPH_L0_FLUSH_PARALLELISM:-1}"
    # Hydration builds the adjacency matrices; without this the peak stays
    # resident long after the traversal that needed it has answered.
    export GRAPH_TRIM_MEMORY_AFTER_HYDRATION="${GRAPH_TRIM_MEMORY_AFTER_HYDRATION:-true}"

    mkdir -p "${LOCAL_PATH}" "${GRAPH_DATA_CACHE_DIR}" 2>/dev/null || true

    # The graph refuses to start without TLS unless plaintext is opted into
    # explicitly, which is the right default for a service on a network. This
    # one is not on a network: it binds 127.0.0.1 inside this container, so the
    # only thing that can reach it is the app process sharing the namespace.
    # Terminating TLS on a loopback socket would encrypt a conversation that
    # never leaves the process boundary.
    export GRAPH_ALLOW_PLAINTEXT="${GRAPH_ALLOW_PLAINTEXT:-true}"

    # The token is read from a file, not from the environment.
    mkdir -p "${GRAPH_DIR}" 2>/dev/null || true
    TOKEN_FILE="${GRAPH_DIR}/auth-token"
    if [ -n "${HYDRA_TOKEN:-}" ]; then
        echo "${HYDRA_TOKEN}" > "${TOKEN_FILE}"
        chmod 600 "${TOKEN_FILE}" 2>/dev/null || true
        export GRAPH_AUTH_TOKEN_FILE="${TOKEN_FILE}"
    else
        log "FATAL: HYDRA_TOKEN is unset; the graph would start unauthenticated"
        exit 78
    fi
    # We are the graph, so the app talks to loopback — whatever the environment
    # says.
    #
    # This is not defensive tidiness. Render does not delete an environment
    # variable when it disappears from render.yaml; it keeps whatever was set
    # before. So a service that used to point at a separate graph service still
    # carries HYDRA_URL=http://hydradb-l2lg:10000 after the blueprint stops
    # mentioning it, that stale value overrides the image's own ENV, and the app
    # spends its life dialling a service that was deleted — reporting "the
    # dependency graph is unavailable" while the graph runs inside its own
    # container. Overriding here makes the deployment correct without anyone
    # having to remember to clean up the dashboard.
    if [ -n "${HYDRA_URL:-}" ] && [ "${HYDRA_URL}" != "http://127.0.0.1:${GRAPH_PORT}" ]; then
        log "ignoring inherited HYDRA_URL=${HYDRA_URL} — the graph is in this container"
    fi
    export HYDRA_URL="http://127.0.0.1:${GRAPH_PORT}"
    export HYDRA_ADMIN_URL="http://127.0.0.1:${GRAPH_ADMIN_PORT}"

    log "starting graph node on ${GRAPH_HTTP_ADDR} (admin ${GRAPH_ADMIN_ADDR})"
    /usr/local/bin/graph-node &
    GRAPH_PID=$!

    # Only covers the window before `exec gunicorn` below, which replaces this
    # shell and every trap it installed. After that point PID 1 is gunicorn and
    # the container's own teardown stops the graph. That is acceptable here
    # precisely because the store is a cache: an unclean stop costs a rebuild
    # at next boot, which is what boot does anyway.
    trap 'kill -TERM ${GRAPH_PID} 2>/dev/null || true' TERM INT
}


# Is a process alive, as opposed to merely still having a slot in the table?
#
# `kill -0` is the reflex here and it is wrong for this job. A dead child that
# nobody has waited on stays a zombie, and signalling a zombie succeeds — so
# `kill -0` reports the graph as healthy for as long as it goes unreaped, which
# is exactly the interval this watchdog exists to notice. /proc has no such
# ambiguity: State is Z once the process is gone, whoever its parent is.
alive() {
    [ -n "${1:-}" ] || return 1
    [ -e "/proc/$1/status" ] || return 1
    state="$(awk '/^State:/{print $2; exit}' "/proc/$1/status" 2>/dev/null || echo Z)"
    [ -n "${state}" ] && [ "${state}" != "Z" ]
}

watch_graph() {
    # The failure this exists for: the graph dies and nothing notices.
    #
    # graph-node is the largest process in the container, so it is the one the
    # cgroup OOM killer takes. It dies without logging anything — from the
    # database's point of view nothing went wrong, it simply ceased — and
    # gunicorn carries on serving. /api/health then answers 200, deliberately,
    # because a restart cannot fix a *remote* dependency and bouncing the
    # process on every hiccup is how one outage becomes a restart loop.
    #
    # But this dependency is not remote. It is a sibling process in this
    # container, and restarting the container genuinely does bring it back with
    # a freshly rebuilt graph. Without this watchdog the instance stays up,
    # healthy and permanently unable to answer a traversal, which is the state
    # the console reports as "the dependency graph is unavailable" — for hours,
    # because nothing is going to change it.
    #
    # Runs as a background child so gunicorn can keep PID 1 and its signal
    # handling. `kill -TERM 1` asks gunicorn to shut down gracefully; the
    # container then exits and the platform restarts it.
    while alive "${GRAPH_PID}"; do
        sleep "${GRAPH_WATCH_INTERVAL:-5}"
    done
    log "graph-node (pid ${GRAPH_PID}) is gone — most likely the cgroup OOM"
    log "killer, which leaves no message of its own. Stopping the container so"
    log "it restarts with a graph rather than serving without one."
    kill -TERM 1 2>/dev/null || true
}

wait_for_graph() {
    [ "${RUN_GRAPH:-1}" = "1" ] || return 0
    tries="${GRAPH_WAIT_TRIES:-45}"
    admin="${HYDRA_ADMIN_URL:-http://127.0.0.1:${GRAPH_ADMIN_PORT}}"
    i=0
    while [ "$i" -lt "$tries" ]; do
        # Readiness lives on the ADMIN port (/readyz), not the query port.
        # Probing /readyz on the query port returns 404 forever against a
        # perfectly healthy database — which is exactly how the previous
        # deployment concluded the graph was dead.
        code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "${admin}/readyz" 2>/dev/null || echo 000)"
        if [ "$code" -ge 200 ] 2>/dev/null && [ "$code" -lt 400 ] 2>/dev/null; then
            log "graph ready: ${admin}/readyz -> ${code}"
            return 0
        fi
        i=$((i + 1))
        [ $((i % 5)) -eq 0 ] && log "waiting for the graph (${i}/${tries}) last status: ${code}"
        sleep 2
    done
    log "graph did not become ready after ${tries} attempts; starting anyway so"
    log "the health endpoint can report the outage rather than the container hiding it"
}

rehydrate_graph() {
    [ "${RUN_GRAPH:-1}" = "1" ] || return 0
    [ "${REHYDRATE:-1}" = "1" ] || { log "REHYDRATE=0 — leaving the graph as it is"; return 0; }

    # The graph starts empty on every deploy, because it has no disk. Rebuild
    # it from the edges Postgres already holds. This is a replay of data we own,
    # not a recrawl: no npm traffic, and it is a no-op when the graph already
    # has edges, so a restart that kept its filesystem does not double them.
    log "rebuilding the graph from the sidecar (no npm traffic; see rehydrate.py)"
    # Not piped through sed for a prefix. A pipeline's exit status is the last
    # command's, so `python -m rehydrate | sed ... || log "failed"` reports
    # sed's success no matter how the rebuild went, and the failure branch can
    # never run. rehydrate.py already prefixes its own lines, and its traceback
    # is worth more on stderr unmangled than it is aligned.
    if python -m rehydrate; then
        log "graph rebuilt from the sidecar"
    else
        log "rehydrate failed (exit $?); the site stays up and the graph-free"
        log "features — lockfile audit, intel, remediation — are unaffected"
    fi
}

# --------------------------------------------------------------------------
# roles
# --------------------------------------------------------------------------

case "$ROLE" in
  web)
    start_graph
    wait_for_graph
    rehydrate_graph

    # Started after the rebuild, not before it: hydration is the memory peak
    # this watches for, and a watchdog racing the loader would restart the
    # container into another rebuild.
    if [ "${RUN_GRAPH:-1}" = "1" ] && [ -n "${GRAPH_PID:-}" ]; then
        # If the graph is already gone by here it died during the rebuild —
        # the peak is where it dies. Exit rather than exec gunicorn: an
        # instance that cannot answer a single traversal is worth restarting
        # now, while this is the deploy log's last line and obvious, rather
        # than after it has served without a graph for an afternoon.
        if ! alive "${GRAPH_PID}"; then
            log "FATAL: graph-node did not survive the rebuild. Lower"
            log "REHYDRATE_MAX_EDGES (currently ${REHYDRATE_MAX_EDGES:-unset}) or"
            log "give the instance more memory; see render.yaml."
            exit 1
        fi
        watch_graph &
        log "watching graph-node (pid ${GRAPH_PID})"
    fi

    # ONE worker by default. Each gunicorn worker is a full copy of the app —
    # interpreter, fixtures, Supabase client, connection pool — and two of them
    # exceeded a 512Mi instance and were OOM-killed in a loop. This work is
    # I/O-bound on the graph and on Postgres, which a single uvicorn worker
    # already overlaps through async; the second process bought concurrency
    # this workload was not short of, at twice the memory. Now that the graph
    # shares the instance, the headroom matters more than it did.
    #
    # The timeout is generous because a depth-5 traversal legitimately takes
    # seconds on a cold cache.
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
    # Kept for the split deployment and for local use. In the consolidated
    # single-service layout the crawl runs as threads inside the web process
    # (LIVE_INGEST=1) rather than as a second paid instance.
    wait_for_graph
    exec python worker.py
    ;;

  rehydrate)
    wait_for_graph
    exec python -m rehydrate "$@"
    ;;

  *)
    log "unknown role '${ROLE}' (expected: web | worker | rehydrate)"
    exit 64
    ;;
esac
