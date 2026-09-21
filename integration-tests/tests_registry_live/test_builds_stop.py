"""``stardag builds stop`` stops what it selected, and only that.

The command's contract has three parts and they are only meaningful
together, against real containers:

1. The list is read off the task rows *while the build still holds their
   claims*, so it names this build's executions and nobody else's.
2. The calls it selected are cancelled -- actually cancelled, in Modal,
   not merely recorded as cancelled in the registry.
3. The build is cancelled **afterwards**, releasing the claims.

The selection rules are pinned in ``tests/test__cli/test_stop.py``; what
cannot be pinned there is whether a container actually died, which is the
entire question. So the assertion is made against Modal itself: after the
command returns, the calls it named are asked whether they are still
running, and so are the calls it left alone.

**Why Modal and not the task rows.** The registry says CANCELLED for every
one of these the moment the build is cancelled, whether or not anything
stopped -- that is the whole reason this command exists. The one place the
difference between "stopped" and "recorded as stopped" is visible is the
backend, so that is where it is looked for.

**Why the window is narrow, for now.** A tick of a terminal build still
runs the automated cancel drain, which cancels *every* execution the build
started -- including the ones a filter deliberately left alone. That is
STA-81's to delete, after this merges, and until then "the rest run on to
completion" is not observable end to end. The probe below is taken at the
one moment that is unambiguous: immediately after the command returns,
before any tick can reach the build. When the drain goes, this scenario
should grow the other half -- wait for the excluded upstreams to reach
COMPLETED, which proves both that they were untouched and that a result
landing after the cancel still counts.

Against a command that cancelled the build first, this fails from both
ends at once: the list would be taken after the claims were released, and
the stopped containers would still be running when it returned.
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

    # The half a unit test cannot reach: the containers the command left
    # alone are still running, right now, with their claims already
    # released. Asserted before the wait below, because it is the one that
    # is only true in this window.
    still_running = {
        task_id: _call_is_running(ref) for task_id, ref in excluded_refs.items()
    }
    assert all(still_running.values()), (
        "An execution the worker filter excluded is no longer running on "
        "Modal, so 'builds stop' stopped more than it selected.\n"
        f"{still_running}\n{describe(build_id)}"
    )

    # ...and the ones it did select are gone. Polled rather than asserted
    # once: the cancel reaches Modal's scheduler promptly, not atomically.
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
