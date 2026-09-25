"""Which deployment a driver plans under — the first half of the scope.

``scope = (deployment_id, settings_hash)`` (design.md, "The deterministic
scope"). A driver resolves its deployment once, at the start of a build:

1. **Its own deployment.** A container of a Modal deployment has
   ``STARDAG_DEPLOYMENT_ID`` baked in by ``stardag modal deploy`` (the
   bootstrap, the ticks, the workers and the resident ``build`` function);
   it plans under that. Wins over everything else.
2. **The app's current deployment** (D13), for a driver that is not the
   deployment but whose tasks run on one: a hybrid ``sd.build()`` with a
   Modal executor, or ``reactive_discovery="local"``. The registry's
   current deployment for the app — the activated row with the highest
   generation — is read at start. That the driver's code matches it is on
   the user, stated in the docs.
3. **A local deployment**, for a pure local build: looked up or created by
   ``code_id`` with ``kind="local"``, born activated. ``code_id`` is
   ``STARDAG_CODE_ID`` if set, else the clean git HEAD SHA, else a fresh
   uuid per process (warned: a dirty tree shares its scope with nothing).
"""

from __future__ import annotations

import logging
import os
import subprocess
import uuid
from uuid import UUID

import uuid6

from stardag.exceptions import StardagError
from stardag.registry import RegistryABC

logger = logging.getLogger(__name__)

STARDAG_DEPLOYMENT_ID_ENV = "STARDAG_DEPLOYMENT_ID"
"""The deployment a container belongs to, baked into every function of a
Modal deployment by ``stardag modal deploy``. A worker reports it on its
yields (``/yield`` refuses one that is not the plan's), so the executor
never forwards it: it is the container's, not the build's."""

STARDAG_CODE_ID_ENV = "STARDAG_CODE_ID"
"""The explicit code-id pin of a local build (and the code id a deploy
records). Only feeds the local lookup; ``STARDAG_DEPLOYMENT_ID`` wins."""

_process_code_id: str | None = None


class DeploymentResolutionError(StardagError):
    """No deployment can host this build's plan (e.g. the Modal app its
    tasks run on has no activated deployment in the registry)."""


def _git(*args: str) -> str | None:
    try:
        return (
            subprocess.check_output(["git", *args], stderr=subprocess.DEVNULL)
            .strip()
            .decode("utf-8")
        )
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return None


def code_id() -> str:
    """This process's code identity: ``STARDAG_CODE_ID``, else the clean git
    HEAD SHA, else a fresh uuid for the process (warned). Stable for the
    process."""
    global _process_code_id
    env = os.environ.get(STARDAG_CODE_ID_ENV)
    if env is not None:
        if not env.strip():
            raise ValueError(f"{STARDAG_CODE_ID_ENV} is set but empty.")
        return env
    if _process_code_id is not None:
        return _process_code_id
    sha = _git("rev-parse", "HEAD")
    dirty = _git("status", "--porcelain")
    if sha and not dirty:
        _process_code_id = sha
    else:
        _process_code_id = uuid.uuid4().hex
        if sha:
            logger.warning(
                "The working tree has uncommitted changes, so this code has no "
                "identity: using a one-off code id %s. Builds started from it "
                "share their scope with nothing. Commit, or set %s, to get a "
                "stable one.",
                _process_code_id,
                STARDAG_CODE_ID_ENV,
            )
        else:
            logger.warning(
                "No git repository found and %s is not set: using a one-off "
                "code id %s. Set %s to give this code a stable identity.",
                STARDAG_CODE_ID_ENV,
                _process_code_id,
                STARDAG_CODE_ID_ENV,
            )
    return _process_code_id


def own_deployment_id() -> UUID | None:
    """The deployment this process belongs to (``STARDAG_DEPLOYMENT_ID``), if
    it runs inside one."""
    raw = os.environ.get(STARDAG_DEPLOYMENT_ID_ENV)
    if not raw:
        return None
    try:
        return UUID(raw)
    except ValueError as e:
        raise ValueError(f"{STARDAG_DEPLOYMENT_ID_ENV}={raw!r} is not a uuid") from e


async def current_app_deployment_id_aio(registry: RegistryABC, app_name: str) -> UUID:
    """The registry's current deployment of the Modal app ``app_name``.

    Raises:
        DeploymentResolutionError: The app has no activated deployment.
    """
    rows = await registry.deployment_list_aio(
        kind="modal", app_name=app_name, current=True
    )
    current = [r for r in rows if r.app_name == app_name]
    if not current:
        raise DeploymentResolutionError(
            f"The registry has no activated deployment of the Modal app "
            f"{app_name!r}, so a build whose tasks run on it has no scope to "
            "plan under. Deploy it with `stardag modal deploy`, which records "
            "the deployment (and fails if it cannot)."
        )
    return current[0].id


async def local_deployment_id_aio(registry: RegistryABC) -> UUID:
    """Look up or create the local deployment of this process's code id.

    The id is client-minted, as every deployment id is (design.md,
    ``deployment``); the lookup keys on the code id, so an existing row is
    returned with its own id and a retried create is idempotent."""
    row = await registry.deployment_create_aio(
        kind="local", code_id=code_id(), deployment_id=UUID(str(uuid6.uuid7()))
    )
    return row.id


async def resolve_deployment_id_aio(
    registry: RegistryABC, *, app_name: str | None = None
) -> UUID:
    """The deployment a driver plans under; see the module docstring."""
    own = own_deployment_id()
    if own is not None:
        return own
    if app_name is not None:
        return await current_app_deployment_id_aio(registry, app_name)
    return await local_deployment_id_aio(registry)
