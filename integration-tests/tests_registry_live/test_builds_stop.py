"""``stardag builds stop`` stops what it selected, and only that.

The command's contract has three parts and they are only meaningful
together, against real containers:

1. The list is read off the task rows *while the build still holds their
   claims*, so it names this build's executions and nobody else's.
2. The calls it selected are cancelled -- actually cancelled, in Modal,
   not merely recorded as cancelled in the registry.
3. The build is cancelled **afterwards**, releasing the claims. Anything a
   filter excluded keeps running, and its result still lands.

Every one of those is invisible to a unit test. The selection rules are
pinned in ``tests/test__cli/test_stop.py``; what cannot be pinned there is
whether a container actually died, which is the entire question.

**How "it died" is established.** Both groups of upstreams start within
seconds of each other and the stopped group sleeps for *less* time than
the kept group. So a stopped container that survived its cancellation
would reach completion first, and COMPLETED is sticky -- the row would say
so no matter what happened afterwards. Waiting for the kept group to
complete and then finding the stopped group still not completed is
therefore evidence, not a race won.

Against a command that cancelled the build first, this fails from both
ends at once: the list would be taken after the claims were released, and
the stopped containers would run on to completion.
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

# The build has to stay resident long enough to put four containers on
# workers and for the kept pair to finish afterwards.
LINGER_SECONDS = 240

RUNNING_TIMEOUT_SECONDS = 420
COMPLETION_TIMEOUT_SECONDS = 420


def _ids(tasks) -> list[str]:
    return [str(task.id) for task in tasks]


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
    assert sorted(task["task_id"] for task in payload["selected"]) == sorted(
        _ids(stopped)
    ), (
        "The command stopped a different set than the worker filter names.\n"
        f"{result.output}\n{describe(build_id)}"
    )
    assert sorted(task["task_id"] for task in payload["excluded_by_filter"]) == sorted(
        _ids(kept)
    ), (
        "The untouched upstreams were not reported as excluded, so the "
        "operator was not told what keeps running.\n"
        f"{result.output}\n{describe(build_id)}"
    )

    # The build is cancelled after the calls, which is what releases the
    # claims. Checked before the long wait below so a failure here is not
    # reported as a timeout.
    assert build_status(build_id) == "cancelled", describe(build_id)

    # The kept containers run on with their claims released and report
    # their results anyway -- COMPLETED is sticky, so a completion that
    # lands after the cancel still wins.
    wait_until(
        lambda: all(task_status(task.id) == "completed" for task in kept),
        build_id=build_id,
        timeout=COMPLETION_TIMEOUT_SECONDS,
        what="the upstreams the filter excluded to finish on their own",
    )

    # ...and by then a surviving stopped container would have completed
    # too: it started at the same time and sleeps for less. Anything but
    # COMPLETED here means its container is gone.
    for task in stopped:
        status = task_status(task.id)
        assert status != "completed", (
            f"Task {task.id} was selected by 'builds stop' and completed "
            "anyway, so its Modal call outlived the cancellation -- the "
            "one thing this command has to guarantee. It sleeps for less "
            "than the tasks that were left alone, and those have already "
            f"finished.\n{describe(build_id)}"
        )
