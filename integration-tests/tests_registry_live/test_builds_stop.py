"""``stardag builds stop`` selects, stops and cancels, in that order.

What only a live run can show is that the command reaches Modal at all:
that it reads a real registry's execution ledger (``GET
/builds/{id}/executions``: the unended executions), picks the ones its
filters name, ends the corresponding function calls, and cancels the build
afterwards. The selection *rules* are pinned in
``tests/test__cli/test_stop.py``, including the one that matters most --
that only the selected executions are handed to the canceller.

**The exclusivity half is asserted here again** (STA-81). It could not be,
while a tick of a *terminal* build still ran the automated cancel drain:
that drain cancelled every execution the build had started, excluded ones
included, so an earlier version of this scenario passed twice locally and
failed in CI where a lingering tick got there first
(``cancelled_refs=4`` in its summary). The drain is gone, nothing else
reaches into a container, and the claim is testable directly.

It is asserted twice, in increasing strength. Immediately after the
command returns, the excluded calls are still running -- which is the
command's actual promise. Then the excluded executions are waited to
their own end on the ledger, ``outcome = completed``, which proves the
stronger thing: they were never touched at all. On v2 that report arrives
*late* -- cancelling the build released their claims, so the tasks went
CANCELLED ("revocation is not a result", design.md, "The runnable rule")
and the completion is recorded on the execution without moving the task
(design.md, "Claim × plan invariants"). The ledger end is the execution's
own report and nothing else writes it, so it is the exclusivity evidence;
the task row staying CANCELLED is asserted too, because a late report that
moved it would be a defect of its own.

**It depends on the upstreams having no cooperative checkpoint**, which
``SlowOnWorker`` does not -- one plain ``time.sleep`` inside ``run()``,
past the start-of-attempt check and with no dynamic-dependency yield.
Give that task a ``stardag.cancellation_requested()`` call and the
excluded workers would exit on the cascade instead of completing, and this
assertion would invert. That is the right behaviour and the wrong test;
change the assertion, not the framework.

The "the selected calls stopped" check below is now an exclusivity check
rather than a mere liveness one, for the same reason: nothing but this
command could have stopped them.
"""

from __future__ import annotations

import json
import uuid

import pytest

from stardag_integration_tests.registry_live._events import task_events
from stardag_integration_tests.registry_live._guard import registry_live_guard
from stardag_integration_tests.registry_live._harness import Deployment
from stardag_integration_tests.registry_live._wait import (
    build_status,
    describe,
    task_status,
    wait_until,
)

registry_live_guard()

pytestmark = [
    pytest.mark.registry_live,
    # Longer than the tier's usual 900: this scenario now waits out the
    # excluded upstreams' own sleep, which is the price of asserting that
    # they were never touched rather than merely that they were listed.
    #
    # Sized above the sum of the per-step budgets below (420 + 420 + 420 +
    # 120 + 180 + 180), so a slow run fails on the step that is actually late
    # and says which, rather than on this one, which says nothing.
    pytest.mark.timeout(1900),
]

# Long enough for the tick to put four containers on two workers and stay
# resident while they start.
LINGER_SECONDS = 240

RUNNING_TIMEOUT_SECONDS = 420
# A cancel reaches Modal's scheduler promptly, but "promptly" is not
# "synchronously" -- poll rather than assert once.
STOPPED_TIMEOUT_SECONDS = 120

# On top of the upstreams' own sleep, for the completion wait: container
# start skew, the time this scenario spends getting all four running, and
# the report's trip back to the registry.
COMPLETION_SLACK_SECONDS = 180


def _call_is_running(ref: str) -> bool:
    """Ask Modal whether a function call is still in flight.

    The same poll the Modal executor's own ``detached_status`` makes: a
    zero timeout raises the builtin ``TimeoutError`` while the call is
    running, and anything else means it is over -- finished, cancelled, or
    gone. Here the ambiguity that note warns about cannot arise, because
    these tasks only ever end by sleeping out or by being cancelled.
    """
    import modal

    try:
        modal.FunctionCall.from_id(ref).get(timeout=0)
    except TimeoutError:
        return True
    except Exception:
        return False
    return False


def _refs(entries: list[dict]) -> dict[str, str]:
    """``task_id -> executor_ref`` from one of the command's JSON lists."""
    return {entry["task_id"]: entry["executor_ref"] for entry in entries}


def _stoppable_ids(build_id: uuid.UUID) -> set[str]:
    """Task ids whose unended execution in this build already names a call.

    Read through the command's own verdict (``_stop.is_stoppable``) over
    the ledger it lists, which is the one thing here that is not
    independent of the code under test. Acceptable, and worth stating: the
    selection *rules* are pinned in ``tests/test__cli/test_stop.py``, and a
    verdict that was wrong would fail this scenario as a timeout rather
    than as a wrong answer. What it buys is that the wait below is on the
    state the command is actually defined against.
    """
    from stardag._cli import _stop
    from stardag.registry import registry_provider

    executions = registry_provider.get().build_list_executions(build_id)
    return {str(e.task_id) for e in executions if _stop.is_stoppable(e)}


def test_stop_cancels_only_the_selected_workers_calls(deployment: Deployment) -> None:
    from typer.testing import CliRunner

    from stardag._cli.builds import app
    from stardag_integration_tests.registry_live.dag_app import app as dag_app
    from stardag_integration_tests.registry_live.selectors import ALT_WORKER
    from stardag_integration_tests.registry_live.tasks import WorkerFanIn

    salt = uuid.uuid4().hex
    root = WorkerFanIn(salt=salt, stopped_worker=ALT_WORKER)
    stopped = root.stopped_tasks()
    kept = root.kept_tasks()

    build_id = dag_app.build_trigger(
        root,
        reactive=True,
        tick_kwargs={"linger_seconds": LINGER_SECONDS, "poll_interval_seconds": 3},
    ).build_id

    # Every upstream running at once is the state the command is defined
    # against: four unended executions on the build's ledger. Waiting for the state rather than sleeping -- a
    # stop issued before the containers are up would select nothing and
    # test nothing, while still passing.
    wait_until(
        lambda: all(task_status(task.id) == "running" for task in stopped + kept),
        build_id=build_id,
        timeout=RUNNING_TIMEOUT_SECONDS,
        what="all four upstreams to be running under this build",
    )

    # RUNNING is not enough, and the difference is STA-88. A task is
    # claimed -- which is what makes it RUNNING -- before its spawn reports
    # a call id, so between the two the row names no call. The command
    # lists such a row and marks it not stoppable, which is correct and is
    # what that issue fixed; but a scenario that stopped there would hand
    # ``selected_refs`` a null and sail through the Modal check below
    # having cancelled nothing. The end-to-end path this scenario exists
    # for only exists once the refs are on the rows, so that is the state
    # to wait for.
    wait_until(
        lambda: _stoppable_ids(build_id) >= {str(task.id) for task in stopped + kept},
        build_id=build_id,
        timeout=RUNNING_TIMEOUT_SECONDS,
        what="all four upstreams to have reported a call id",
    )

    # And the containers must be *inside* ``run()``, not merely spawned. On
    # v2 a worker whose container starts after its build was cancelled finds
    # its self-report start refused (the claim was released) and stops at
    # the start-of-attempt checkpoint without reporting -- correct, and the
    # first run of this scenario on v2 met exactly that for one of the kept
    # upstreams. A start is recorded three times per execution: the claim,
    # the tick's post-spawn start carrying the ref, and the worker's own
    # self-report from inside the container; the third is the state needed.
    def _worker_started(task) -> bool:
        return (
            sum(
                1
                for event in task_events(deployment, task.id, missing_ok=True)
                if event["event_type"] == "task_started" and event["report_applied"]
            )
            >= 3
        )

    wait_until(
        lambda: all(_worker_started(task) for task in stopped + kept),
        build_id=build_id,
        timeout=RUNNING_TIMEOUT_SECONDS,
        what="all four workers to report their own start from inside the container",
    )

    # The command itself, not a reimplementation of it. --json so the
    # assertion is on what it selected rather than on rendered text;
    # --yes because there is nobody to confirm.
    result = CliRunner(env={"COLUMNS": "240"}).invoke(
        app,
        ["stop", str(build_id), "--worker", ALT_WORKER, "--json", "--yes"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, f"{result.output}\n{describe(build_id)}"

    payload = json.loads(result.stdout)
    selected_refs = _refs(payload["selected"])
    excluded_refs = _refs(payload["excluded_by_filter"])

    assert sorted(selected_refs) == sorted(str(task.id) for task in stopped), (
        "The command selected a different set than the worker filter "
        f"names.\n{result.output}\n{describe(build_id)}"
    )
    assert sorted(excluded_refs) == sorted(str(task.id) for task in kept), (
        "The untouched upstreams were not reported as excluded, so the "
        "operator was not told what keeps running.\n"
        f"{result.output}\n{describe(build_id)}"
    )

    # The two assertions above already pin the listing's completeness --
    # between them they name all four rows, which is how STA-88 surfaced
    # here in the first place. What they do not pin is that the refs are
    # real, and without that the Modal check below passes on nulls having
    # cancelled nothing.
    assert all(selected_refs.values()), (
        "A selected execution carried no call id, so the Modal check "
        "below would pass without cancelling anything.\n"
        f"{result.output}\n{describe(build_id)}"
    )
    assert all(excluded_refs.values()), (
        "An excluded execution carried no call id, so the exclusivity "
        "check below would have nothing to probe.\n"
        f"{result.output}\n{describe(build_id)}"
    )

    # The excluded calls are still running, right now. Asserted before the
    # poll below rather than after it, because this is the claim with a
    # deadline: these containers end on their own eventually, and a check
    # made minutes later could not tell "never touched" from "finished".
    #
    # It therefore requires the upstreams' sleep to outlast everything
    # above — the two RUNNING waits and the command's own run. That is
    # already what ``WorkerFanIn``'s docstring says the duration is for,
    # and it fails safe: an upstream that outran its sleep is COMPLETED,
    # so the "all four running" wait times out first and says so.
    # Nothing else in the system can stop them -- the drain that used to is
    # gone (see the module docstring) -- so a dead one here means the
    # command reached past its own selection.
    assert all(_call_is_running(ref) for ref in excluded_refs.values()), (
        "An execution the filter excluded is no longer running, so the "
        "command stopped something it was told to leave alone.\n"
        f"{result.output}\n{describe(build_id)}"
    )

    # The selected calls are gone from Modal. Polled rather than asserted
    # once: the cancel reaches Modal's scheduler promptly, not atomically.
    # An exclusivity check as well as a liveness one, now that nothing but
    # this command could have ended them.
    wait_until(
        lambda: not any(_call_is_running(ref) for ref in selected_refs.values()),
        build_id=build_id,
        timeout=STOPPED_TIMEOUT_SECONDS,
        poll_interval=3.0,
        what="the selected Modal calls to stop running",
    )

    # Each selected call was reported stopped on the ledger.
    reported = {r["task_id"] for r in payload["stop_results"] if r["reported"]}
    assert reported == set(selected_refs), (
        f"Not every stopped call was reported stopped.\n{result.output}"
    )

    # And the build is cancelled, which is what released the claims. Before
    # the completion wait, because it is what makes that wait meaningful:
    # the excluded upstreams are CANCELLED by now, their claims released.
    assert payload["build_cancelled"] is True
    assert build_status(build_id) == "cancelled", describe(build_id)

    # The strong form. The excluded executions run out their sleep and
    # report their own completion -- late, since their claims were
    # released -- which ends them on the ledger with outcome ``completed``.
    # Nothing writes that end but the execution itself, and a cancelled
    # Modal call raises rather than reporting, so it proves they were never
    # touched. The task rows stay CANCELLED: a late report never moves one.
    #
    # Sized off the task's own sleep plus the skew this scenario has
    # already spent waiting, so a hang fails on the timeout rather than on
    # the tier's.
    kept_ids = {str(task.id) for task in kept}

    def _kept_outcomes() -> dict[str, str | None]:
        from stardag.registry import registry_provider

        rows = registry_provider.get().build_list_executions(
            build_id, include_ended=True
        )
        return {str(e.task_id): e.outcome for e in rows if str(e.task_id) in kept_ids}

    wait_until(
        lambda: set(_kept_outcomes().values()) == {"completed"},
        build_id=build_id,
        timeout=root.seconds + COMPLETION_SLACK_SECONDS,
        poll_interval=5.0,
        what="the excluded executions to run to their own completion untouched",
    )
    assert all(task_status(task.id) == "cancelled" for task in kept), (
        "A late completion moved a task its build had released.\n" + describe(build_id)
    )
    assert all(task.complete() for task in kept), (
        "The excluded executions reported completion without writing output."
    )
