"""Serve the API reference without asking someone else's CDN for permission.

FastAPI's built-in `/api/docs` pulls Swagger UI from cdn.jsdelivr.net. That is
fine on a laptop and unreliable everywhere it matters:

  * Brave, uBlock and most corporate proxies block third-party CDN requests as
    a matter of policy, so the page loads, returns 200, and renders blank — the
    worst kind of broken, because the server looks healthy and the user sees
    nothing.
  * It is the one dependency that can fail while a demo is being recorded, on
    someone else's infrastructure, with no way to fix it in the moment.

The console already refuses CDN script tags for exactly this reason — the
force-directed graph is hand-rolled rather than imported. The API reference was
the one page still breaking that rule, so the assets are vendored into
web/vendor/ and served from the same origin as everything else.

`install(app)` replaces the generated route rather than asking FastAPI not to
create one, so the path, the OpenAPI URL and the title stay wherever the app
defined them and this module cannot drift out of step with that choice.
"""

from __future__ import annotations

import os

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

HERE = os.path.dirname(os.path.abspath(__file__))
VENDOR = os.path.join(HERE, "web", "vendor")

CSS = "/vendor/swagger-ui.css"
JS = "/vendor/swagger-ui-bundle.js"

PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<link rel="stylesheet" href="{css}">
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><circle cx='16' cy='16' r='15' fill='none' stroke='%232f6bff' stroke-width='1' opacity='.28'/><circle cx='16' cy='16' r='10.5' fill='none' stroke='%232f6bff' stroke-width='1.5' opacity='.55'/><circle cx='16' cy='16' r='5' fill='%23d63a2f'/></svg>">
<style>
  body {{ margin: 0; }}
  .topbar {{ display: none; }}
  #offline {{
    display: none; margin: 40px auto; max-width: 640px; padding: 20px 22px;
    border: 1px solid #f6d3cf; background: #fdeeed; border-radius: 14px;
    font: 14px/1.6 ui-monospace, SFMono-Regular, Menlo, monospace; color: #d63a2f;
  }}
</style>
</head>
<body>
<div id="swagger-ui"></div>
<div id="offline">
  The API reference could not start. The spec is still available as raw JSON at
  <a href="{openapi}">{openapi}</a>.
</div>
<script src="{js}"></script>
<script>
  // Everything above is same-origin, so there is nothing left for a shield or a
  // proxy to block. If the bundle still failed to parse, say so and point at the
  // spec rather than leaving an empty page that looks like a server fault.
  if (typeof SwaggerUIBundle === 'undefined') {{
    document.getElementById('offline').style.display = 'block';
  }} else {{
    SwaggerUIBundle({{
      url: '{openapi}',
      dom_id: '#swagger-ui',
      deepLinking: true,
      displayRequestDuration: true,
      tryItOutEnabled: true,
      presets: [SwaggerUIBundle.presets.apis],
      layout: 'BaseLayout',
    }});
  }}
</script>
</body>
</html>
"""


def assets_present() -> bool:
    """Both files vendored and non-empty. A truncated download would render a
    blank page just as effectively as a blocked CDN."""
    for name in ("swagger-ui.css", "swagger-ui-bundle.js"):
        path = os.path.join(VENDOR, name)
        if not os.path.exists(path) or os.path.getsize(path) < 10_000:
            return False
    return True


def install(app: FastAPI) -> bool:
    """Point `/api/docs` at the vendored assets. Returns whether it took.

    If the vendored files are missing this leaves FastAPI's CDN-backed page
    alone: a page that needs the network beats no page at all, and the caller
    can log which one it got.
    """
    docs_url = app.docs_url
    if not docs_url or not assets_present():
        return False

    openapi_url = app.openapi_url or "/openapi.json"
    title = f"{app.title} — API reference"

    # Drop the generated route and register ours at the same path, so the app's
    # own docs_url stays the single source of truth for where this lives.
    app.router.routes = [r for r in app.router.routes
                         if getattr(r, "path", None) != docs_url]

    @app.get(docs_url, include_in_schema=False)
    def api_docs() -> HTMLResponse:
        return HTMLResponse(PAGE.format(
            title=title, css=CSS, js=JS, openapi=openapi_url))

    return True
