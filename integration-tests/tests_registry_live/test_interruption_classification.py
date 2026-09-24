"""An execution the platform ended is reported, not mistaken for a restart.

STA-44, reproduced against a real worker and a real registry.

A worker that catches a platform interruption and asks to be resumed has to
decide one thing before it dies: *is anything going to restart this input?*
Only a preemption is. A function timeout and an explicit cancel both end the
call for good, and both arrive as ``InputCancellation`` — so a worker that
guesses "preemption" there reports nothing, nothing restarts the input, and
the task sits RUNNING behind a claim nobody will release until the claim
lapses. In the incident that was a day.

It used to guess from ``elapsed >= declared_timeout - 5s``, on a clock that
starts *inside* the container after boot, image load and deserialisation. It
therefore under-reads, and the guess was wrong by three seconds on an
86400-second worker.

**Why this scenario cancels the call rather than waiting for a timeout.**
The two are the same event as far as the worker can see — identical signal,
identical exception, identical message — and a cancel arrives on demand
where a timeout would cost this tier the worker's whole 600s budget. It is
also the *harder* case: elapsed is nowhere near the declared timeout, so the
old rule classifies it as a preemption with total confidence. Against the
fix, the exception chain says ``InputCancellation`` and the worker reports.

**And who classifies it.** The worker is the only party that can: a probe
sees that the call is gone, not what ended it, and Modal ends a cancelled
input the moment the cancel is issued — while the container is still
unwinding. A tick that called that a failure would spend an attempt on an
interruption and get the worker's report refused, which is STA-65. Both
halves are asserted below: the resumption happened, and no failure was
recorded on the way.

**Why the cancel comes from outside stardag.** A cancel stardag issued
itself would be one it has already recorded, and the registry would then
(correctly) refuse the interruption as a report about a task it just
cancelled — the authority rule. Cancelling the Modal call directly is a
platform-ended execution the registry has no other way to learn about,
which is exactly the class of event this path exists for.

What the old code does here: nothing is reported, the task stays RUNNING,
no tick is woken, and the build hangs to the pytest timeout.
"""

from __future__ import annotations

import uuid

import pytest

from stardag_integration_tests.registry_live._events import (
    current_execution,
    events_by,
    task_events,
)
from stardag_integration_tests.registry_live._guard import registry_live_guard
from stardag_integration_tests.registry_live._harness import Deployment
from stardag_integration_tests.registry_live._wait import (
    assert_trail_complete,
    describe,
    task_status,
    tick_summaries,
    wait_for_task_status,
    wait_for_terminal,
    wait_until,
)

registry_live_guard()

pytestmark = [
    pytest.mark.registry_live,
    pytest.mark.timeout(900),
]

# Long enough that the task is still asleep when the harness has noticed it
# is RUNNING and cancelled its call — the sleep is the window the whole
# scenario happens inside. Short enough that the *resumed* execution, which
# starts the sleep again from zero, does not dominate the run.
SLEEP_SECONDS = 45

# Ticks exit quickly so the interruption is met by a woken tick rather than
# by one that happened to still be lingering.
TICK_LINGER_SECONDS = 10

BUILD_TIMEOUT_SECONDS = 600

# Time for the first worker to get a container and report itself RUNNING
# with the call id the cancel needs.
RUNNING_TIMEOUT_SECONDS = 300


# How many TASK_STARTED events one reactive execution records, and the
# reason this scenario counts them.
#
# The ref alone is **not** a readiness signal: the tick writes it the moment
# `submit_detached` returns, which is when Modal *accepted* the spawn, not
# when a container exists. Cancelling then can end the input before the task
# body ever runs, so the interruption is raised nowhere the task can catch
# it, nothing is reported, and the scenario fails having tested nothing.
#
# It is a real race and it fired: passing alone, failing in the concurrent
# run, where thirteen scenarios contend for cold containers and the window
# between "spawn accepted" and "task body running" is at its widest.
#
# The third start is the worker's own self-report, from inside the
# container — the first evidence that stardag code is executing there. The
# reactive path records three per execution: the claiming start, the tick's
# ref-recording start, then this one.
_STARTS_BEFORE_THE_TASK_BODY_RUNS = 3


def _worker_has_started(deployment: Deployment, task_id) -> bool:
    """Whether the worker itself has reported starting — see above."""
    events = task_events(deployment, task_id, missing_ok=True)
    starts = [
        e for e in events if e["event_type"] == "task_started" and e["report_applied"]
    ]
    return len(starts) >= _STARTS_BEFORE_THE_TASK_BODY_RUNS


def _executor_ref(deployment: Deployment, task_id) -> str | None:
    """The Modal call id the registry recorded for this task's current
    execution, if any (on the execution ledger, not the task row)."""
    current = current_execution(deployment, task_id)
    return current.executor_ref if current is not None else None


def _resumption_reports(summaries: list[dict]) -> str:
    """What the ticks counted, for a failure message. Never an assertion.

    A summary exists only if the tick that would have written it lived
    long enough, so its absence says something about the reporter rather
    than about the resumption.
    """
    counted = sum(summary.get("interruptions_restarted", 0) for summary in summaries)
    return (
        f"  [diagnostic] ticks report {counted} interruption restart(s) "
        f"across {len(summaries)} retained summaries."
    )


def test_a_cancelled_input_is_reported_rather_than_read_as_a_preemption(
    deployment: Deployment,
) -> None:
    import modal

    from stardag_integration_tests.registry_live.dag_app import app
    from stardag_integration_tests.registry_live.tasks import Resumable

    salt = uuid.uuid4().hex
    root = Resumable(salt=salt, seconds=SLEEP_SECONDS)

    triggered = app.build_trigger(
        root,
        reactive=True,
        tick_kwargs={
            "linger_seconds": TICK_LINGER_SECONDS,
            "poll_interval_seconds": 3,
        },
    )
    build_id = triggered.build_id

    wait_for_task_status(
        root.id,
        expected="running",
        build_id=build_id,
        timeout=RUNNING_TIMEOUT_SECONDS,
    )
    ref = wait_until(
        lambda: _executor_ref(deployment, root.id),
        build_id=build_id,
        timeout=RUNNING_TIMEOUT_SECONDS,
        what=f"task {root.id} to record its Modal call id",
    )
    # ...and then for the container to be *in* the task body, which the ref
    # does not say. See _STARTS_BEFORE_THE_TASK_BODY_RUNS.
    wait_until(
        lambda: _worker_has_started(deployment, root.id),
        build_id=build_id,
        timeout=RUNNING_TIMEOUT_SECONDS,
        what=(
            f"the worker for task {root.id} to report its own start, which "
            "is the first evidence a container is running the task body"
        ),
    )

    # The platform ends the execution. From inside the container this is
    # indistinguishable from the function timeout firing.
    modal.FunctionCall.from_id(ref).cancel()

    # The assertion the incident is about. The worker is the only thing that
    # knows this execution has ended: it is dying, nothing else is watching,
    # and the tick that spawned it lingered out long ago.
    def interruption_recorded() -> list[dict] | None:
        found = task_events(deployment, root.id)
        types = [event["event_type"] for event in found]
        return found if "task_interrupted" in types else None

    events = wait_until(
        interruption_recorded,
        build_id=build_id,
        timeout=RUNNING_TIMEOUT_SECONDS,
        what=(
            f"task {root.id} to record an interruption. Without one the "
            "worker classified a cancelled input as a preemption and "
            "reported nothing, which is STA-44"
        ),
    )
    assert not any(event["event_type"] == "task_preempted" for event in events), (
        "The worker recorded a preemption for an input nothing was going to "
        "restart. The exception chain carried an InputCancellation, so the "
        "classification should not have consulted the clock at all.\n"
        + describe(build_id)
    )

    # ...and the recovery the report buys: the interruption released the
    # claim and woke a tick, which resumed the task on a fresh container.
    status = wait_for_terminal(build_id, timeout=BUILD_TIMEOUT_SECONDS)
    assert status == "completed", describe(build_id)

    summaries = tick_summaries(build_id)
    assert_trail_complete(build_id, summaries)

    # The resumption was a *resumption*, not a retry. An interruption is
    # bounded by its own budget and deliberately spends no attempt — a task
    # designed to be killed and resumed until it converges would otherwise
    # fail the build for the one reason it was built to survive.
    assert task_status(root.id) == "completed", describe(build_id)

    # The resumption, from the task's own event log rather than from a
    # tick's count of them. Same reasoning as the rollover scenario
    # (STA-87): a tick that resumes a task and is preempted before
    # reporting leaves no count, and "no tick said so" would then be
    # indistinguishable from "it never happened" -- which is the one
    # reading that must stay falsifiable here. A start recorded *after*
    # the interruption is the registry's own evidence that the task ran
    # again, and no tick has to survive for it to be true.
    # Strict, and scoped to this build. ``task_events`` answers across
    # every build that has touched the task, while the counter this
    # replaces was build-scoped -- and ``missing_ok`` would turn a task
    # the registry has never heard of into an empty list, which is the
    # answer an absence assertion reads as proof of correct behaviour.
    after = events_by(task_events(deployment, root.id), build_id)
    types = [event["event_type"] for event in after]
    assert "task_interrupted" in types, (
        f"The interruption is no longer in the event log: {types}\n"
        + describe(build_id)
    )
    interrupted_at = types.index("task_interrupted")
    assert "task_started" in types[interrupted_at + 1 :], (
        "The interrupted task was never started again, so the build "
        "completed by some other route than the one under test. Events: "
        f"{types}\n" + _resumption_reports(summaries) + "\n" + describe(build_id)
    )

    # The other route, named, because it is the one this used to take
    # under load (STA-65). A tick probing the cancelled call sees it gone
    # before the worker's report lands, and calling that a failure spends
    # an attempt, retries the task, and gets the worker's report refused —
    # the build still completes, so only the accounting says which
    # happened. Nothing in this scenario should fail: the execution ended
    # because the platform was asked to end it, and the worker said so.
    # Across *both* the build's tasks, not just the interrupted one: the
    # counter this replaces (`failed_recorded`) was build-wide, and a
    # failure recorded on the upstream and then retried would have been
    # caught by it. A `task_failed` event is the durable form of the same
    # thing -- it is written when the failure is recorded, and a later
    # retry appends rather than erases.
    for task in (root.requires(), root):
        task_types = [
            event["event_type"]
            for event in events_by(task_events(deployment, task.id), build_id)
        ]
        assert "task_failed" not in task_types, (
            f"A failure was recorded for task {task.id}, whose execution the "
            "worker reported as an interruption — the probe classified the "
            "cancelled input before the report landed (STA-65). Events: "
            f"{task_types}\n" + describe(build_id)
        )

    deployment.assert_same_container()
