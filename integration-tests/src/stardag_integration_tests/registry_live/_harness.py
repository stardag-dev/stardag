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

    **This writes to the SDK config under ``$HOME``.** The session fixture
    points ``HOME`` at a temporary directory before calling it -- without
    that, a local run silently rewrites the developer's own ``selfhosted``
    profile to point at a deployment that is about to be deleted.
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
        boot_id=read_boot_id(api_url),
    )
