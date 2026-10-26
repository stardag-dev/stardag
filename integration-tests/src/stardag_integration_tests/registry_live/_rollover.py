"""Deploying the rollover app, as a scenario needs it deployed.

The rollover scenarios (S3, S6, S7, S20, S33, S37, S40) each redeploy an app of
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
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from .rollover_app import DEPLOY_NONCE_ENV, DEPLOY_PROBE_FUNCTION, ROLLOVER_APP_NAME_ENV
from .tasks import FIELD_VARIANT_ENV, ROOT_VARIANT_ENV

APP_MODULE = "stardag_integration_tests.registry_live.rollover_app"

# How long a deploy may take to start serving before the deploy fails (see
# ``wait_until_the_deploy_serves``). Since every probe is a fresh container,
# the wait is one cold start: in CI the deploys that already met no warm
# container (each app's first) answered in 4.7 s median, 10.7 s p95 and
# 14.2 s at most (119 deploys, 17 tier runs, 2026-10-19..26). The bound is
# four times that tail. It also has to leave room inside the shortest gate a
# rollover scenario holds its D1 task open with -- S7's 110 s pre-yield,
# which runs from the task starting and so covers the deploy command too:
# once the gate's bound passes, the D1 task stops waiting, and a scenario
# carrying on would no longer overlap the rollover it exists to test.
DEPLOY_SERVING_TIMEOUT_SECONDS = 60
# Only to report a deploy that was late (see ``wait_until_the_deploy_serves``).
DEPLOY_SERVING_REPORT_GRACE_SECONDS = 60
PROBE_INTERVAL_SECONDS = 2

# Every app name a rollover scenario deploys, so the log dump can collect
# them. A scenario names its app from here and nowhere else.
ROLLOVER_APP_NAMES = {
    "S3": "registry-live-rollover",
    "S6": "registry-live-s6-redeploy",
    "S7": "registry-live-s7-late-yield",
    "S20": "registry-live-s20-root-identity",
    "S33": "registry-live-s33-seal-race",
    "S37": "registry-live-s37-late-record",
    "S40": "registry-live-s40-added-field",
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
    field_variant: str = "",
    activate: bool = True,
) -> None:
    """Deploy the rollover app as ``app_name`` with code identity ``code_id``.

    ``activate=False`` deploys the code and records the deployment, but
    never activates it (S37).

    Returns only once a fresh call reaches the new code
    (``wait_until_the_deploy_serves``).
    """
    nonce = uuid.uuid4().hex
    env = {
        **os.environ,
        "MODAL_ENVIRONMENT": modal_environment,
        "STARDAG_CODE_ID": code_id,
        ROLLOVER_APP_NAME_ENV: app_name,
        DEPLOY_NONCE_ENV: nonce,
    }
    for name, value in (
        (ROOT_VARIANT_ENV, root_variant),
        (FIELD_VARIANT_ENV, field_variant),
    ):
        env.pop(name, None)
        if value:
            env[name] = value
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
    # Also for ``activate=False``: that skips only the registry's activation
    # of the deployment record, and the Modal deploy itself goes live, so
    # the new code is what the next spawn reaches either way (S37).
    serving = wait_until_the_deploy_serves(app_name, modal_environment, nonce)
    assert serving.within_bound, (
        f"The deploy of {app_name!r} as {code_id[:12]} returned, but no fresh "
        f"call reached it within {DEPLOY_SERVING_TIMEOUT_SECONDS}s "
        f"({serving.describe()}). Carrying on would run the scenario against "
        "the previous deployment, so it would no longer test a rollover."
    )


@dataclass
class DeployServing:
    """How the wait for a deploy to serve ended (``wait_until_the_deploy_serves``)."""

    bound: float
    # Seconds from the deploy returning to the first probe the new deploy
    # answered; None if none did, even within the report grace.
    answered_after: float | None = None
    probes: int = 0
    # Probes answered by another deploy, and probes that raised.
    other_deploy: int = 0
    faults: int = 0
    last_answer: str | None = None

    @property
    def within_bound(self) -> bool:
        return self.answered_after is not None and self.answered_after <= self.bound

    def describe(self) -> str:
        if self.answered_after is None:
            outcome = "it never answered"
        else:
            outcome = f"it answered after {self.answered_after:.1f}s"
        return (
            f"{outcome}; {self.probes} probe(s): {self.other_deploy} answered by "
            f"another deploy, {self.faults} raised, last answer "
            f"{self.last_answer!r}"
        )


def wait_until_the_deploy_serves(
    app_name: str,
    modal_environment: str,
    nonce: str,
    *,
    timeout: float = DEPLOY_SERVING_TIMEOUT_SECONDS,
    report_grace: float = DEPLOY_SERVING_REPORT_GRACE_SECONDS,
) -> DeployServing:
    """Wait until a fresh call to the app reaches the deploy tagged ``nonce``.

    **``modal deploy`` returning is not the new code serving.** Measured in
    CI: a tick spawned seconds after the deploy command returned started a
    container of the *previous* version -- in S33, of a deployment already
    superseded. The old clock-sized windows (a minute or more past every
    deploy) hid that; a gate released the moment the deploy returns does
    not. So the deploy waits for the state instead: ``deploy_probe``
    answers with its deploy's nonce, and a call answering with this one is a
    fresh container reaching the new code.

    **Every probe is a fresh container** (``max_inputs=1`` on
    ``deploy_probe``), and that is load-bearing. Modal routes a call to a
    warm container of the previous deploy after a redeploy, and a short
    ``scaledown_window`` does not bound how long one stays warm: an idle
    container measured outliving its 2 s window by 12 s, and a probe every
    few seconds resets the idle clock. So a probe reusing containers kept
    the previous deploy's probe container alive *by probing it*, and read
    the deploy as not serving until Modal happened to reap it. In CI that
    was every slow wait: 255 deploys over 17 runs split into 82% answered
    within 15 s (cold starts) and a tail out to 169 s plus five 180 s
    timeouts, with no new probe container started at all during a slow
    wait -- the probe container of the app's previous deploy answered
    throughout. It hit the second deploy of each app, the only one with a
    warm predecessor, and lengthened with the tier's load. Reproduced on an
    idle workspace with no in-flight work: the same old container answered
    every 5 s probe for 46 s; with ``max_inputs=1`` the first probe after
    the deploy answered with the new nonce, in 4-5 s (a cold start), also
    with long calls still running on the old deploy.

    Bounded: at ``timeout`` the deploy fails, since a scenario that carried
    on would be testing the previous deployment. The probing continues for
    ``report_grace`` more seconds only to say whether, and when, the deploy
    did answer, so a late deploy reads differently from one that never
    serves.
    """
    import modal

    function = modal.Function.from_name(
        app_name, DEPLOY_PROBE_FUNCTION, environment_name=modal_environment
    )
    serving = DeployServing(bound=timeout)
    started = time.monotonic()
    while time.monotonic() - started < timeout + report_grace:
        serving.probes += 1
        try:
            answered = function.remote()
        except Exception as error:  # a fault is "not yet"
            answered = f"<{type(error).__name__}>"
            serving.faults += 1
        serving.last_answer = answered
        if answered == nonce:
            serving.answered_after = time.monotonic() - started
            break
        if not answered.startswith("<"):
            serving.other_deploy += 1
        time.sleep(PROBE_INTERVAL_SECONDS)
    if serving.within_bound:
        print(
            f"[harness] {app_name}: the new deploy serves after "
            f"{serving.answered_after:.1f}s ({serving.probes} probe(s))",
            file=sys.stderr,
        )
    else:
        print(
            f"[harness] {app_name}: the new deploy did not serve within "
            f"{timeout:.0f}s of returning; {serving.describe()}",
            file=sys.stderr,
        )
    return serving


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
