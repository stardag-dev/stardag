"""Combined server: Registry API + static web UI in a single ASGI app.

This module is the single source of truth for serving the API and the
built web UI from one process/origin (no CORS, no UI API-URL config):

- ``mount_ui(app, ui_dist_dir)`` mounts the UI dist directory with an
  SPA fallback (unknown paths serve ``index.html`` so client-side routes
  work on hard reloads). Mounts are matched *after* routes, so
  ``/api/v1/*``, ``/health`` and ``/.well-known/jwks.json`` keep working.
- ``create_app(ui_dist_dir)`` returns the API app with the UI mounted
  when the directory exists (API-only otherwise).
- ``app`` is a module-level ASGI app configured from the
  ``STARDAG_UI_DIST`` environment variable, for use as an ASGI target:
  ``uvicorn stardag_api.server:app``.
"""

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

UI_MOUNT_NAME = "ui"


class SPAStaticFiles(StaticFiles):
    """Static files with SPA fallback.

    Unknown paths serve ``index.html`` so client-side routes
    (e.g. ``/builds/<id>``) work on hard reloads. API-shaped paths
    (``api/``, ``health*``, ``.well-known/``) are excluded so unknown
    API endpoints return a proper 404 instead of 200 with HTML.
    (Inside the mount, ``path`` has no leading slash.)
    """

    async def get_response(self, path: str, scope):
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as e:
            if e.status_code == 404 and not path.startswith(
                ("api/", "health", ".well-known/")
            ):
                return await super().get_response("index.html", scope)
            raise


def mount_ui(app: FastAPI, ui_dist_dir: str | Path) -> None:
    """Mount the built web UI (SPA) at ``/`` on ``app``. Idempotent.

    Mounts are matched after routes, so all API routes keep working;
    everything else serves the UI.
    """
    already_mounted = any(
        getattr(route, "name", None) == UI_MOUNT_NAME for route in app.router.routes
    )
    if already_mounted:
        return
    app.mount(
        "/",
        SPAStaticFiles(directory=str(ui_dist_dir), html=True),
        name=UI_MOUNT_NAME,
    )


def create_app(ui_dist_dir: str | None = None) -> FastAPI:
    """Return the combined ASGI app: Registry API + web UI (when available).

    When ``ui_dist_dir`` is unset or does not exist the API app is
    returned as-is (API only).
    """
    from stardag_api.main import app as api_app

    if ui_dist_dir and Path(ui_dist_dir).is_dir():
        mount_ui(api_app, ui_dist_dir)
    return api_app


app = create_app(os.environ.get("STARDAG_UI_DIST"))
