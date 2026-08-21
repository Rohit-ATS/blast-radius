# Blast Radius — the app and the worker.
#
# One image serves both Render services; the command decides which. They share
# every dependency and all of the query code, so building twice would only mean
# two chances for them to drift apart.
#
# Kept lean deliberately: Render bills build minutes, and requirements.txt
# changes far less often than the source does, so it is copied and installed on
# its own layer. A source-only change then reuses the dependency layer instead
# of recompiling the world.

FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# curl is here for the container healthcheck; ca-certificates for outbound TLS
# to the npm registry and osv.dev. Nothing else — no compiler, no git.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

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
# so a fresh worker has something to crawl before anyone visits the site.
COPY seeds.txt seeds_expanded.txt ./
COPY deploy/app-entrypoint.sh /usr/local/bin/app-entrypoint.sh
# Strip CR before chmod. A checkout on Windows can hand these over with
# CRLF endings, and Linux then reads the trailing  as part of the
# interpreter path — the container dies with
#     exec /usr/local/bin/...: no such file or directory
# naming a file that is plainly there. .gitattributes pins LF in the repo;
# this makes the image correct regardless of how the file arrived.
RUN sed -i 's/$//' /usr/local/bin/app-entrypoint.sh && chmod +x /usr/local/bin/app-entrypoint.sh

# Nothing in here needs root at runtime.
RUN useradd --create-home --uid 10001 blast \
    && mkdir -p /app/state \
    && chown -R blast:blast /app
USER blast

# Render assigns $PORT; this default is only for `docker run` locally.
ENV PORT=8000
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=90s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT}/api/health" || exit 1

ENTRYPOINT ["/usr/local/bin/app-entrypoint.sh"]
CMD ["web"]
