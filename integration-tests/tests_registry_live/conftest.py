"""Session setup for the registry-live tier.

This file **deploys nothing**. The stack is brought up separately, by
``registry_live.provision``, and the scenarios are pure consumers of it.

That split is what makes the tier concurrent. Under ``pytest-xdist`` every
worker process runs its own session hooks, so a session-scoped deployment
would be built once per worker -- several registries, several databases,
and scenarios talking to whichever one their worker happened to create.
Provisioning outside pytest means each worker instead reads the same
coordinates off disk. The scenarios spend nearly all their wall clock
asleep waiting on Modal containers, so running them together costs almost
nothing and the tier's runtime stops being the sum of its parts.

It also means a developer keeps a stack between runs, which turns
iterating on one scenario from a two-minute cycle into a twenty-second
one. See ``provision``'s docstring and the DEV_README section.

Deliberately a *separate* test root from ``tests/``: that directory's
conftest brings up docker-compose and Playwright, which this tier needs
none of, and ``testpaths`` in ``pyproject.toml`` still points only at
``tests``, so a bare ``pytest`` never reaches the live tier.
"""

from __future__ import annotations

import os
import sys

import pytest

from stardag_integration_tests.registry_live._diagnostics import (
    record_non_timeout_failure,
    record_transport_timeout,
    transport_timeout,
)
from stardag_integration_tests.registry_live._guard import ENV_API_URL, is_enabled
from stardag_integration_tests.registry_live._harness import (
    BootCheckUnanswered,
    Deployment,
    RegistryContainerRecycled,
)
from stardag_integration_tests.registry_live.provision import (
    default_environment_name,
    load_coordinates,
    sdk_environment,
)

ENV_MODAL_ENVIRONMENT = "MODAL_ENVIRONMENT"

_deployment: Deployment | None = None


def pytest_configure(config: pytest.Config) -> None:
    """Point this process at the provisioned stack, before collection.

    Runs in the xdist controller *and* in every worker, which is exactly
    right: each process needs these environment variables set before it
    imports a scenario module, and reading one small file is cheap enough
    to do several times.

    Before collection matters because each scenario module calls
    ``registry_live_guard()`` at import, so a stack that is missing, or
    pointed somewhere unexpected, is a collection error rather than a
    scenario that quietly ran against the wrong registry.
    """
    global _deployment
    if not is_enabled():
        return

    modal_environment = (
        os.environ.get(ENV_MODAL_ENVIRONMENT, "").strip() or default_environment_name()
    )
    deployment = load_coordinates(modal_environment)
    if deployment is None:
        raise pytest.UsageError(
            f"No provisioned stack for Modal environment "
            f"{modal_environment!r}. This tier deploys nothing itself. "
            "Bring one up first:\n\n"
            "    python -m stardag_integration_tests.registry_live.provision "
            f"up --modal-env {modal_environment}\n\n"
            "and tear it down with `provision down` when you are finished "
            "with it."
        )

    # Direct overrides rather than a profile: profile resolution walks the
    # working directory's parents for a config file as well as reading
    # ~/.stardag, so a checkout under the developer's home finds their real
    # config -- whose default profile may be a registry other people depend
    # on. The guard asserts the resulting URL regardless.
    os.environ.update(sdk_environment(deployment))
    os.environ.pop("STARDAG_PROFILE", None)
    # What the guard compares the resolved registry against. Kept separate
    # from the SDK variables above on purpose: those *configure* the SDK,
    # this one records what the answer is supposed to be, and a check that
    # reads its expectation from the thing it is checking proves nothing.
    os.environ[ENV_API_URL] = deployment.api_url
    _deployment = deployment


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]):
    """Classify every failure, at the instant it happens.

    Two records come out of this, and CI needs both: a transport timeout
    is what buys a run its one retry, and anything else is what forbids
    one. The tier runs twelve scenarios at once, so "somebody timed out"
    and "somebody failed for real" are routinely both true of the same
    run, and only the second may decide it.

    **Here rather than in a fixture, because the timing is the value.**
    The probe asks whether the registry is answering *while the
    scenario's own request is timing out*. A finaliser would run after
    the scenario's other teardown -- including the autouse check below,
    which spends up to a hundred seconds retrying the boot read when the
    registry is unreachable -- by which time the contention has passed
    and the registry answers everything in milliseconds. Every occurrence
    would read as the same reassuring nothing.

    **A wrapper rather than a plain hook**, so the decision is made on
    pytest's own report. ``call.excinfo`` alone is set for a skip and for
    an xfail as well as for a failure, and recording either as a real
    failure would silently forbid a retry the run was entitled to.

    **Every phase is classified, and exactly one failure is exempt.**
    Setup and teardown are not formalities here: fixtures talk to the
    registry at both ends -- ``test_limit_slot_wake``'s ``slot_limit``
    sets a concurrency limit before the scenario and deletes it in a
    ``finally`` afterwards -- so a failure at either end is as real as one
    in the body, and must forbid the retry just the same.

    The exemption is ``RegistryContainerRecycled`` and nothing else. That
    one has its own marker and its own retry, which re-provisions because
    the replacement's database is empty; recording it here would disarm
    the recovery that exists for it. Exempting it by *type* rather than by
    phase is the point -- an earlier version exempted all of teardown,
    which also exempted a fixture's own teardown failing for real.

    The same applies to skipping the boot probe: only
    ``BootCheckUnanswered`` arrives with its probe already done, and it
    says so by being that type. A ``slot_limit`` cleanup timing out is an
    ordinary teardown timeout and gets probed like any other.

    Nothing raised in here may reach pytest: a diagnostic that breaks
    reporting would cost the run the very evidence it exists to collect.
    Under xdist this runs in the worker process; the files it writes are
    on the runner's disk, which is what CI reads back.
    """
    report = yield
    try:
        _classify(item, call, report)
    except Exception as error:  # pragma: no cover - diagnostics only
        print(f"[harness] failure classification errored: {error!r}", file=sys.stderr)
    return report


def _classify(
    item: pytest.Item, call: pytest.CallInfo[None], report: pytest.TestReport
) -> None:
    if not report.failed or call.excinfo is None or _deployment is None:
        return
    if report.when not in ("setup", "call", "teardown"):
        return

    error = call.excinfo.value
    timeout = transport_timeout(error)
    if timeout is not None:
        record_transport_timeout(
            _deployment,
            nodeid=item.nodeid,
            phase=report.when,
            error=error,
            timeout=timeout,
            already_probed=isinstance(error, BootCheckUnanswered),
        )
    elif not isinstance(error, RegistryContainerRecycled):
        record_non_timeout_failure(nodeid=item.nodeid, phase=report.when, error=error)


@pytest.fixture(autouse=True)
def _registry_survived(deployment: Deployment):
    """Check after every scenario that the database still exists.

    A finaliser rather than a line at the end of each test, and the
    difference is the whole point of the check. A recycled container takes
    the database with it, and the symptoms -- tasks reverted to
    unregistered, a build that cannot find its own plan -- fail one of the
    scenario's own assertions *first*. A trailing call therefore runs
    exactly never in the case it was written for, leaving behind a very
    convincing impression of a scheduling bug.

    Raising here on an already-failed test adds an error rather than
    replacing the failure, which is right: both are true, and the second
    explains the first.
    """
    yield
    deployment.assert_same_container()


@pytest.fixture(scope="session")
def deployment() -> Deployment:
    """The provisioned registry for this session."""
    if _deployment is None:
        pytest.fail(
            "No deployment: pytest_configure did not complete. The error "
            "that stopped it is above this line."
        )
    return _deployment
