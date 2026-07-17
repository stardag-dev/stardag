"""Modal app definition for self-hosting the Stardag service.

Builds a single Modal app that serves the Registry API and the web UI from
one ASGI endpoint (same-origin, so no CORS or UI API-URL configuration),
plus a one-off ``migrate`` function that applies Alembic migrations.

Two image modes:

- **Prebuilt** (default): the released server image
  ``ghcr.io/stardag-dev/stardag-server:<version>`` is pulled from the
  public GitHub Container Registry — no repo checkout needed.
- **From source**: the image is built from a local checkout of the
  stardag repo: the UI is compiled with npm *inside* the image build
  (no local Node required) and the API package is pip-installed from
  source.

Both modes place the alembic config at ``/opt/stardag/api`` and the built
UI at ``/opt/stardag/ui`` (see ``app/server.Dockerfile``), so the Modal
function bodies are mode-agnostic.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import modal

API_REMOTE_DIR = "/opt/stardag/api"
UI_SRC_REMOTE_DIR = "/opt/stardag/ui-src"
UI_DIST_REMOTE_DIR = "/opt/stardag/ui"

# The Modal app name (and URL label). Kept short: the deployed URL is
# https://<workspace>-<modal-env>--<app>.modal.run, so with the default
# server Modal environment below that reads ...-stardag-host--server....
DEFAULT_APP_NAME = "server"
DEFAULT_CONFIG_SECRET = "server-config"
DEFAULT_JWT_SECRET = "server-jwt"

# Modal environment the server app and its secrets live in, created on
# demand. Keeping the server/control-plane in its own Modal environment
# isolates it from the environments where the user's DAG apps run (no name
# collisions with user apps/secrets/volumes, and `modal app list` etc. stay
# uncluttered).
DEFAULT_SERVER_MODAL_ENV = "stardag-host"

SERVER_IMAGE_REPO = "ghcr.io/stardag-dev/stardag-server"
# The server release this SDK version is tested against. Bumped at SDK
# release time; override per deployment with --server-version.
DEFAULT_SERVER_VERSION = "0.1.0"

# Python (major, minor) baked into the prebuilt server image - MUST match
# the base image in app/server.Dockerfile. The `migrate`/`web` functions are
# serialized (cloudpickled) by the *client* interpreter and unpickled inside
# the image, so Modal requires the two versions to match exactly; the CLI
# fails fast with a remedy when they don't. Bumping the Dockerfile's Python
# version must be coordinated with this constant (see DEV_README.md,
# "Releasing the Server").
PREBUILT_IMAGE_PYTHON = (3, 12)

# Minimum client interpreter for from-source image builds (stardag-api's
# requires-python; the image gets the client's version via add_python).
MIN_IMAGE_PYTHON = (3, 10)


def server_image_ref(version: str) -> str:
    """Full image reference for a released server version (or "latest")."""
    return f"{SERVER_IMAGE_REPO}:{version}"


def client_python_version() -> str:
    """The running interpreter's "major.minor", validated for image use.

    Used as the from-source image's Python so it matches the client that
    serializes the Modal function bodies (a mismatch fails at deploy time
    with Modal's ``InvalidError``). Raises RuntimeError when the client
    interpreter is older than stardag-api supports.
    """
    version = tuple(sys.version_info[:2])
    if version < MIN_IMAGE_PYTHON:
        raise RuntimeError(
            "The stardag server requires Python >= {}.{}".format(*MIN_IMAGE_PYTHON)
            + ", but this CLI is running under {}.{}".format(*version)
            + ". Re-run under a newer interpreter, e.g.: "
            'uvx --python 3.12 --from "stardag[selfhost]" stardag self-host up'
        )
    return "{}.{}".format(*version)


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
    repo_root: Path | None = None,
    app_name: str = DEFAULT_APP_NAME,
    config_secret_name: str | None = None,
    jwt_secret_name: str | None = None,
    keep_warm: int = 0,
    python_version: str | None = None,
    server_version: str = DEFAULT_SERVER_VERSION,
    environment_name: str | None = None,
) -> tuple["modal.App", dict[str, Any]]:
    """Build the Modal app serving the Stardag service.

    When ``repo_root`` is None (default) the prebuilt public server image
    ``ghcr.io/stardag-dev/stardag-server:<server_version>`` is used.
    When ``repo_root`` is given the image is built from that checkout
    (``server_version`` is ignored). ``python_version`` applies to the
    from-source image only and defaults to the running interpreter's
    version: the function bodies are serialized by *this* process, and
    Modal requires the image's Python to match the serializing one. (The
    prebuilt image is fixed at ``PREBUILT_IMAGE_PYTHON``; the CLI checks
    the client interpreter against it before deploying.)

    ``environment_name`` is the Modal environment the referenced secrets
    live in (the app itself is placed there by the caller at deploy time);
    None means Modal's default environment.

    Secret names default to "<app_name>-config" and "<app_name>-jwt".
    Returns (app, {"web": web_fn, "migrate": migrate_fn}).
    """
    import modal

    config_secret_name = config_secret_name or f"{app_name}-config"
    jwt_secret_name = jwt_secret_name or f"{app_name}-jwt"

    if repo_root is None:
        # Prebuilt release image (public registry, no secret needed). The
        # image ships python (PREBUILT_IMAGE_PYTHON) + the API package +
        # built UI + alembic config at the same paths as the from-source
        # build below.
        image = modal.Image.from_registry(server_image_ref(server_version))
    else:
        api_dir = repo_root / "app" / "stardag-api"
        ui_dir = repo_root / "app" / "stardag-ui"

        image = (
            # node:22-slim for the in-image UI build; add_python for the
            # API - matching the client interpreter (see docstring)
            modal.Image.from_registry(
                "node:22-slim",
                add_python=python_version or client_python_version(),
            )
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
    # The secrets must be resolved in the same Modal environment the app is
    # deployed to (from_name lookups are environment-scoped).
    secrets = [
        modal.Secret.from_name(config_secret_name, environment_name=environment_name),
        modal.Secret.from_name(jwt_secret_name, environment_name=environment_name),
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
        # Installed in the container image, not in the SDK environment
        from stardag_api.server import create_app  # type: ignore[import-not-found] # pyright: ignore[reportMissingImports]

        return create_app(ui_dist_remote_dir)

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
