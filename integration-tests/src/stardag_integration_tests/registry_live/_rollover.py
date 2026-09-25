"""Deploying the rollover app, as a scenario needs it deployed.

The rollover scenarios (S3, S6, S7, S20, S33, S37) each redeploy an app of
their own mid-run; see ``rollover_app`` for why each needs its own name.
Everything here goes through ``stardag modal deploy`` from *this* venv, for
the reason ``provision._deploy_dag_apps`` gives: the app's functions are
serialised by the interpreter that imports the module, and every trigger
unpickles them in a container built for that Python.

``STARDAG_CODE_ID`` in the deploy's environment is the code identity the
deployment records (the same override a CI image uses). A deployment's *id*
is minted per deploy, so two deploys of one code id are two deployments --
S6's premise.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from uuid import UUID

from .rollover_app import ROLLOVER_APP_NAME_ENV
from .tasks import ROOT_VARIANT_ENV

APP_MODULE = "stardag_integration_tests.registry_live.rollover_app"

# Every app name a rollover scenario deploys, so the log dump can collect
# them. A scenario names its app from here and nowhere else.
ROLLOVER_APP_NAMES = {
    "S3": "registry-live-rollover",
    "S6": "registry-live-s6-redeploy",
    "S7": "registry-live-s7-late-yield",
    "S20": "registry-live-s20-root-identity",
    "S33": "registry-live-s33-seal-race",
    "S37": "registry-live-s37-late-record",
}

# Run in the deploy subprocess instead of the CLI's entry point when a
# scenario needs the deploy to succeed and its activation never to land:
# the operator who ignored the non-zero exit (design.md, "Rollover"). The
# CLI's own activation step is replaced, nothing else.
_DEPLOY_WITHOUT_ACTIVATION = """
import sys
import stardag._cli.modal as modal_cli
modal_cli._activate_deployment = lambda *args, **kwargs: None
from stardag._cli import app
sys.argv = ["stardag", "modal", "deploy", "-m", sys.argv[1]]
app()
"""


def deploy_rollover_app(
    app_name: str,
    modal_environment: str,
    *,
    code_id: str,
    root_variant: str = "",
    activate: bool = True,
) -> None:
    """Deploy the rollover app as ``app_name`` with code identity ``code_id``.

    ``activate=False`` deploys the code and records the deployment, but
    never activates it (S37).
    """
    env = {
        **os.environ,
        "MODAL_ENVIRONMENT": modal_environment,
        "STARDAG_CODE_ID": code_id,
        ROLLOVER_APP_NAME_ENV: app_name,
    }
    env.pop(ROOT_VARIANT_ENV, None)
    if root_variant:
        env[ROOT_VARIANT_ENV] = root_variant
    if activate:
        stardag_cli = Path(sys.executable).with_name("stardag")
        assert stardag_cli.exists(), f"No stardag CLI next to {sys.executable}"
        command = [str(stardag_cli), "modal", "deploy", "-m", APP_MODULE]
    else:
        command = [sys.executable, "-c", _DEPLOY_WITHOUT_ACTIVATION, APP_MODULE]
    result = subprocess.run(
        command,
        cwd=Path(__file__).resolve().parents[3],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, (
        f"Deploy of {app_name!r} as {code_id[:12]} failed:\n"
        f"{result.stdout}\n{result.stderr}"
    )


def trigger_app(app_name: str):
    """A local handle on a deployed rollover app, for ``build_trigger``.

    Constructed rather than imported: ``rollover_app.app`` is named by the
    environment at import, and a test process importing it would get
    whichever name it happened to be started with.
    """
    from ._scenario_app import build_scenario_app

    return build_scenario_app(app_name)


def app_deployments(app_name: str) -> list:
    """The app's recorded Modal deployments, newest generation first."""
    from stardag.registry import registry_provider

    rows = registry_provider.get().deployment_list(kind="modal", app_name=app_name)
    return sorted(
        (d for d in rows if d.app_name == app_name),
        key=lambda d: d.generation,
        reverse=True,
    )


def deployment_of(app_name: str, code_id: str) -> UUID:
    """The id of the (only) deployment of ``app_name`` recorded for ``code_id``."""
    matches = [d.id for d in app_deployments(app_name) if d.code_id == code_id]
    assert len(matches) == 1, (app_name, code_id, app_deployments(app_name))
    return matches[0]
