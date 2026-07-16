"""Modal app definition for self-hosting the Stardag service.

Builds a single Modal app that serves the Registry API and the web UI from
one ASGI endpoint (same-origin, so no CORS or UI API-URL configuration),
plus a one-off ``migrate`` function that applies Alembic migrations.

The container image is built from a local checkout of the stardag repo:
the UI is compiled with npm *inside* the image build (no local Node
required) and the API package is pip-installed from source.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import modal

API_REMOTE_DIR = "/opt/stardag/api"
UI_SRC_REMOTE_DIR = "/opt/stardag/ui-src"
UI_DIST_REMOTE_DIR = "/opt/stardag/ui"

DEFAULT_APP_NAME = "stardag-server"
DEFAULT_CONFIG_SECRET = "stardag-server-config"
DEFAULT_JWT_SECRET = "stardag-server-jwt"


def find_repo_root(start: Path | None = None) -> Path | None:
    """Locate the stardag repo root by walking up from ``start`` (or cwd).

    The root is identified by the presence of both the API and UI packages.
    """
    current = (start or Path.cwd()).resolve()
    for candidate in [current, *current.parents]:
        if (candidate / "app" / "stardag-api" / "pyproject.toml").exists() and (
            candidate / "app" / "stardag-ui" / "package.json"
        ).exists():
            return candidate
    return None


def build_server_app(
    repo_root: Path,
    app_name: str = DEFAULT_APP_NAME,
    config_secret_name: str | None = None,
    jwt_secret_name: str | None = None,
    keep_warm: int = 0,
    python_version: str = "3.12",
) -> tuple["modal.App", dict[str, Any]]:
    """Build the Modal app serving the Stardag service from a repo checkout.

    Secret names default to "<app_name>-config" and "<app_name>-jwt".
    Returns (app, {"web": web_fn, "migrate": migrate_fn}).
    """
    import modal

    config_secret_name = config_secret_name or f"{app_name}-config"
    jwt_secret_name = jwt_secret_name or f"{app_name}-jwt"

    api_dir = repo_root / "app" / "stardag-api"
    ui_dir = repo_root / "app" / "stardag-ui"

    image = (
        # node:22-slim for the in-image UI build; add_python for the API
        modal.Image.from_registry("node:22-slim", add_python=python_version)
        .add_local_dir(
            ui_dir.as_posix(),
            UI_SRC_REMOTE_DIR,
            copy=True,
            ignore=["node_modules", "dist", ".env", ".env.*"],
        )
        .run_commands(
            f"cd {UI_SRC_REMOTE_DIR} && npm ci && npm run build"
            f" && mv dist {UI_DIST_REMOTE_DIR}",
        )
        .add_local_dir(
            api_dir.as_posix(),
            API_REMOTE_DIR,
            copy=True,
            ignore=[".venv", "__pycache__", ".pytest_cache", "tests"],
        )
        .run_commands(f"python -m pip install {API_REMOTE_DIR}")
    )

    app = modal.App(app_name)
    secrets = [
        modal.Secret.from_name(config_secret_name),
        modal.Secret.from_name(jwt_secret_name),
    ]

    # NOTE: the function bodies below are defined as closures (not module-
    # level functions) on purpose: serialized=True pickles closures BY VALUE,
    # so the container doesn't need the stardag package installed. They must
    # only reference in-container paths and imports.
    api_remote_dir = API_REMOTE_DIR
    ui_dist_remote_dir = UI_DIST_REMOTE_DIR

    def _migrate_impl() -> str:
        """Run alembic upgrade head inside the container."""
        import subprocess

        result = subprocess.run(
            ["python", "-m", "alembic", "-c", "alembic.ini", "upgrade", "head"],
            cwd=api_remote_dir,
            capture_output=True,
            text=True,
        )
        output = (result.stdout or "") + (result.stderr or "")
        if result.returncode != 0:
            raise RuntimeError(f"Migration failed:\n{output}")
        return output

    def _web_impl():
        """Construct the combined API + static-UI ASGI app."""
        from fastapi.staticfiles import StaticFiles
        from starlette.exceptions import HTTPException as StarletteHTTPException

        # Installed in the container image, not in the SDK environment
        from stardag_api.main import app as api_app  # type: ignore[import-not-found] # pyright: ignore[reportMissingImports]

        class SPAStaticFiles(StaticFiles):
            # SPA fallback: unknown paths serve index.html so client-side
            # routes (e.g. /builds/<id>) work on hard reloads.
            async def get_response(self, path: str, scope):
                try:
                    return await super().get_response(path, scope)
                except StarletteHTTPException as e:
                    if e.status_code == 404:
                        return await super().get_response("index.html", scope)
                    raise

        # Mounts are matched after routes, so /api/v1/*, /health and
        # /.well-known/jwks.json keep working; everything else serves the UI.
        api_app.mount(
            "/", SPAStaticFiles(directory=ui_dist_remote_dir, html=True), name="ui"
        )
        return api_app

    migrate = app.function(
        image=image,
        secrets=secrets,
        serialized=True,
        timeout=600,
        name="migrate",
    )(_migrate_impl)

    web_wrapped = modal.asgi_app(label=app_name)(_web_impl)
    web = app.function(
        image=image,
        secrets=secrets,
        serialized=True,
        min_containers=keep_warm or None,
        scaledown_window=300,
        timeout=300,
        name="web",
    )(modal.concurrent(max_inputs=100)(web_wrapped))

    return app, {"web": web, "migrate": migrate}
