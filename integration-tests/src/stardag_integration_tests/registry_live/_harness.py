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

import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from ._registry_app import (
    DEFAULT_APP_NAME,
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

# Where a run records that the registry container was replaced under it, so
# that whatever is driving the tier can tell that failure apart from a real
# one. Set by CI; unset locally, where nothing is written and the assertion
# message is the whole story.
RECYCLE_MARKER_ENV = "STARDAG_REGISTRY_LIVE_RECYCLE_MARKER"

# Reading the boot id at provisioning time waits out a cold start, so it
# gets a long timeout and one try. The post-scenario check cannot: it runs
# after every scenario, so its worst case has to stay small enough to sit
# inside the job's budget even when the registry has gone for good. Six
# tries at fifteen seconds gives a replacement a hundred seconds to
# identify itself, which is several times what one needs, and costs under
# two minutes when nothing ever answers.
BOOT_READ_TIMEOUT_SECONDS = 60.0
BOOT_READ_ATTEMPTS = 6


def record_recycle(previous: str, current: str) -> None:
    """Leave the evidence of a recycle where a shell can read it.

    Two callers, and neither is optional. The post-scenario check below
    is the usual one. The other is the boot probe a transport timeout
    runs (``_diagnostics``): if it answers from a *different* container,
    it has identified a recycle off this same nonce, and saying so there
    rather than waiting for a second read means the retry re-provisions
    even when that second read gets no answer either.

    **Why a retry, and not a database that survives the container.** The
    obvious fix for "a recycle loses the whole database" is to put PGDATA on
    a Modal Volume. It was considered and rejected, on three counts:

    - It does not save the run. The scenarios in flight were mid-request
      against a process that no longer exists, and the several seconds a
      replacement spends starting Postgres and re-running Alembic are
      several seconds of refused connections. A durable database converts
      "every scenario fails" into "every scenario in flight fails", which
      is still a red check.
    - It costs the property that makes provisioning cheap. The cluster is
      ``initdb``-ed into an image layer, so a container is serving in about
      a second. Mounting a volume over ``/pgdata`` hides that layer, so
      initialisation moves to first boot and needs an "is it already
      there?" branch -- a slower start, and a new way for two runs of the
      same environment to disagree about whose data they are looking at.
    - ``fsync`` is off, deliberately, because the database is meant to be
      discarded. A container killed mid-write would leave a cluster that
      may not start at all, trading a clean "the database is gone" for a
      corrupt one.

    So a recycle is *identified* instead -- which is what the boot nonce
    already does -- and a run that lost its database is provisioned again
    and re-run. Keyed to the identification, so it masks nothing: with a
    stable boot id nothing is retried, and a scenario that fails on its own
    merits fails the check exactly as before. The marker is also the
    measurement this decision was waiting on, since every retry is one
    recycle, recorded where somebody will see it.
    """
    path = os.environ.get(RECYCLE_MARKER_ENV, "").strip()
    if not path:
        return
    try:
        Path(path).write_text(f"{previous} -> {current}\n")
    except OSError as error:  # pragma: no cover - diagnostics only
        print(
            f"Could not write the recycle marker to {path!r}: {error}",
            file=sys.stderr,
        )


class BootCheckUnanswered(RuntimeError):
    """The post-scenario boot check got no answer at all.

    Raised *from* the transport error that ended it, so the
    transport-timeout discriminator still finds the timeout by walking
    the cause chain and classifies this as the failure class it is.

    Its own type for one reason: it is the only failure that arrives with
    its probe already done. ``assert_same_container`` has just read
    ``/_harness/boot`` six times over a hundred seconds without an
    answer, so probing again would add delay and no information. Nothing
    else may claim that -- fixtures do registry I/O in teardown too, and
    an earlier version inferred it from the *phase*, which quietly
    swallowed the probe for a ``slot_limit`` cleanup that timed out.
    """


class RegistryContainerRecycled(AssertionError):
    """The process holding the database was replaced mid-run.

    Its own type because one consumer has to tell it apart from every
    other teardown failure and cannot do so by phase. A non-timeout
    failure in teardown normally *forbids* CI's retry -- fixtures here
    tear down through the registry, and ``test_limit_slot_wake``'s
    ``slot_limit`` deletes a concurrency limit in a ``finally`` -- but
    this one must not, because it has its own marker and its own retry,
    and that retry re-provisions. An earlier version exempted the whole
    teardown phase instead, which was one exemption doing the work of
    two.

    An ``AssertionError`` subclass, so pytest reports it as before and
    the transport-timeout discriminator keeps excluding it for free.
    """


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

    def current_boot_id(
        self,
        *,
        attempts: int = 1,
        retry_pause: float = 3.0,
        timeout: float = BOOT_READ_TIMEOUT_SECONDS,
    ) -> str:
        """The boot id the registry answers with now.

        ``attempts`` above one tolerates a boot endpoint that is briefly
        unanswerable, which is not a hypothetical: the moment this is most
        needed -- a container replaced mid-run -- is also the moment the
        replacement may still be starting Postgres and running Alembic.
        A shorter ``timeout`` goes with it, so that several attempts stay
        bounded by something a teardown check can afford.
        """
        last: Exception | None = None
        for attempt in range(attempts):
            try:
                return read_boot_id(self.api_url, timeout=timeout)
            except Exception as error:
                last = error
                if attempt + 1 < attempts:
                    time.sleep(retry_pause)
        assert last is not None
        raise last

    def assert_same_container(self) -> None:
        """Fail loudly if the process holding the database was replaced.

        Worth calling at the end of any scenario that spans minutes. A
        recycled container does not lose *rows*, it loses the entire
        database, and the symptom -- tasks that have silently reverted to
        unregistered, a build that cannot find its own plan -- is a very
        convincing impression of a stardag bug. One assertion converts that
        into a sentence.
        """
        # Retried, because an unanswerable registry must not be allowed to
        # turn a recycle into an unclassified failure: the marker below is
        # what buys the run its one retry, and it is only written when the
        # replacement has actually identified itself. A registry that never
        # answers stays unclassified on purpose -- nothing was identified,
        # so nothing is retried.
        try:
            current = self.current_boot_id(
                attempts=BOOT_READ_ATTEMPTS, retry_pause=3.0, timeout=15.0
            )
        except Exception as error:
            # Named, rather than left as the bare transport error, so the
            # one caller that needs to know this probe has already run can
            # tell. See BootCheckUnanswered.
            raise BootCheckUnanswered(
                f"The registry at {self.api_url} did not answer "
                f"/_harness/boot in {BOOT_READ_ATTEMPTS} attempts, so "
                f"whether the container survived this scenario is unknown."
            ) from error
        if current != self.boot_id:
            record_recycle(self.boot_id, current)
            raise RegistryContainerRecycled(
                f"The registry container was replaced mid-run (boot id "
                f"{self.boot_id} -> {current}). Its Postgres is inside that "
                f"container, so the database this scenario was writing to no "
                f"longer exists. This is a harness failure and not a "
                f"scheduling one: provision the stack again and re-run. CI "
                f"does that by itself, once, off the marker this just "
                f"wrote -- see record_recycle for why that rather than a "
                f"database outliving the container."
            )


def deploy_registry(
    repo_root: Path,
    *,
    modal_environment: str,
    admin_password: str,
    app_name: str = DEFAULT_APP_NAME,
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
    if _looks_like_nothing_to_stop(output):
        print(f"[harness] no running {app_name!r} to stop")
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


def _looks_like_nothing_to_stop(output: str) -> bool:
    """Whether Modal's complaint means "there is nothing running here".

    Two ways to already be in the state this function wants: no such app,
    and an app that is already stopped. Both satisfy what the caller
    actually needs — no survivor serving the previous run's data or code,
    and no warm container holding a since-rotated API key.

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
            # Kept as a second spelling of the same idea, because this one
            # is specific to apps. The bare "not found" / "could not find"
            # that used to sit here are gone: they are substrings of
            # unrelated Modal errors (auth, network, a stale CLI), so they
            # undid the narrowness the docstring claims -- and swallowing
            # one of those turns this helper back into the no-op it once
            # silently was.
            "no such app",
            # An app that has been stopped already. Reached on the *second*
            # run against a reused environment — the per-PR environment
            # outlives a run and is deleted only when the PR closes, so the
            # previous run's teardown (or its cancellation) leaves the app
            # stopped and the next run found that fatal. "Already in the
            # state I was asked to put it in" is success, exactly as it is
            # for the environment-create race in `modal-live.yml`.
            "already stopped",
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


def read_boot_id(api_url: str, *, timeout: float = BOOT_READ_TIMEOUT_SECONDS) -> str:
    """The current container's boot id (see ``_registry_app``)."""
    with httpx.Client(timeout=timeout) as client:
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
