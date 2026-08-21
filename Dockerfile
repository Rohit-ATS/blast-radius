# Blast Radius — the graph, the API and the crawler in one image.
#
# Why one image and one service
# -----------------------------
# The graph used to be its own Render private service with its own disk. Both
# of those are paid-tier features, and the combination — a standard instance
# plus a 10GB disk — was most of the hosting bill for a project whose web
# service fits comfortably in the smallest instance there is. When the credit
# ran down, the graph went away and took every traversal with it.
#
# So the graph runs beside the app instead. That is sound here for a specific
# reason: the graph is *derived* data. Postgres holds every edge the crawler
# has ever written, so the graph node can live on an ephemeral filesystem and
# be rebuilt from the sidecar on boot — seconds, and no traffic to npm. See
# rehydrate.py. What would be reckless for a system of record is routine for a
# cache.
#
# Why FROM the HydraDB image rather than copying the binary out of it
# -------------------------------------------------------------------
# graph-node links libgraphblas.so.7 against Ubuntu 24.04's glibc. Dropping it
# onto python:3.12-slim (Debian bookworm, an older glibc) is the kind of thing
# that appears to work and then dies on an unrelated symbol at load time.
# Building on top of the upstream image keeps the binary in exactly the
# environment it was compiled for, and it happens to cost nothing: Ubuntu 24.04
# ships Python 3.12, the same version this project already targets.
#
# The graph binary is upstream's, unmodified. Only the process layout changed.

FROM ghcr.io/hydra-db/hydradb:latest

# The upstream image drops to uid 10001. Installing packages and seeding the
# data directory both need root; the entrypoint drops privileges itself.
USER root

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DEBIAN_FRONTEND=noninteractive \
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH

# python3-venv rather than pip --break-system-packages: Ubuntu 24.04 marks its
# Python externally managed (PEP 668), and overriding that puts this project's
# dependencies in the same site-packages as the system's own tooling.
# curl is for the healthcheck and the readiness probe; ca-certificates for
# outbound TLS to npm, osv.dev and Supabase.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
         python3 python3-venv ca-certificates curl \
    && python3 -m venv /opt/venv \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# --- dependency layer, cached across source changes ------------------------
COPY requirements.txt ./
# Playwright and pytest are development-only. Installing them here would add
# ~200MB and a browser download to every deploy for code that never runs in
# production.
RUN grep -viE '^(playwright|pytest)' requirements.txt > /tmp/prod-requirements.txt \
    && pip install --no-cache-dir -r /tmp/prod-requirements.txt \
    && pip install --no-cache-dir "gunicorn>=22.0" "uvicorn[standard]>=0.29"

# --- source layer ----------------------------------------------------------
COPY *.py ./
COPY ecosystems/ ./ecosystems/
COPY web/ ./web/
COPY fixtures/ ./fixtures/
# The crawler needs a starting set. Small text files; they belong in the image
# so a fresh instance has something to crawl before anyone visits the site.
COPY seeds.txt seeds_expanded.txt ./
COPY deploy/app-entrypoint.sh /usr/local/bin/app-entrypoint.sh
# Strip CR before chmod. A checkout on Windows can hand these over with CRLF
# endings, and Linux then reads the trailing return as part of the interpreter
# path — the container dies with
#     exec /usr/local/bin/...: no such file or directory
# naming a file that is plainly there. .gitattributes pins LF in the repo; this
# makes the image correct regardless of how the file arrived.
RUN sed -i 's/\r$//' /usr/local/bin/app-entrypoint.sh \
    && chmod +x /usr/local/bin/app-entrypoint.sh

# The graph node writes here, and runs as 10001.
RUN mkdir -p /data /app/state && chown -R 10001:10001 /data /app/state /app

# $HOME is /home/graph in the upstream image and the directory is not in it.
# Nothing needed it until gunicorn's control server did, which then failed at
# every boot with
#     [ERROR] Control server error: [Errno 13] Permission denied: '/home/graph'
# — harmless in itself, and exactly the kind of standing ERROR that teaches
# whoever reads these logs to skim past the line that eventually matters.
RUN mkdir -p /home/graph && chown 10001:10001 /home/graph

# A cold node exceeds its own query timeout on deep traversals without this.
ENV RUST_MIN_STACK=33554432

# Where the graph listens *inside* this container. Not published: nothing
# outside needs to reach it now that the app is in the same network namespace,
# which also means the query port is no longer an exposed surface at all.
ENV HYDRA_URL=http://127.0.0.1:8443 \
    HYDRA_ADMIN_URL=http://127.0.0.1:9090

USER 10001

# Render assigns $PORT; this default is only for `docker run` locally.
ENV PORT=8000
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=180s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT}/api/health" || exit 1

ENTRYPOINT ["/usr/local/bin/app-entrypoint.sh"]
CMD ["web"]
