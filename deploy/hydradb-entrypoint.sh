#!/bin/sh
# HydraDB entrypoint for Render.
#
# Two things stand between the stock image and a Render private service, and
# both are solved here rather than in the platform config, because neither can
# be expressed as an environment variable alone.
#
#   1. THE PORT. Render assigns $PORT and expects the process to listen on it.
#      HydraDB does not read $PORT — it reads GRAPH_HTTP_ADDR. Verified against
#      ghcr.io/hydra-db/hydradb:latest by starting it with
#      GRAPH_HTTP_ADDR=0.0.0.0:9999 and reading back
#      `"message":"graph node listeners started","http_addr":"0.0.0.0:9999"`.
#      So this translates one into the other.
#
#   2. THE AUTH TOKEN. HydraDB authenticates against a *file*, not an env var
#      (GRAPH_AUTH_TOKEN_FILE). A secret belongs in an environment variable, so
#      this writes the env var into the file on the persistent disk at boot.
#
#   3. OWNERSHIP. A freshly attached disk is mounted root-owned, and the image
#      runs as uid 10001 (`graph`). The stock image therefore dies with
#      `Os { code: 13, kind: PermissionDenied }` on first boot against a new
#      volume. This seeds and chowns before dropping to that user.
#
# Everything here is idempotent: a redeploy against an existing disk leaves the
# store untouched, which is the whole point of the disk.

set -eu

DATA_DIR="${HYDRA_DATA_DIR:-/data}"
RUN_UID=10001
RUN_GID=10001

log() { echo "[entrypoint] $*"; }

# ---------------------------------------------------------------- the port
# Render sets $PORT. Locally there is none, so the image's own default stands.
if [ -n "${PORT:-}" ]; then
    export GRAPH_HTTP_ADDR="0.0.0.0:${PORT}"
    log "PORT=${PORT} -> GRAPH_HTTP_ADDR=${GRAPH_HTTP_ADDR}"
else
    export GRAPH_HTTP_ADDR="${GRAPH_HTTP_ADDR:-0.0.0.0:8443}"
    log "no \$PORT; GRAPH_HTTP_ADDR=${GRAPH_HTTP_ADDR}"
fi

# Say the whole address out loud. This translation was correct and still cost an
# outage, because Render defaults $PORT to 10000 and the peers were configured
# to dial 8443: nothing logged the two numbers next to each other, so both sides
# looked right in isolation. Printing the URL peers must use turns that into a
# one-line diff instead of a packet capture.
log "peers must use HYDRA_URL=http://<this-service-name>:${GRAPH_HTTP_ADDR##*:}"

# --------------------------------------------------------------- the layout
mkdir -p "${DATA_DIR}/store" "${DATA_DIR}/cache"

# ---------------------------------------------------------------- the token
TOKEN_FILE="${GRAPH_AUTH_TOKEN_FILE:-${DATA_DIR}/auth-token}"
if [ -n "${GRAPH_AUTH_TOKEN:-}" ]; then
    # Written every boot so rotating the Render env var actually rotates the
    # token, rather than being silently ignored because the file already exists.
    printf '%s\n' "${GRAPH_AUTH_TOKEN}" > "${TOKEN_FILE}"
    chmod 600 "${TOKEN_FILE}"
    log "auth token written to ${TOKEN_FILE} from GRAPH_AUTH_TOKEN"
elif [ -f "${TOKEN_FILE}" ]; then
    log "GRAPH_AUTH_TOKEN unset; using the token already on the disk"
else
    log "FATAL: no GRAPH_AUTH_TOKEN and no ${TOKEN_FILE}."
    log "       Generate one with: openssl rand -hex 32"
    exit 1
fi
export GRAPH_AUTH_TOKEN_FILE="${TOKEN_FILE}"

# ------------------------------------------------------------- ownership
# Only meaningful when we start as root. If the platform already runs us as the
# graph user, the chown is skipped and the mkdir above will have failed loudly.
if [ "$(id -u)" = "0" ]; then
    chown -R "${RUN_UID}:${RUN_GID}" "${DATA_DIR}"
    log "chowned ${DATA_DIR} to ${RUN_UID}:${RUN_GID}"
    log "starting graph-node"
    # su-exec is not in this image; setpriv is part of util-linux and is.
    if command -v setpriv >/dev/null 2>&1; then
        exec setpriv --reuid="${RUN_UID}" --regid="${RUN_GID}" --clear-groups \
            /usr/local/bin/graph-node "$@"
    fi
    log "setpriv unavailable; continuing as root"
fi

log "starting graph-node"
exec /usr/local/bin/graph-node "$@"
