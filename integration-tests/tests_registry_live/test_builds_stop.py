"""``stardag builds stop`` selects, stops and cancels, in that order.

What only a live run can show is that the command reaches Modal at all:
that it reads a real registry's task rows, picks the executions its
filters name, ends the corresponding function calls, and cancels the build
afterwards. The selection *rules* are pinned in
``tests/test__cli/test_stop.py``, including the one that matters most --
that only the selected executions are handed to the canceller.

**What this scenario deliberately does not assert, and why.** The obvious
companion claim -- that the executions a filter excluded are left running
-- is not observable here yet. A tick of a *terminal* build still runs the
automated cancel drain, which cancels every execution the build started,
excluded ones included. An earlier version of this scenario asserted the
excluded calls were still live immediately after the command returned; it
passed twice locally and failed in CI, where a lingering tick polling
every three seconds noticed the cancelled build first and drained all four
(``cancelled_refs=4`` in its summary). That is a race against a component
this issue does not change, and a flaky scenario is worth less than a
narrow one.

The drain is STA-81's to delete, immediately after this merges. **When it
goes, this scenario should grow the assertion back**, and in its strongest
form: wait for the excluded upstreams to reach COMPLETED, which proves
both that they were never touched and that a result landing after the
build was cancelled still counts. Until then that guarantee rests on the
unit test that pins exactly which executions reach ``cancel_modal_calls``.

Note the same drain also weakens the "the selected calls stopped" check
below into a liveness test rather than an exclusivity one: it would have
stopped them too. It is kept because it is the only place the path from
CLI to a real Modal cancellation is exercised end to end, and because it
fails loudly if that path breaks.
"""

from __future__ import annotations

import json
import uuid

import pytest

from stardag_integration_tests.registry_live._guard import registry_live_guard
from stardag_integration_tests.registry_live._wait import (
    build_status,
    describe,
    task_status,
    wait_until,
)

registry_live_guard()

pytestmark = [
    pytest.mark.registry_live,
    pytest.mark.timeout(900),
]

# Long enough for the tick to put four containers on two workers and stay
# resident while they start.
LINGER_SECONDS = 240

RUNNING_TIMEOUT_SECONDS = 420
# A cancel reaches Modal's scheduler promptly, but "promptly" is not
# "synchronously" -- poll rather than assert once.
STOPPED_TIMEOUT_SECONDS = 120


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


def test_stop_cancels_only_the_selected_workers_calls() -> None:
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
    # against: the claims are held, so the task rows name this build's
    # executions exactly. Waiting for the state rather than sleeping -- a
    # stop issued before the containers are up would select nothing and
    # test nothing, while still passing.
    wait_until(
        lambda: all(task_status(task.id) == "running" for task in stopped + kept),
        build_id=build_id,
        timeout=RUNNING_TIMEOUT_SECONDS,
        what="all four upstreams to be running under this build",
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

    # The selected calls are gone from Modal. Polled rather than asserted
    # once: the cancel reaches Modal's scheduler promptly, not atomically.
    #
    # A liveness check rather than an exclusivity one -- the terminal
    # build's cancel drain would stop these too (see the module docstring)
    # -- but it is the only place the path from this CLI to a real Modal
    # cancellation is exercised, and it fails loudly if that path breaks.
    wait_until(
        lambda: not any(_call_is_running(ref) for ref in selected_refs.values()),
        build_id=build_id,
        timeout=STOPPED_TIMEOUT_SECONDS,
        poll_interval=3.0,
        what="the selected Modal calls to stop running",
    )

    # And the build is cancelled, which is what released the claims. Last,
    # because it is the only step that is also visible from the registry --
    # a failure here should not mask the two above.
    assert build_status(build_id) == "cancelled", describe(build_id)
