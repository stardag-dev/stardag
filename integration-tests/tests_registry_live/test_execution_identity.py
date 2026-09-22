"""A retried claim is the same attempt asking again, against the real arbiter.

The registry client retries a POST whose response never arrived, so a
claiming start that *succeeded* can be delivered twice. Refused, the
second delivery tells the worker that somebody else holds the task — a
correct reason to stand down, and it does, while itself holding the
claim, leaving the task claimed and not running until the claim expires.

The claim is taken before the spawn, so there is no executor ref to
identify the attempt by and never was; ``execution_id`` is the identity
that exists anyway. What makes this worth a live scenario rather than
only a unit test is the arbiter: a real Postgres row held ``FOR UPDATE``
by the deployed API, reached over the network, rather than a double
written from the same understanding as the code it stands in for.

Driven directly rather than through a spawned build. Reproducing a lost
response against a live API is not something a test can arrange, and the
delivery the client would repeat is this exact call — so a worker would
add a second thing that can fail without adding anything to the
property under test.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from stardag_integration_tests.registry_live._events import task_events
from stardag_integration_tests.registry_live._guard import registry_live_guard
from stardag_integration_tests.registry_live._wait import (
    describe,
    find_task,
    task_status,
    wait_for_task_status,
    wait_for_terminal,
    wait_until,
)

registry_live_guard()

pytestmark = [
    pytest.mark.registry_live,
    pytest.mark.timeout(900),
]

# Long enough that the shared task is still RUNNING under A when the
# cascade lands, and that B's own copy is still running when A's
# superseded start arrives.
SHARED_SLEEP_SECONDS = 60

A_LINGER_SECONDS = 30
B_LINGER_SECONDS = 180

STATUS_TIMEOUT_SECONDS = 300
BUILD_TIMEOUT_SECONDS = 600

# Far longer than the scenario waits, on purpose: a worker that ignored
# the cancel is still sleeping when the poll below gives up, so the
# failure is a timeout rather than a slow pass.
COOPERATIVE_SLEEP_SECONDS = 600
# The default check interval is 30s and the task's slice is 2s, so a
# cooperative exit lands inside ~35s of the cancel. The rest is Modal's
# own latency in reporting a call as over.
COOPERATIVE_EXIT_TIMEOUT_SECONDS = 180


_STILL_RUNNING = object()


def _call_outcome(ref: str) -> object:
    """How a function call ended, or ``_STILL_RUNNING`` while it has not.

    A zero-timeout ``get`` raises the builtin ``TimeoutError`` while the
    call is in flight, and otherwise gives the call's own result or
    re-raises the exception that ended it.

    The *exception* is what this scenario turns on, not merely the fact
    that the call is over. There is still a cancel drain in the reactive
    tick (STA-81 deletes it), so a lingering tick can stop a container of
    its own accord — and a scenario that only checked "the call is gone"
    would pass on that and prove nothing about the worker. A worker that
    stopped itself raises ``ExecutionCancelled`` out of the container, and
    nothing else in the system produces that.
    """
    import modal

    try:
        return modal.FunctionCall.from_id(ref).get(timeout=0)
    except TimeoutError:
        return _STILL_RUNNING
    except Exception as e:
        return e


def test_a_retried_claim_is_granted_to_the_attempt_that_won_it() -> None:
    from stardag.registry import registry_provider
    from stardag_integration_tests.registry_live.tasks import get_range

    salt = uuid.uuid4().hex
    task = get_range(limit=3, salt=salt)
    registry = registry_provider.get()

    build_id = registry.build_start([task], description="STA-50 retried claim")
    registry.task_register(build_id, task)
    execution_id = uuid.uuid4()

    async def _claim(eid: uuid.UUID):
        return await registry.task_start_claim_aio(build_id, task, execution_id=eid)

    first = asyncio.run(_claim(execution_id))
    assert first.started, f"the first claim was denied: {first}"
    assert first.execution_id == str(execution_id), (
        "The registry did not echo the claim identity, so it predates the "
        f"field and this scenario proves nothing: {first}"
    )

    retried = asyncio.run(_claim(execution_id))
    assert retried.started, (
        "A retried claim was refused, so a worker holding the claim would "
        f"stand down from its own task: {retried}"
    )

    second_attempt = asyncio.run(_claim(uuid.uuid4()))
    assert not second_attempt.started, (
        "A genuine second attempt was granted while the claim was live, so "
        f"the identity has cost the exactly-once guarantee: {second_attempt}"
    )
    assert second_attempt.denied_reason == "already_running"
    assert second_attempt.execution_id == str(execution_id), (
        "The denial did not name the claim that holds the task."
    )


# --- The worker carries the identity, and both rules that reads it -------


def test_a_superseded_workers_start_cannot_take_the_task_back() -> None:
    """STA-49, produced rather than simulated, against the real arbiter.

    Build A claims and runs the shared task; a cascading cancel releases
    A's claim -- which is what lets the next build have the task, and is
    exactly how the production incident began -- and B claims and runs its
    own execution. From that moment A's execution is superseded while A's
    container is, as far as anything here knows, still going: the server
    cannot stop anything.

    **What is synthesised is only the last step**, A's worker checking in.
    A real one would need a Modal preemption whose restart arrives after
    the claim lapsed, which cannot be forced from a test. The start itself
    is an ordinary non-claiming ``task_start``, byte for byte what that
    restarted worker sends, and the registry cannot tell the difference --
    which is the point. So this pins the *server rule under genuine
    concurrent takeover*; it is not an end-to-end preemption restart.
    """
    from stardag.exceptions import APIError
    from stardag.registry import registry_provider
    from stardag_integration_tests.registry_live.dag_app import app
    from stardag_integration_tests.registry_live.tasks import (
        get_range,
        get_sum,
        slow,
        square,
    )

    salt = uuid.uuid4().hex
    leaf = get_range(limit=4, salt=salt)
    shared = slow(values=leaf, seconds=SHARED_SLEEP_SECONDS)
    shared_id = str(shared.id)
    registry = registry_provider.get()

    build_a = app.build_trigger(
        get_sum(integers=shared),
        reactive=True,
        tick_kwargs={"linger_seconds": A_LINGER_SECONDS, "poll_interval_seconds": 3},
    ).build_id

    wait_for_task_status(
        shared.id,
        expected="running",
        build_id=build_a,
        timeout=STATUS_TIMEOUT_SECONDS,
    )
    # The identity A's tick minted, read back off the row rather than
    # guessed: this is the value A's worker would repeat, and the whole
    # rule turns on it being the one the task no longer holds.
    a_execution = find_task(shared_id, task_name="Slow").latest_execution_id
    assert a_execution is not None, (
        "A's claim recorded no execution identity, so this scenario would "
        "pass for the wrong reason -- a start with nothing to compare is "
        "accepted by design.\n" + describe(build_a)
    )

    cancelled = registry.build_cancel(build_a, cascade=True)
    assert cancelled is not None
    assert shared_id in cancelled.cascaded_task_ids, (
        "The cascade did not release the shared task's claim, so there is "
        f"no takeover to supersede anything.\n{describe(build_a)}"
    )

    build_b = app.build_trigger(
        square(values=shared, offset=11),
        reactive=True,
        tick_kwargs={"linger_seconds": B_LINGER_SECONDS, "poll_interval_seconds": 3},
    ).build_id
    # Wait for the owner to be B, not merely for the task to be RUNNING:
    # A's own worker is alive and still talking, so a status-only wait can
    # return on A's execution.
    wait_until(
        lambda: task_status(shared.id) == "running"
        and find_task(shared_id, task_name="Slow").latest_status_build_id == build_b,
        build_id=build_b,
        timeout=STATUS_TIMEOUT_SECONDS,
        what=f"build {build_b} to claim the shared task",
    )
    b_execution = find_task(shared_id, task_name="Slow").latest_execution_id
    assert b_execution is not None and b_execution != a_execution

    # A's restarted worker checks in, naming the execution it is.
    with pytest.raises(APIError) as refused:
        registry.task_start(
            build_a,
            shared,
            executor="modal",
            executor_ref="fc-a-restarted",
            execution_id=a_execution,
        )

    assert refused.value.status_code == 409, refused.value
    assert (refused.value.payload or {}).get("error_code") == "execution_superseded", (
        f"refused for the wrong reason: {refused.value.payload!r}"
    )

    row = find_task(shared_id, task_name="Slow")
    assert row.latest_status_build_id == build_b, (
        "The superseded start took the task back from the build that holds "
        "it, which is the two-executions-of-one-task outcome claims exist "
        f"to prevent.\n{describe(build_a)}\n{describe(build_b)}"
    )
    assert row.latest_execution_id == b_execution
    assert row.latest_executor_ref != "fc-a-restarted", (
        "The superseded start's reference was recorded over the live "
        "holder's, so a later cancel would address the wrong container."
    )

    status_b = wait_for_terminal(build_b, timeout=BUILD_TIMEOUT_SECONDS)
    assert status_b == "completed", (
        f"Build B did not finish.\n{describe(build_a)}\n{describe(build_b)}"
    )


def test_a_cancelled_builds_worker_stops_itself(deployment) -> None:
    """Cooperative cancellation, with nothing reaching into the container.

    Nothing calls Modal here. The cancel releases the task's claim and
    stops; the only thing that can end this container is the container,
    asking at a point its own author chose -- ``Cooperative`` calls
    ``stardag.cancellation_requested()`` between sleeps, which is the
    surface no unit test can exercise against a real registry.

    **The build is cancelled**, which is the case this issue names and
    the one a human actually causes. It was written against a *task*
    cancel while the tick's cancel drain still existed: a lingering tick
    would drain a cancelled build's executions and the scenario passed
    with ``cancelled_refs=1`` in the tick summary, having proved nothing
    about the worker at all. STA-81 deleted the drain, so the container
    now has nothing but its own checkpoint to end it, and the stronger
    case is the testable one.

    Note which of the endpoint's three answers this exercises. A build
    cancel without ``cascade`` leaves the task RUNNING under this very
    execution, so neither ``task_cancelled`` nor ``superseded`` can fire
    -- the worker stops on ``build_not_running``, and it is the only
    reason available. The task-status and supersession answers have their
    own unit coverage.

    Three assertions, failing from three directions. The call being
    **gone** catches a worker that ignored the answer -- it would still be
    sleeping, far past the poll's timeout. The exception being
    ``ExecutionCancelled`` catches anything that reached into the
    container instead, since nothing else in the system raises it. The
    target being **absent** catches a worker that noticed and exited
    untidily: returning normally from ``run()`` writes the output and is
    reported as a completion, which is exactly what "exits cleanly" rules
    out.
    """
    from stardag.exceptions import ExecutionCancelled
    from stardag.registry import registry_provider
    from stardag_integration_tests.registry_live.dag_app import app
    from stardag_integration_tests.registry_live.tasks import (
        cooperative,
        get_range,
        get_sum,
    )

    salt = uuid.uuid4().hex
    leaf = get_range(limit=4, salt=salt)
    worker_task = cooperative(values=leaf, seconds=COOPERATIVE_SLEEP_SECONDS)
    registry = registry_provider.get()

    build_id = app.build_trigger(
        get_sum(integers=worker_task),
        reactive=True,
        tick_kwargs={"linger_seconds": 30, "poll_interval_seconds": 3},
    ).build_id

    wait_for_task_status(
        worker_task.id,
        expected="running",
        build_id=build_id,
        timeout=STATUS_TIMEOUT_SECONDS,
    )

    # Then wait for the container to have **started running the task**, not
    # merely to have been spawned, and the difference decides which
    # checkpoint this scenario exercises.
    #
    # A start is recorded three times for one execution: the claim (no
    # ref), the tick's post-spawn start (the ref), and the worker's own
    # self-report from inside the container. Cancelling after the second
    # means the cancel is already in place when the container starts, and
    # the start-of-attempt checkpoint catches it before ``run()`` is ever
    # entered -- true, useful, and already covered by unit tests. Waiting
    # for the third puts the worker *inside* its loop, so what stops it is
    # ``stardag.cancellation_requested()``, which is the surface this
    # scenario exists for and the one no unit test can exercise against a
    # real registry.
    #
    # Wait on the state you need, never on the one that usually
    # accompanies it -- both halves of that cost a red run here.
    def _starts() -> int:
        return sum(
            1
            for event in task_events(deployment, worker_task.id, missing_ok=True)
            if event["event_type"] == "task_started"
        )

    wait_until(
        lambda: _starts() >= 3,
        build_id=build_id,
        timeout=STATUS_TIMEOUT_SECONDS,
        what="the worker to report its own start from inside the container",
    )
    ref = find_task(str(worker_task.id), task_name="Cooperative").latest_executor_ref
    assert ref is not None

    registry.build_cancel(build_id)

    wait_until(
        lambda: _call_outcome(ref) is not _STILL_RUNNING,
        build_id=build_id,
        timeout=COOPERATIVE_EXIT_TIMEOUT_SECONDS,
        what=(
            f"execution {ref} to end after its build was cancelled "
            f"(it would otherwise sleep for {COOPERATIVE_SLEEP_SECONDS}s)"
        ),
    )

    outcome = _call_outcome(ref)
    assert isinstance(outcome, ExecutionCancelled), (
        "The execution ended, but not by stopping itself. Only the worker "
        "raises ExecutionCancelled; anything else here means something "
        "reached into the container, which is what cooperative "
        f"cancellation exists to stop needing.\nGot: {outcome!r}\n"
        f"{describe(build_id)}"
    )

    assert not worker_task.complete(), (
        "The cancelled execution wrote its output, so it did not exit at a "
        "checkpoint -- it either ran to completion or returned normally "
        f"from run().\n{describe(build_id)}"
    )
    assert task_status(worker_task.id) != "completed", (
        f"A cancelled execution reported a completion.\n{describe(build_id)}"
    )
