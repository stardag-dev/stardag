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

import collections
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

import pytest

from stardag_integration_tests.registry_live._diagnostics import (
    CLASSIFICATION_FAILED,
    DIAGNOSTICS_DIR_ENV,
    record_non_timeout_failure,
    record_transport_timeout,
    transport_timeout,
)
from stardag_integration_tests.registry_live._gates import GateSet
from stardag_integration_tests.registry_live._guard import ENV_API_URL, is_enabled
from stardag_integration_tests.registry_live._harness import (
    BootCheckUnanswered,
    Deployment,
    RegistryContainerRecycled,
)
from stardag_integration_tests.registry_live._ordering import (
    controller_dist_mode,
    plan_order,
)
from stardag_integration_tests.registry_live.provision import (
    default_environment_name,
    load_coordinates,
    sdk_environment,
)

ENV_MODAL_ENVIRONMENT = "MODAL_ENVIRONMENT"

# A scenario's declared wall clock, ``pytest.mark.budget(seconds)``: what the
# collection is ordered by (longest first, see ``_ordering``) and what the
# run's summary compares each scenario's actual time against. A scenario
# without one is planned as this, and the summary says it has none.
DEFAULT_BUDGET_SECONDS = 240.0
BUDGET_PROPERTY = "registry_live_budget_seconds"

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
    config.addinivalue_line(
        "markers",
        "budget(seconds): the scenario's expected wall clock; orders the tier "
        "longest first and is reported against the actual time",
    )
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


def _budget(item: pytest.Item) -> tuple[float, bool]:
    """``(budget seconds, declared?)`` for one collected scenario."""
    marker = item.get_closest_marker("budget")
    if marker is None or not marker.args:
        return DEFAULT_BUDGET_SECONDS, False
    return float(marker.args[0]), True


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]):
    """Hand xdist the longest scenarios first, so the critical path starts at
    t=0 rather than wherever the alphabet put it.

    ``trylast`` so it plans only what ``-k``/``-m`` left selected. Runs in
    every xdist worker, each of which collects on its own; the plan is a
    pure function of the items and the worker count, so they all agree, as
    xdist requires. See ``_ordering`` for why the plan is not a plain sort.
    """
    budgets = [_budget(item)[0] for item in items]
    workerinput = getattr(config, "workerinput", None)
    workers = int(workerinput["workercount"]) if workerinput else 1
    if workers > 1 and controller_dist_mode(workerinput) == "load":
        order = plan_order(
            budgets, workers, maxschedchunk=config.getoption("maxschedchunk", None)
        )
    else:
        order = sorted(range(len(items)), key=lambda i: (-budgets[i], i))
    items[:] = [items[i] for i in order]
    for item in items:
        budget, declared = _budget(item)
        # Carried on every report, so the xdist controller -- which never
        # collects -- can put budget and actual side by side.
        item.user_properties.append((BUDGET_PROPERTY, budget if declared else None))


_actual_seconds: dict[str, float] = defaultdict(float)
_budgets: dict[str, float | None] = {}
_started_at: dict[str, float] = {}
_failed: set[str] = set()


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    """Accumulate each scenario's wall clock across setup, call and teardown."""
    properties = dict(report.user_properties)
    if BUDGET_PROPERTY not in properties:
        return
    budget = properties[BUDGET_PROPERTY]
    _budgets[report.nodeid] = float(budget) if isinstance(budget, int | float) else None
    _actual_seconds[report.nodeid] += report.duration
    start = getattr(report, "start", None)
    if start is not None:
        _started_at[report.nodeid] = min(_started_at.get(report.nodeid, start), start)
    if report.failed:
        _failed.add(report.nodeid)


def _budget_summary(terminalreporter) -> None:
    """Budget against actual, per scenario, slowest first; overruns flagged.

    Printed on every run, green or red, because a budget that has drifted is
    how the ordering silently stops putting the critical path first: the
    plan is only as good as these numbers. When the tier is under xdist,
    this runs in the controller, from the workers' reports.
    """
    if not _actual_seconds:
        return
    t0 = min(_started_at.values(), default=0.0)
    over = []
    terminalreporter.section("registry-live budgets (actual / budget, start)")
    for nodeid in sorted(_actual_seconds, key=lambda n: -_actual_seconds[n]):
        actual = _actual_seconds[nodeid]
        budget = _budgets.get(nodeid)
        start = _started_at.get(nodeid)
        offset = f"+{start - t0:4.0f}s" if start is not None else "    ?"
        if budget is None:
            verdict, shown = "  NO BUDGET DECLARED", "   -"
        elif actual > budget:
            verdict, shown = "  OVER BUDGET", f"{budget:4.0f}"
            over.append(nodeid)
        else:
            verdict, shown = "", f"{budget:4.0f}"
        failed = "  (failed)" if nodeid in _failed else ""
        terminalreporter.write_line(
            f"{actual:5.0f}s / {shown}s  start {offset}  {nodeid}{verdict}{failed}",
            yellow=bool(verdict),
        )
    if over:
        terminalreporter.write_line(
            f"WARNING: {len(over)} scenario(s) over budget. Raise the budget "
            "if the scenario legitimately grew; otherwise it is a regression "
            "in the scenario's own timing.",
            yellow=True,
            bold=True,
        )


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]):
    """Classify every failure, at the instant it happens.

    Two records come out of this, and CI needs both: a transport timeout
    is what buys a run its one retry, and anything else is what forbids
    one. The tier runs sixteen scenarios at once, so "somebody timed out"
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

    **Every failed report is classified, exception or not**, because a
    strict ``xfail`` that passes carries none and would otherwise leave
    the run retryable. And anything that goes wrong in here fails
    *closed*: it prints ``CLASSIFICATION_FAILED``, which the workflow
    refuses to retry over, because an absent marker is read as proof that
    nothing real broke.

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
        # Fail closed, loudly. A classifier that died has decided nothing,
        # and CI reads an absent marker as "nothing real broke" -- so the
        # run must become non-retryable on the strength of this line.
        print(f"{CLASSIFICATION_FAILED}: {error!r}", file=sys.stderr)
    return report


def _classify(
    item: pytest.Item, call: pytest.CallInfo[None], report: pytest.TestReport
) -> None:
    if not report.failed or _deployment is None:
        return
    if report.when not in ("setup", "call", "teardown"):
        return

    # Not gated on ``call.excinfo``. A strict ``xfail`` that passes is a
    # failed report carrying no exception at all, and dropping it would
    # leave the run retryable over an XPASS.
    error = call.excinfo.value if call.excinfo is not None else None
    timeout = transport_timeout(error) if error is not None else None
    if error is not None and timeout is not None:
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


@pytest.fixture
def gates(deployment: Deployment):
    """The scenario's gates (see ``_gates``): ``gates.new(name, salt=...)``.

    On teardown every gate the scenario did not release is released, so a
    scenario that failed early frees its held containers now rather than at
    their bound; then one line per recorded hold says how it ended, flagging
    any that ran to its bound -- the lost release that would otherwise pass
    for a slow scenario. Neither step can raise: this is teardown, and a
    failure here would be classified as a real one.
    """
    gate_set = GateSet(deployment.modal_environment)
    yield gate_set
    for gate in gate_set.gates:
        if gate.released_at is None:
            gate.release(attempts=2)
    for line in gate_set.report():
        print(f"[registry-live] {line}", file=sys.stderr)


@pytest.fixture(scope="session")
def deployment() -> Deployment:
    """The provisioned registry for this session."""
    if _deployment is None:
        pytest.fail(
            "No deployment: pytest_configure did not complete. The error "
            "that stopped it is above this line."
        )
    return _deployment


# -- Retries: the lost answers that no longer fail a scenario ----------------
#
# The SDK and the harness both send an exchange again when it got no
# complete answer, so the class that used to redden this tier now mostly
# passes. It is still counted, because a tier that absorbs a fault silently
# has stopped measuring it: each xdist worker hands its tally to the
# controller, which prints the total and leaves it in the diagnostics
# directory for CI to annotate, green run or red.

RETRY_COUNTS_NAME = "transport-retries.json"

_worker_retry_counts: collections.Counter[str] = collections.Counter()


def _process_retry_counts() -> dict[str, int]:
    from stardag.registry._api_http import transport_retry_counts

    from stardag_integration_tests.registry_live._events import retry_counts

    counts = collections.Counter(transport_retry_counts())
    counts.update(retry_counts())
    return dict(counts)


def pytest_sessionfinish(session: pytest.Session) -> None:
    workeroutput = getattr(session.config, "workeroutput", None)
    if workeroutput is not None:
        workeroutput["transport_retries"] = _process_retry_counts()


def pytest_testnodedown(node, error) -> None:  # pytest-xdist hook
    counts = getattr(node, "workeroutput", {}).get("transport_retries") or {}
    _worker_retry_counts.update(counts)


def _retry_summary(terminalreporter, config) -> None:
    if not is_enabled() or hasattr(config, "workerinput"):
        return
    counts = collections.Counter(_process_retry_counts())
    counts.update(_worker_retry_counts)
    total = sum(counts.values())
    by_cause = ", ".join(f"{cause}: {n}" for cause, n in counts.most_common())
    terminalreporter.write_sep(
        "-",
        f"registry exchanges retried from this runner: {total}"
        + (f" ({by_cause})" if by_cause else ""),
    )
    directory = os.environ.get(DIAGNOSTICS_DIR_ENV, "").strip()
    if directory:
        try:
            Path(directory).mkdir(parents=True, exist_ok=True)
            (Path(directory) / RETRY_COUNTS_NAME).write_text(
                json.dumps(dict(counts), sort_keys=True) + "\n"
            )
        except OSError as error:
            print(f"Could not record the retry counts: {error}", file=sys.stderr)


def pytest_terminal_summary(terminalreporter, exitstatus, config) -> None:
    """Two tallies at the end of every run: time against budget per scenario,
    and the lost answers the clients absorbed."""
    _budget_summary(terminalreporter)
    _retry_summary(terminalreporter, config)
