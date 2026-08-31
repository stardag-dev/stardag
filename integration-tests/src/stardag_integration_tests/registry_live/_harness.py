"""Bring a throwaway registry up, wire the SDK to it, and read it back.

The whole lifecycle of one run lives here so the scenarios can be about
scheduling rather than about setup. Three steps:

1. **Deploy** the registry app (:mod:`._registry_app`) into the run's own
   Modal environment.
2. **Connect** -- the self-host CLI's own post-deploy flow, reused rather
   than reimplemented. It logs in, resolves the workspace and environment,
   creates the default target root, mints an API key and pushes it as the
   ``stardag-api-key`` Modal secret *into the execution environment*, and
   writes a local SDK registry + profile. Everything it creates is inside
   the run's Modal environment, so it all goes away with that environment.
3. **Observe** -- the boot-id read that tells a scenario whether the
   container holding the database is still the one it started with.

Teardown is not here, because there is nothing to tear down piecemeal:
``modal environment delete`` takes the app, the volumes, the secrets and
the database with it in one call. That is the reason the database lives
inside the container in the first place.
"""

from __future__ import annotations

import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from ._registry_app import (
    build_registry_app,
    generate_jwt_keypair,
    registry_config,
)

# The bootstrap admin. A throwaway credential for a deployment that lives
# minutes and is reachable only by whoever holds the run's Modal token; it
# is generated per run rather than fixed so that two runs never share one.
ADMIN_EMAIL = "harness@stardag.invalid"

DEFAULT_WORKSPACE_NAME = "registry-live"
DEFAULT_ENVIRONMENT_SLUG = "main"


@dataclass(frozen=True)
class Deployment:
    """A live registry and the coordinates needed to talk to it."""

    api_url: str
    modal_environment: str
    workspace_slug: str
    environment_slug: str
    workspace_id: str
    environment_id: str
    # The harness's own key, so the scenarios authenticate exactly the way
    # the workers do. See `_mint_api_key`.
    api_key: str
    boot_id: str

    def current_boot_id(self) -> str:
        return read_boot_id(self.api_url)

    def assert_same_container(self) -> None:
        """Fail loudly if the process holding the database was replaced.

        Worth calling at the end of any scenario that spans minutes. A
        recycled container does not lose *rows*, it loses the entire
        database, and the symptom -- tasks that have silently reverted to
        unregistered, a build that cannot find its own plan -- is a very
        convincing impression of a stardag bug. One assertion converts that
        into a sentence.
        """
        current = self.current_boot_id()
        if current != self.boot_id:
            raise AssertionError(
                f"The registry container was replaced mid-run (boot id "
                f"{self.boot_id} -> {current}). Its Postgres is inside that "
                f"container, so the database this scenario was writing to no "
                f"longer exists. This is a harness failure, not a scheduling "
                f"one: raise scaledown_window, or move PGDATA onto a Modal "
                f"Volume."
            )


def deploy_registry(
    repo_root: Path,
    *,
    modal_environment: str,
    admin_password: str,
    app_name: str = "registry",
    workspace_name: str = DEFAULT_WORKSPACE_NAME,
    environment_slug: str = DEFAULT_ENVIRONMENT_SLUG,
    health_timeout: float = 300.0,
) -> str:
    """Deploy the registry into ``modal_environment``; return its URL.

    ``modal_environment`` is required and never defaulted. The workspace
    this runs in also holds real deployments, so an unset Modal environment
    is not a convenience -- it is a deploy into whatever ``main`` happens to
    be.
    """
    import modal

    if not modal_environment:
        raise ValueError(
            "modal_environment is required: this tier deploys into a "
            "throwaway per-run environment, and the workspace it runs in "
            "also holds live deployments."
        )

    # Before anything else: retire whatever is already deployed under this
    # name. Redeploying does not retire the previous deployment's warm
    # containers, and here that is not the usual mild version of the
    # problem. Two things live in that container -- the branch's code and
    # the entire database -- so a survivor serves the *previous* push's API
    # against the previous push's data, complete with the bootstrap admin
    # password from that run, which no longer matches the one generated
    # here. The visible symptom is a baffling "Invalid email or password";
    # the invisible one, if the passwords ever did match, is a green run
    # that tested the previous commit.
    #
    # CI reuses `ci-pr-<n>` across pushes to the same PR, so this is the
    # normal case there, not an edge.
    stop_existing_app(app_name, modal_environment)

    private_key, public_key = generate_jwt_keypair()
    app, _functions = build_registry_app(
        repo_root,
        app_name,
        config=registry_config(
            admin_email=ADMIN_EMAIL,
            admin_password=admin_password,
            workspace_name=workspace_name,
            environment_slug=environment_slug,
            jwt_private_key=private_key,
            jwt_public_key=public_key,
        ),
    )

    with modal.enable_output():
        app.deploy(environment_name=modal_environment)

    url = modal.Function.from_name(
        app_name, "web", environment_name=modal_environment
    ).get_web_url()
    if not url:
        raise RuntimeError(
            f"Deployed {app_name!r} into Modal environment "
            f"{modal_environment!r} but it reports no web URL."
        )

    wait_for_health(url, timeout=health_timeout)
    return url


def _mint_api_key(
    api_url: str, session_token: str, *, workspace_name: str, environment_slug: str
) -> tuple[str, str, str]:
    """Mint an API key for the harness. Returns (key, workspace_id, env_id).

    A second key alongside the one connect pushed to Modal, and deliberately
    so. Connect's key goes into a Modal secret and is never handed back in
    plaintext -- correct for a credential meant for containers, useless for
    a caller that needs to authenticate here.

    The alternative was to let the SDK authenticate from the profile connect
    wrote, which uses a browser-login JWT resolved through a token cache
    keyed on (registry, workspace, user) and refreshed on expiry. That works
    for a person at a terminal and is the wrong shape for a harness: it adds
    a cache, a clock and a refresh path between a scenario and its registry,
    each able to fail in a way that reads as a scheduling bug. An API key
    has none of those parts -- and it is the credential the workers use, so
    the two halves of every scenario now authenticate identically.
    """
    with httpx.Client(timeout=60.0, base_url=f"{api_url.rstrip('/')}/api/v1") as client:
        session_headers = {"authorization": f"Bearer {session_token}"}

        me = client.get("/ui/me", headers=session_headers)
        me.raise_for_status()
        workspaces = me.json().get("workspaces", [])
        workspace = _pick(
            workspaces, workspace_name, what=f"workspace {workspace_name!r}"
        )
        workspace_id = workspace["id"]

        exchanged = client.post(
            "/auth/exchange",
            headers=session_headers,
            json={"workspace_id": workspace_id},
        )
        exchanged.raise_for_status()
        access_headers = {"authorization": f"Bearer {exchanged.json()['access_token']}"}

        environments = client.get(
            f"/ui/workspaces/{workspace_id}/environments", headers=access_headers
        )
        environments.raise_for_status()
        environment = _pick(
            environments.json(),
            environment_slug,
            what=f"environment {environment_slug!r}",
        )
        environment_id = environment["id"]

        created = client.post(
            f"/ui/workspaces/{workspace_id}/environments/{environment_id}/api-keys",
            headers=access_headers,
            json={"name": "registry-live-harness"},
        )
        created.raise_for_status()
        key = created.json()["key"]

        # Use it once, here, against the environment it was minted for.
        # A key that does not work is a harness failure, and it is worth
        # discovering at the line that created it rather than three layers
        # later where it reads as a scheduling problem.
        check = client.get(
            "/builds",
            headers={"X-API-Key": key},
            params={"environment_id": environment_id, "limit": 1},
        )
        if check.status_code != 200:
            raise RuntimeError(
                f"The API key just minted for workspace {workspace_id} / "
                f"environment {environment_id} does not authenticate: "
                f"{check.status_code} {check.text[:200]!r} "
                f"(key prefix {key[:12]!r})"
            )
        return key, workspace_id, environment_id


def _pick(items: list[dict], wanted: str, *, what: str) -> dict:
    """The item whose slug or name is ``wanted``; the only one if unique."""
    for item in items:
        if wanted in (item.get("slug"), item.get("name")):
            return item
    if len(items) == 1:
        return items[0]
    raise RuntimeError(
        f"Could not find {what} in {[i.get('slug') or i.get('name') for i in items]}"
    )


def modal_cli() -> str:
    """The ``modal`` executable from *this* interpreter's environment.

    Not a bare ``modal`` off ``PATH``. A developer machine can easily have
    an older Modal on the path -- a pyenv shim, a pipx install -- and the
    CLI's options move between versions. When that happens the failure is
    not a clean "command not found" but an argument parse error from a
    different program than the one intended, which is easy to mistake for
    the operation simply having nothing to do.

    Same reasoning as resolving the ``stardag`` CLI next to
    ``sys.executable`` in ``provision``: the tool that runs should be the
    one this environment installed.
    """
    candidate = Path(sys.executable).with_name("modal")
    if candidate.exists():
        return str(candidate)
    # A packaged environment may put it elsewhere; PATH is the fallback,
    # not the default.
    return "modal"


def stop_existing_app(app_name: str, modal_environment: str) -> None:
    """Stop an app of this name in this environment, if one is deployed.

    Both apps this tier deploys need this, for two different reasons that
    happen to have the same fix.

    The registry, because its container *is* the database: a survivor
    serves the previous run's data and code (see the caller).

    The DAG app, because its containers hold the ``stardag-api-key`` Modal
    secret as it was when they started, and the connect flow **rotates**
    that key -- it revokes the old one. A warm worker or tick container
    from an earlier run therefore authenticates with a revoked credential
    and gets a 401 from the registry mid-build. Observed exactly that: a
    build whose tick lost its wake-up to an unauthorised request and sat
    RUNNING until the scenario timed out.

    Stopping also keeps a reused environment from accumulating always-on
    containers. ``min_containers=1`` keeps a container warm *even when the
    function is idle*, and ``scaledown_window`` governs only containers
    above that minimum -- so an abandoned deployment does not wind down on
    its own at all. It runs until someone stops the app or deletes the
    environment.
    """
    result = subprocess.run(
        [modal_cli(), "app", "stop", app_name, "-e", modal_environment, "--yes"],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        print(f"[harness] stopped the previous {app_name!r} deployment")
        return

    output = ((result.stderr or "") + (result.stdout or "")).strip()
    if _looks_like_no_such_app(output):
        print(f"[harness] no previous {app_name!r} to stop")
        return

    # Anything else is a real failure and must not be swallowed. Treating
    # every non-zero exit as "nothing to stop" hid an argument-parse error
    # from a stale `modal` on PATH for a whole afternoon, and the effect
    # was that nothing was ever stopped -- which is precisely the bug this
    # function exists to prevent, silently reintroduced.
    raise RuntimeError(
        f"Could not stop the existing {app_name!r} deployment in Modal "
        f"environment {modal_environment!r}: {output[:500]}"
    )


def _looks_like_no_such_app(output: str) -> bool:
    """Whether Modal's complaint means "there is nothing deployed here".

    Matched on text because the CLI exits 1 for both this and real errors.
    Deliberately narrow: an unrecognised message is treated as a failure,
    so a future wording change makes the tier noisy rather than silently
    ineffective.
    """
    lowered = output.lower()
    return any(
        phrase in lowered
        for phrase in (
            # Modal's actual wording, as of 1.5: "No App with name 'x'
            # found in the 'y' environment." Note it does not contain the
            # substring "not found", which an earlier version of this list
            # assumed and which cost a CI run to discover -- on the very
            # first run against a fresh environment, where there is by
            # definition nothing to stop.
            "no app with name",
            "could not find",
            "not found",
            "no such app",
            "no apps",
        )
    )


def wait_for_health(api_url: str, timeout: float = 300.0) -> None:
    """Block until the deployment answers ``/health``.

    The first request pays for the container start, which is where Postgres
    boots and the migration chain runs -- a few seconds, but the request
    that triggers it can sit far longer than a default client timeout while
    Modal schedules the container.
    """
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    with httpx.Client(timeout=60.0) as client:
        while time.monotonic() < deadline:
            try:
                response = client.get(f"{api_url.rstrip('/')}/health")
                if response.status_code == 200:
                    return
                last_error = RuntimeError(
                    f"{response.status_code}: {response.text[:200]}"
                )
            except httpx.HTTPError as error:
                last_error = error
            time.sleep(2.0)
    raise TimeoutError(
        f"The registry at {api_url} did not become healthy within "
        f"{timeout:.0f}s. Last: {last_error!r}. Migrations run at container "
        f"start, so a persistent failure here is usually a migration error -- "
        f"check the app's Modal logs."
    )


def read_boot_id(api_url: str) -> str:
    """The current container's boot id (see ``_registry_app``)."""
    with httpx.Client(timeout=60.0) as client:
        response = client.get(f"{api_url.rstrip('/')}/_harness/boot")
        response.raise_for_status()
        return str(response.json()["boot_id"])


def connect(
    api_url: str,
    *,
    admin_password: str,
    execution_modal_env: str,
    workspace_name: str = DEFAULT_WORKSPACE_NAME,
    environment_slug: str = DEFAULT_ENVIRONMENT_SLUG,
    registry_name: str = "registry-live",
    profile_name: str = "registry-live",
) -> Deployment:
    """Run the self-host connect flow against ``api_url``.

    Calling ``run_connect`` rather than shelling out to the CLI: same code,
    no subprocess, and its return value says what it actually did instead of
    having to be parsed back out of console output.

    It writes an SDK registry and profile under ``~/.stardag`` as a side
    effect. Nothing here relies on that profile -- the returned
    ``Deployment`` carries the coordinates, and callers configure the SDK
    from environment variables instead (see ``provision.sdk_environment``)
    -- but be aware that running this against a personal machine adds a
    profile named ``registry-live``.
    """
    from stardag._cli._selfhost_connect import login_local, run_connect

    if not execution_modal_env:
        raise ValueError(
            "execution_modal_env is required: it decides which Modal "
            "environment receives the stardag-api-key secret, and the "
            "default is the account's main environment."
        )

    session_token = login_local(
        api_url, ADMIN_EMAIL, admin_password, registry_name=registry_name
    )
    outcome = run_connect(
        api_url,
        session_token,
        ADMIN_EMAIL,
        primary_workspace=workspace_name,
        environment_slug=environment_slug,
        execution_modal_env=execution_modal_env,
        registry_name=registry_name,
        profile_name=profile_name,
        interactive=False,
        # There is no pre-existing secret to protect: the execution
        # environment was created by this run and is deleted with it.
        overwrite_api_key_secret=True,
    )

    api_key, workspace_id, environment_id = _mint_api_key(
        api_url,
        session_token,
        workspace_name=workspace_name,
        environment_slug=environment_slug,
    )

    if outcome.modal_secret_name is None:
        raise RuntimeError(
            "connect did not push the stardag-api-key Modal secret, so the "
            "workers would have no credential and would fall back to "
            "reporting nowhere. That is the one failure this tier must never "
            "paper over."
        )

    return Deployment(
        api_url=api_url,
        modal_environment=execution_modal_env,
        workspace_slug=outcome.workspace_slug,
        environment_slug=outcome.environment_slug,
        workspace_id=workspace_id,
        environment_id=environment_id,
        api_key=api_key,
        boot_id=read_boot_id(api_url),
    )
