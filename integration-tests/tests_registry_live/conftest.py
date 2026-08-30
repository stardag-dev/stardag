"""Session setup for the registry-live tier.

One deployment per session, shared by every scenario: deploying the registry
and the DAG app costs about a minute, and nothing in the scenarios wants a
fresh database -- they salt their own task ids instead, which is both
cheaper and a better test, since it means each scenario runs against a
registry that already holds other builds.

Deliberately a *separate* test root from ``tests/``. That directory's
conftest brings up docker-compose and Playwright, which this tier needs none
of, and ``testpaths`` in ``pyproject.toml`` still points only at ``tests``
-- so a bare ``pytest`` in this project never reaches the live tier. Running
it is an explicit act, which is what keeps it from costing anyone Modal
compute by surprise.
"""

from __future__ import annotations

import os
import secrets
import subprocess
import sys
from pathlib import Path

import pytest

from stardag_integration_tests.registry_live._harness import (
    Deployment,
    connect,
    deploy_registry,
)

ENV_MODAL_ENVIRONMENT = "MODAL_ENVIRONMENT"


def _repo_root() -> Path:
    """The stardag checkout, located by the two packages the image needs."""
    for candidate in [Path(__file__).resolve(), *Path(__file__).resolve().parents]:
        if (candidate / "app" / "stardag-api" / "pyproject.toml").exists():
            return candidate
    raise RuntimeError(
        "Could not locate the stardag repo root from "
        f"{Path(__file__).resolve()} (looked for app/stardag-api)."
    )


@pytest.fixture(scope="session")
def modal_environment() -> str:
    """The run's own Modal environment. Required, never defaulted.

    The workspace this runs in also holds real deployments, so falling back
    to the account's default environment is not a convenience -- it would
    deploy a registry, push a secret and create a volume next to them.
    """
    name = os.environ.get(ENV_MODAL_ENVIRONMENT, "").strip()
    if not name:
        pytest.fail(
            f"{ENV_MODAL_ENVIRONMENT} must name a throwaway Modal environment "
            "for this run (CI uses ci-pr-<n> / ci-manual-<run-id>). This tier "
            "deploys a registry and pushes a secret; it will not do that into "
            "a default environment shared with real deployments."
        )
    return name


@pytest.fixture(scope="session", autouse=True)
def isolated_sdk_home(tmp_path_factory) -> Path:
    """Point ``HOME`` at a temporary directory for the whole session.

    The connect flow writes an SDK registry and profile under ``~/.stardag``.
    Without this, running the tier locally rewrites the developer's own
    profile of that name to point at a deployment that is deleted minutes
    later -- and the next ordinary command then talks to nothing.

    Modal credentials are unaffected in CI, where they come from
    ``MODAL_TOKEN_ID`` / ``MODAL_TOKEN_SECRET`` and never from a file. For a
    local run, export those, or set ``MODAL_CONFIG_PATH`` to the real
    ``~/.modal.toml`` before starting -- which is why it is captured here
    before ``HOME`` moves.
    """
    if "MODAL_CONFIG_PATH" not in os.environ:
        real_modal_config = Path.home() / ".modal.toml"
        if real_modal_config.exists():
            os.environ["MODAL_CONFIG_PATH"] = str(real_modal_config)

    home = tmp_path_factory.mktemp("sdk-home")
    os.environ["HOME"] = str(home)
    return home


@pytest.fixture(scope="session")
def deployment(modal_environment: str, isolated_sdk_home: Path) -> Deployment:
    """Deploy the registry, wire the SDK to it, deploy the DAG app.

    No teardown here on purpose. Everything this creates lives inside
    ``modal_environment``, and deleting that environment removes the app,
    the volume, the secret and the database in one call -- which is the
    caller's job (the workflow's teardown step), because it must also run
    when the whole session died before reaching a fixture finaliser.
    """
    repo_root = _repo_root()
    admin_password = f"harness-{secrets.token_urlsafe(24)}"

    api_url = deploy_registry(
        repo_root,
        modal_environment=modal_environment,
        admin_password=admin_password,
    )
    live = connect(
        api_url,
        admin_password=admin_password,
        execution_modal_env=modal_environment,
    )

    # Only now: the guard in each scenario module reads this, and the
    # profile written by connect is what makes the SDK resolve an
    # APIRegistry pointed here.
    os.environ["STARDAG_REGISTRY_LIVE_API_URL"] = api_url

    _deploy_dag_app(repo_root, modal_environment)
    return live


def _deploy_dag_app(repo_root: Path, modal_environment: str) -> None:
    """Deploy the scenario DAG app with the CLI, in this interpreter.

    ``sys.executable -m stardag`` rather than a bare ``stardag``: the app's
    Modal functions are serialized by whatever interpreter imports the
    module, and every later trigger unpickles them in a container built for
    *that* Python. Running the deploy under the interpreter that will also
    run the scenarios is what makes the two agree by construction rather
    than by coincidence.
    """
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "stardag",
            "modal",
            "deploy",
            "-m",
            "stardag_integration_tests.registry_live.dag_app",
        ],
        cwd=repo_root / "integration-tests",
        capture_output=True,
        text=True,
        env={**os.environ, "MODAL_ENVIRONMENT": modal_environment},
    )
    if result.returncode != 0:
        pytest.fail(
            f"Failed to deploy the scenario DAG app:\n{result.stdout}\n{result.stderr}"
        )
