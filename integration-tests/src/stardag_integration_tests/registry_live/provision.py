"""Bring a throwaway Stardag stack up and down on Modal.

    python -m stardag_integration_tests.registry_live.provision up
    python -m stardag_integration_tests.registry_live.provision status
    python -m stardag_integration_tests.registry_live.provision stop
    python -m stardag_integration_tests.registry_live.provision down

Provisioning is **outside pytest**, and that is not a stylistic choice.
Under ``pytest-xdist`` every worker process runs ``pytest_sessionstart``,
so a session-scoped deployment would be built once per worker -- four
registries, four databases, four sets of credentials, and scenarios
talking to whichever one their worker happened to create. Splitting the
stack out is what lets the scenarios run concurrently, which is the whole
reason to be on Modal: they spend nearly all their wall clock asleep
waiting on containers.

It is also the primitive worth having on its own. Verifying reactive
scheduling by hand used to mean a long-lived hosted deployment and a
database account; this brings up an equivalent stack in about a minute,
throws it away in one call, and gives every worktree its own.

The coordinates are written to a file rather than exported, because a
subprocess cannot set its parent's environment. ``pytest`` reads the file
for the Modal environment it was told to use.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

from ._harness import (
    Deployment,
    connect,
    deploy_registry,
    modal_cli,
    stop_existing_app,
)
from ._registry_app import DEFAULT_APP_NAME as DEFAULT_REGISTRY_APP

# Local stacks are named `dev-*`; CI's are `ci-pr-<n>` and
# `ci-manual-<run-id>`. Keeping the two prefixes disjoint is load-bearing:
# the workflow's sweeper deletes environments by name match, so a local
# stack must never be nameable by that sweep, and no local stack should be
# able to shadow a CI one.
LOCAL_PREFIX = "dev-"

# The only Modal environments this module will create or destroy. An
# allowlist rather than a list of names to avoid, because the failure being
# guarded against is unbounded: `modal environment delete` is irrevocable
# and takes every app, volume and secret inside with it, and this runs in a
# workspace that also holds standing deployments. Anything not obviously
# disposable is refused, including names nobody has thought of yet.
DISPOSABLE_PREFIXES = (LOCAL_PREFIX, "ci-")

COORDINATES_DIR = ".registry-live"


def default_environment_name() -> str:
    """A Modal environment name derived from this checkout.

    Stable across runs in one worktree, so a re-run reuses the same
    environment (and its warm image layers) and ``down`` has something
    predictable to delete. Distinct between worktrees, so two branches
    being worked on at once do not deploy over each other -- which is the
    normal state of this repo, where several `sta-*` worktrees are live at
    the same time.
    """
    return LOCAL_PREFIX + _slug(_repo_root().name)


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", value.lower()).strip("-")
    return slug or "local"


def _repo_root() -> Path:
    here = Path(__file__).resolve()
    for candidate in [here, *here.parents]:
        if (candidate / "app" / "stardag-api" / "pyproject.toml").exists():
            return candidate
    raise RuntimeError(
        f"Could not locate the stardag repo root from {here} "
        "(looked for app/stardag-api/pyproject.toml)."
    )


def coordinates_path(modal_environment: str) -> Path:
    """Where one environment's coordinates live.

    Keyed by environment name so concurrent worktrees do not overwrite each
    other's, and under `integration-tests/` so a stray file is obvious and
    local rather than hidden in a temp directory.
    """
    return (
        _repo_root()
        / "integration-tests"
        / COORDINATES_DIR
        / f"{_slug(modal_environment)}.json"
    )


def load_coordinates(modal_environment: str) -> Deployment | None:
    path = coordinates_path(modal_environment)
    if not path.exists():
        return None
    return Deployment(**json.loads(path.read_text()))


def sdk_environment(deployment: Deployment) -> dict[str, str]:
    """The environment variables that point the SDK at this deployment.

    Direct overrides, never a profile. Profile resolution reads
    ``~/.stardag/config.toml`` *and* walks the working directory's parents
    looking for one -- so a checkout under the developer's home directory
    finds their real config, whose default profile may well be a registry
    that other people depend on. These four beat every file.
    """
    return {
        "STARDAG_API_URL": deployment.api_url,
        "STARDAG_WORKSPACE_ID": deployment.workspace_id,
        "STARDAG_ENVIRONMENT_ID": deployment.environment_id,
        "STARDAG_API_KEY": deployment.api_key,
        "MODAL_ENVIRONMENT": deployment.modal_environment,
    }


def up(modal_environment: str) -> Deployment:
    """Deploy the registry, wire the SDK to it, deploy the scenario app."""
    _require_disposable_name(modal_environment, "provision into")
    _require_expected_workspace()
    repo_root = _repo_root()
    admin_password = f"harness-{secrets.token_urlsafe(24)}"

    _ensure_modal_environment(modal_environment)

    api_url = deploy_registry(
        repo_root,
        modal_environment=modal_environment,
        admin_password=admin_password,
    )
    deployment = connect(
        api_url,
        admin_password=admin_password,
        execution_modal_env=modal_environment,
    )

    # The CLI subprocess below resolves the registry from these, exactly as
    # the scenarios will.
    os.environ.update(sdk_environment(deployment))
    os.environ.pop("STARDAG_PROFILE", None)

    _deploy_dag_app(repo_root, modal_environment)

    path = coordinates_path(modal_environment)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(deployment), indent=2) + "\n")
    return deployment


def stop(modal_environment: str) -> None:
    """Stop both apps, leaving the environment and its image cache.

    The counterpart to ``down`` for an environment that is meant to
    survive. The registry pins ``min_containers=1``, which keeps a
    container warm *even when idle* -- so a deployment nobody deletes goes
    on running indefinitely rather than winding down.

    That matters for exactly one environment: CI's scheduled run uses a
    permanent `ci-main`, which no cleanup path can name (teardown is gated
    on the environment being ephemeral, the closed-PR job only knows
    `ci-pr-*`, and the sweeper's filters exclude it by design). Without
    this, one scheduled run leaves a registry serving nothing until the
    next one replaces it a week later.

    Stopping rather than deleting keeps the workspace-level image layers
    warm, which is what makes the next run's deploy take seconds.
    """
    _require_disposable_name(modal_environment, "stop apps in")
    from .dag_app import APP_NAME

    for app_name in (DEFAULT_REGISTRY_APP, APP_NAME):
        stop_existing_app(app_name, modal_environment)
    coordinates_path(modal_environment).unlink(missing_ok=True)


def down(modal_environment: str) -> None:
    """Delete the Modal environment, and with it everything in the stack.

    One call removes the registry app, its container and therefore its
    database, the scenario app, the target-root volume and the API-key
    secret. That completeness is the reason the database lives inside the
    container rather than in a hosted service.
    """
    _require_disposable_name(modal_environment, "delete")
    # The destructive half deserves the workspace assertion at least as
    # much as `up` does: the same reasoning about token env vars beating a
    # profile name applies, and this call cannot be undone.
    _require_expected_workspace()
    result = subprocess.run(
        [modal_cli(), "environment", "delete", modal_environment, "--yes"],
        capture_output=True,
        text=True,
    )
    output = ((result.stdout or "") + (result.stderr or "")).strip()
    print(output)

    # Only forget the stack once it is actually gone. Dropping the
    # coordinates on a failed delete is the worst of both worlds: the
    # environment is still there, still holding an always-on container, and
    # the local record that would let anyone find it again is gone -- so it
    # reads as torn down and is not.
    if result.returncode != 0 and not _looks_like_no_such_environment(output):
        raise SystemExit(
            f"Could not delete Modal environment {modal_environment!r}, so "
            "it is still there and still costing. The coordinates file has "
            f"been left in place: {coordinates_path(modal_environment)}"
        )
    coordinates_path(modal_environment).unlink(missing_ok=True)


def _looks_like_no_such_environment(output: str) -> bool:
    """Whether the delete failed because there was nothing to delete.

    That is success for this purpose. Narrow on purpose, like the
    equivalent check for apps: an unrecognised message is treated as a real
    failure, so a wording change makes teardown noisy rather than silently
    leaving environments behind.

    Exactly one phrase, and it is Modal's actual wording read off the CLI
    rather than guessed -- "No such environment 'x'".

    An earlier version held four plausible phrasings and not that one, so
    all four missed. The fix was to add the real one; the *rest* of the
    list then had to go, because "narrow" was doing real work here and the
    extras quietly undid it. Bare "not found" and "could not find" are
    substrings of a wide class of unrelated Modal errors -- auth, network,
    a stale CLI -- and matching one of those here means `down` skips its
    refusal and deletes the coordinates while the environment lives on,
    which is precisely the outcome the caller's guard exists to prevent.
    """
    return "no such environment" in output.lower()


def _require_disposable_name(modal_environment: str, action: str) -> None:
    """Refuse to build or destroy anything that is not clearly throwaway.

    The Modal workspace this runs in also holds standing deployments --
    the self-hosted service, demos, `main`. Deleting an environment is
    irrevocable and takes every app, volume and secret inside it, so the
    one mistake worth making impossible rather than merely unlikely is a
    name that resolves to something someone depends on.

    Hence an allowlist of prefixes. A name nobody anticipated is refused
    by default, which is the right direction for this to fail in.
    """
    if not modal_environment.startswith(DISPOSABLE_PREFIXES):
        raise SystemExit(
            f"Refusing to {action} Modal environment {modal_environment!r}: "
            f"only {' and '.join(p + '*' for p in DISPOSABLE_PREFIXES)} are "
            "treated as disposable, and this workspace also holds standing "
            f"deployments. Local stacks are named {LOCAL_PREFIX}*; pass "
            "--modal-env, or let it default to this checkout's name."
        )


def _environment_exists(name: str) -> bool:
    """Whether a Modal environment of exactly this name exists.

    Parsed rather than substring-matched. A substring test against the raw
    JSON answers a different question than the one being asked: any field
    that happens to contain the name -- a web suffix, another environment
    whose name extends this one -- makes it true, and a CLI that prefixes a
    warning to its output makes it false.
    """
    listed = subprocess.run(
        [modal_cli(), "environment", "list", "--json"],
        capture_output=True,
        text=True,
    )
    if listed.returncode != 0:
        return False
    try:
        environments = json.loads(listed.stdout)
    except json.JSONDecodeError:
        # Not knowing is not the same as knowing it is absent, but the
        # caller's next move either way is to try creating it -- and that
        # now treats "already exists" as success.
        return False
    return any(entry.get("name") == name for entry in environments)


def _require_expected_workspace() -> None:
    """Assert the credentials belong to ``STARDAG_MODAL_TEST_WORKSPACE``.

    Opt-in, and unset means no check -- a contributor pointing this at
    their own Modal account should not have to name it. CI sets it, which
    is where the assertion earns its keep.

    Resolved from the *token*, not from a profile name, and the difference
    is the whole point: ``MODAL_PROFILE`` selects a section of a
    ``.modal.toml``, but ``MODAL_TOKEN_ID``/``MODAL_TOKEN_SECRET`` take
    precedence over that file and are not bound to the name, so a profile
    name can agree with itself while the credentials point somewhere else
    entirely. Same mechanism, and same env var, as ``live_modal_guard``'s
    check -- deliberately one convention rather than two.

    An unresolvable workspace counts as a mismatch. The alternative is
    treating "could not tell" as "fine", on the one code path whose job is
    to be sure.
    """
    expected = os.environ.get("STARDAG_MODAL_TEST_WORKSPACE", "").strip()
    if not expected:
        return

    # Private, and reached deliberately: it is the same helper the live
    # Modal guard uses, in this repo, and duplicating the lookup would let
    # the two answers drift.
    from stardag.testing.modal._live import _active_modal_workspace

    active = _active_modal_workspace()
    if active != expected:
        raise SystemExit(
            f"Modal credentials resolve to workspace {active!r}, which does "
            f"not match STARDAG_MODAL_TEST_WORKSPACE={expected!r}"
            + (
                " (no credentials, or the workspace lookup failed)"
                if active is None
                else ""
            )
        )
    print(f"[provision] Modal workspace {active!r} confirmed from the token")


def _ensure_modal_environment(name: str) -> None:
    if _environment_exists(name):
        print(f"[provision] reusing Modal environment {name!r}")
        return
    created = subprocess.run(
        [modal_cli(), "environment", "create", name],
        capture_output=True,
        text=True,
    )
    if created.returncode == 0:
        print(f"[provision] created Modal environment {name!r}")
        return

    # Losing a race to create it is success, not failure. In CI the two
    # tiers run in parallel and share one environment, so both can find it
    # missing and both try to create it; whichever loses would otherwise
    # abort a job for having got what it wanted.
    output = ((created.stderr or "") + (created.stdout or "")).strip()
    if "already exists" in output.lower():
        print(f"[provision] Modal environment {name!r} was created concurrently")
        return

    raise SystemExit(f"Could not create Modal environment {name!r}: {output}")


def _deploy_dag_app(repo_root: Path, modal_environment: str) -> None:
    """Deploy the scenario app with the CLI from *this* environment.

    Not a bare ``stardag`` off ``PATH``: the app's Modal functions are
    serialized by whatever interpreter imports the module, and every later
    trigger unpickles them in a container built for that Python. Resolving
    the CLI next to ``sys.executable`` makes the deploy and the scenarios
    agree by construction. A mismatch is not a graceful error -- the
    container dies with a bare SIGSEGV and leaves a build that looks empty.
    """
    from .dag_app import APP_NAME

    # Warm containers from an earlier run hold the `stardag-api-key` secret
    # as it was when they started, and connect has just *rotated* that key.
    # A survivor authenticates with a revoked credential and takes a 401
    # from the registry partway through a build.
    stop_existing_app(APP_NAME, modal_environment)

    stardag_cli = Path(sys.executable).with_name("stardag")
    if not stardag_cli.exists():
        raise SystemExit(
            f"No stardag CLI next to {sys.executable}. This tier deploys with "
            "the CLI from its own environment on purpose -- see above."
        )
    result = subprocess.run(
        [str(stardag_cli), "modal", "deploy", "-m", f"{__package__}.dag_app"],
        cwd=repo_root / "integration-tests",
        capture_output=True,
        text=True,
        env={**os.environ, "MODAL_ENVIRONMENT": modal_environment},
    )
    if result.returncode != 0:
        raise SystemExit(
            f"Failed to deploy the scenario app:\n{result.stdout}\n{result.stderr}"
        )
    print(f"[provision] deployed {APP_NAME!r}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="provision",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("action", choices=("up", "down", "stop", "status"))
    parser.add_argument(
        "--modal-env",
        default=os.environ.get("MODAL_ENVIRONMENT") or default_environment_name(),
        help="Modal environment for this stack (default: derived from the checkout)",
    )
    args = parser.parse_args(argv)

    if args.action == "up":
        deployment = up(args.modal_env)
        print(f"\n[provision] {args.modal_env} is up: {deployment.api_url}")
        print(f"[provision] coordinates: {coordinates_path(args.modal_env)}")
        print("[provision] run the scenarios with:")
        print(f"    MODAL_ENVIRONMENT={args.modal_env} tox -e registry-modal-live")
        print("[provision] and when you are done:")
        print(
            "    python -m stardag_integration_tests.registry_live.provision "
            f"down --modal-env {args.modal_env}"
        )
        return 0

    if args.action == "down":
        down(args.modal_env)
        return 0

    if args.action == "stop":
        stop(args.modal_env)
        return 0

    deployment = load_coordinates(args.modal_env)
    if deployment is None:
        print(f"[provision] no stack recorded for {args.modal_env!r}")
        return 1
    print(f"[provision] {args.modal_env}: {deployment.api_url}")
    print(f"[provision] boot id at provision time: {deployment.boot_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
