"""Session setup for the registry-live tier.

**The deployment happens in ``pytest_sessionstart``, not in a fixture**, and
that ordering is the whole reason this file is shaped the way it is. Each
scenario module calls ``registry_live_guard()`` at import, so that a
misconfigured run is a collection error rather than a scenario quietly
executing against a NoOp registry. Module import happens during collection,
which happens *before* any fixture runs -- so a deployment created by a
fixture would not exist yet and every guard would fail. ``sessionstart``
runs before collection, which is exactly early enough.

One deployment per session, shared by every scenario. Deploying the registry
and the DAG app costs about a minute, and nothing here wants a fresh
database: the scenarios salt their own task ids, which is cheaper and a
better test besides, since each one then runs against a registry that
already holds other builds.

Deliberately a *separate* test root from ``tests/``: that directory's
conftest brings up docker-compose and Playwright, which this tier needs none
of, and ``testpaths`` in ``pyproject.toml`` still points only at ``tests``,
so a bare ``pytest`` in this project never reaches the live tier. Running it
is an explicit act, which is what keeps it from costing anyone Modal compute
by surprise.
"""

from __future__ import annotations

import os
import secrets
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from stardag_integration_tests.registry_live._guard import ENV_API_URL, is_enabled
from stardag_integration_tests.registry_live._harness import (
    Deployment,
    connect,
    deploy_registry,
)

ENV_MODAL_ENVIRONMENT = "MODAL_ENVIRONMENT"

_deployment: Deployment | None = None


def pytest_sessionstart(session: pytest.Session) -> None:
    """Deploy the registry and the DAG app, before anything is collected."""
    global _deployment
    if not is_enabled():
        return

    _isolate_sdk_home()
    modal_environment = _require_modal_environment()
    repo_root = _repo_root()
    admin_password = f"harness-{secrets.token_urlsafe(24)}"

    api_url = deploy_registry(
        repo_root,
        modal_environment=modal_environment,
        admin_password=admin_password,
    )
    _deployment = connect(
        api_url,
        admin_password=admin_password,
        execution_modal_env=modal_environment,
    )

    # Set before the scenario modules are imported: their guards read it,
    # and the SDK profile connect just wrote is what makes the registry
    # resolve to an APIRegistry pointed here.
    os.environ[ENV_API_URL] = api_url

    _deploy_dag_app(repo_root, modal_environment)


@pytest.fixture(scope="session")
def deployment() -> Deployment:
    """The live registry for this session.

    No teardown, on purpose. Everything the session creates lives inside the
    run's Modal environment, and deleting that environment removes the app,
    the volume, the secret and the database in one call -- which is the
    caller's job (the workflow's teardown step), because it must also run
    when the session died before any finaliser could.
    """
    if _deployment is None:
        pytest.fail(
            "No deployment: pytest_sessionstart did not complete. The error "
            "that stopped it is above this line."
        )
    return _deployment


def _repo_root() -> Path:
    """The stardag checkout, located by the package the image is built from."""
    here = Path(__file__).resolve()
    for candidate in [here, *here.parents]:
        if (candidate / "app" / "stardag-api" / "pyproject.toml").exists():
            return candidate
    raise RuntimeError(
        f"Could not locate the stardag repo root from {here} "
        "(looked for app/stardag-api/pyproject.toml)."
    )


def _require_modal_environment() -> str:
    """The run's own Modal environment. Required, never defaulted.

    The Modal workspace this runs in also holds real deployments, so falling
    back to the account's default environment is not a convenience -- it
    would deploy a registry, push a secret and create a volume alongside
    them.
    """
    name = os.environ.get(ENV_MODAL_ENVIRONMENT, "").strip()
    if not name:
        raise RuntimeError(
            f"{ENV_MODAL_ENVIRONMENT} must name a throwaway Modal environment "
            "for this run (CI uses ci-pr-<n> / ci-manual-<run-id>). This tier "
            "deploys a registry and pushes a secret; it will not do that into "
            "a default environment shared with real deployments."
        )
    return name


def _isolate_sdk_home() -> None:
    """Point ``HOME`` at a temporary directory for the rest of the session.

    The connect flow writes an SDK registry and profile under ``~/.stardag``.
    Without this, a local run rewrites the developer's own profile of that
    name to point at a deployment that is deleted minutes later, and their
    next ordinary command talks to nothing.

    Modal credentials are unaffected in CI, where they come from
    ``MODAL_TOKEN_ID`` / ``MODAL_TOKEN_SECRET`` and never from a file. A
    local run needs either those or ``MODAL_CONFIG_PATH``, so the real
    config path is captured here before ``HOME`` moves out from under it.
    """
    if "MODAL_CONFIG_PATH" not in os.environ:
        real_modal_config = Path.home() / ".modal.toml"
        if real_modal_config.exists():
            os.environ["MODAL_CONFIG_PATH"] = str(real_modal_config)

    os.environ["HOME"] = tempfile.mkdtemp(prefix="registry-live-home-")


def _deploy_dag_app(repo_root: Path, modal_environment: str) -> None:
    """Deploy the scenario DAG app with the CLI, under *this* interpreter.

    The CLI is taken from this interpreter's own environment rather than
    from ``PATH``: the app's Modal functions are serialized by whatever
    interpreter imports the module, and every later trigger unpickles them
    in a container built for that Python. Resolving the CLI next to
    ``sys.executable`` makes the deploy and the scenarios agree by
    construction rather than by coincidence -- a mismatch kills the
    container with a bare SIGSEGV and leaves a build that looks empty.
    """
    stardag_cli = Path(sys.executable).with_name("stardag")
    if not stardag_cli.exists():
        raise RuntimeError(
            f"No stardag CLI next to {sys.executable}. This tier deploys with "
            "the CLI from its own environment on purpose -- see the note "
            "above about interpreter agreement."
        )
    result = subprocess.run(
        [
            str(stardag_cli),
            "modal",
            "deploy",
            "-m",
            "stardag_integration_tests.registry_live.dag_app",
        ],
        cwd=repo_root / "integration-tests",
        capture_output=True,
        text=True,
        env={**os.environ, ENV_MODAL_ENVIRONMENT: modal_environment},
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Failed to deploy the scenario DAG app:\n{result.stdout}\n{result.stderr}"
        )
