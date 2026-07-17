"""Module-level Modal entry points for the *prebuilt* self-host server image.

The prebuilt server image already contains ``stardag_api`` (and the built
UI + alembic config), so shipping cloudpickled function bodies to it is
redundant. Instead ``build_server_app`` references the ``web`` / ``migrate``
functions below *by name* (``serialized=False``): Modal ships this single
file into the container and imports it there, then calls the referenced
function. Because nothing is cloudpickled, the CLI's own interpreter no
longer has to match the image's Python.

Two invariants make that work:

- **Import-safe on a stardag-SDK-only client.** ``build_server_app`` imports
  this module to build the (by-reference) app graph, and the client does not
  have ``stardag_api`` installed. So every ``stardag_api`` import stays
  inside a function body, evaluated only in-container.
- **Loaded as a standalone single-file module** (see
  ``_load_prebuilt_entry_module`` in ``_modal_app.py``): Modal then treats it
  as a file entrypoint, mounting just this file and importing it in the
  container by its stem — rather than trying to import it as a submodule of
  the ``stardag`` package, which the server image doesn't contain.

The container paths below mirror ``app/server.Dockerfile``'s layout contract
(kept in sync with ``_modal_app.py``); they are resolved in-image.
"""

from __future__ import annotations

API_REMOTE_DIR = "/opt/stardag/api"
UI_DIST_REMOTE_DIR = "/opt/stardag/ui"


def migrate() -> str:
    """Run ``alembic upgrade head`` inside the server container."""
    import subprocess

    result = subprocess.run(
        ["python", "-m", "alembic", "-c", "alembic.ini", "upgrade", "head"],
        cwd=API_REMOTE_DIR,
        capture_output=True,
        text=True,
    )
    output = (result.stdout or "") + (result.stderr or "")
    if result.returncode != 0:
        raise RuntimeError(f"Migration failed:\n{output}")
    return output


def web():
    """Construct the combined API + static-UI ASGI app (in-container)."""
    # Installed in the server image, not in the SDK/client environment.
    from stardag_api.server import create_app  # type: ignore[import-not-found] # pyright: ignore[reportMissingImports]

    return create_app(UI_DIST_REMOTE_DIR)
