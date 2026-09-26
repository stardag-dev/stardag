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
from datetime import datetime, timezone

import pytest

from stardag_integration_tests.registry_live._events import (
    current_execution,
    ledger,
    task_events,
)
from stardag_integration_tests.registry_live._guard import registry_live_guard
from stardag_integration_tests.registry_live._harness import Deployment
from stardag_integration_tests.registry_live._wait import (
    describe,
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

# Long enough that the shared task is still RUNNING under A when A's
# cancel lands, and that B's own copy is still running when A's
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


@pytest.mark.budget(20)
def test_a_retried_claim_is_granted_to_the_attempt_that_won_it() -> None:
    from stardag.build._deployment import local_deployment_id_aio
    from stardag.build._registration import new_id, registration_item
    from stardag.exceptions import APIError
    from stardag.registry import registry_provider
    from stardag_integration_tests.registry_live.tasks import get_range

    salt = uuid.uuid4().hex
    task = get_range(limit=3, salt=salt)
    task_id = str(task.id)
    registry = registry_provider.get()

    # One sealed plan holding the task, as a build's static phase leaves it.
    build = registry.build_create(
        root_task_ids=[task_id], description="STA-50 retried claim"
    )
    observed_at = datetime.now(timezone.utc)

    def _item(declared_upstreams):
        return registration_item(
            task,
            declared_upstreams=declared_upstreams,
            observed_complete=False,
            observed_at=observed_at,
        )

    # Roots are admitted unexpanded; the walk's chunk expands them.
    plan = registry.plan_create(
        build.id,
        plan_id=new_id(),
        deployment_id=asyncio.run(local_deployment_id_aio(registry)),
        settings={},
        roots=[_item(None)],
    )
    registry.plan_register_members(plan.id, [_item([])])
    registry.plan_seal(plan.id)
    execution_id = uuid.uuid4()

    async def _claim(eid: uuid.UUID):
        return await registry.member_start_aio(
            plan.id, task_id, execution_id=eid, claim=True
        )

    first = asyncio.run(_claim(execution_id))
    assert first.execution_id == execution_id, (
        "The registry did not echo the claim identity, so this scenario "
        f"proves nothing: {first}"
    )

    # A retried granted start is a no-op, never a refusal.
    retried = asyncio.run(_claim(execution_id))
    assert retried.execution_id == execution_id, (
        "A retried claim was not answered as the attempt that holds it, so "
        f"a worker holding the claim would stand down from its own task: "
        f"{retried}"
    )

    with pytest.raises(APIError) as second_attempt:
        asyncio.run(_claim(uuid.uuid4()))
    assert second_attempt.value.status_code == 409, second_attempt.value
    assert second_attempt.value.code == "task_already_running", (
        "A genuine second attempt was not refused as a live claim: "
        f"{second_attempt.value.payload!r}"
    )
    # The claim still names the attempt that won it.
    assert registry.task_get(task_id).execution_id == execution_id


# --- The worker carries the identity, and both rules that reads it -------


@pytest.mark.budget(125)
def test_a_superseded_workers_start_cannot_take_the_task_back(
    deployment: Deployment,
) -> None:
    """STA-49, produced rather than simulated, against the real arbiter.

    Build A claims and runs the shared task; cancelling A releases A's
    claim -- which is what lets the next build have the task, and is
    exactly how the production incident began -- and B claims and runs its
    own execution. From that moment A's execution is superseded while A's
    container is, as far as anything here knows, still going: the server
    cannot stop anything.

    **What is synthesised is only the last step**, A's worker checking in.
    A real one would need a Modal preemption whose restart arrives after
    the claim lapsed, which cannot be forced from a test. The start itself
    is an ordinary non-claiming ``member_start``, byte for byte what that
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
    a_execution = registry.task_get(shared_id).execution_id
    assert a_execution is not None, (
        "A's claim recorded no execution identity, so this scenario would "
        "pass for the wrong reason -- a start with nothing to compare is "
        "accepted by design.\n" + describe(build_a)
    )
    plan_a = registry.build_get_frontier(build_a).plan_id
    assert plan_a is not None, describe(build_a)

    registry.build_cancel(build_a)
    assert task_status(shared.id) == "cancelled", (
        "The cancel did not release the shared task's claim, so there is "
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
    def _held_by_b() -> bool:
        current = current_execution(deployment, shared_id)
        return (
            current is not None
            and current.status == "running"
            and current.build_id == str(build_b)
        )

    wait_until(
        _held_by_b,
        build_id=build_b,
        timeout=STATUS_TIMEOUT_SECONDS,
        what=f"build {build_b} to claim the shared task",
    )
    b_execution = registry.task_get(shared_id).execution_id
    assert b_execution is not None and b_execution != a_execution

    # A's restarted worker checks in, naming the execution it is.
    with pytest.raises(APIError) as refused:
        registry.member_start(
            plan_a,
            shared_id,
            execution_id=a_execution,
            claim=False,
            executor="modal",
            executor_ref="fc-a-restarted",
        )

    assert refused.value.status_code == 409, refused.value
    assert refused.value.code == "execution_not_current", (
        f"refused for the wrong reason: {refused.value.payload!r}"
    )

    current = current_execution(deployment, shared_id)
    assert current is not None and current.build_id == str(build_b), (
        "The superseded start took the task back from the build that holds "
        "it, which is the two-executions-of-one-task outcome claims exist "
        f"to prevent.\n{describe(build_a)}\n{describe(build_b)}"
    )
    assert current.execution_id == str(b_execution)
    refs = {
        e["id"]: e.get("executor_ref")
        for build in (build_a, build_b)
        for e in ledger(deployment, build)
    }
    assert "fc-a-restarted" not in refs.values(), (
        "The superseded start's reference was recorded on the ledger, so a "
        "later stop would address a container that is not the holder's."
    )

    status_b = wait_for_terminal(build_b, timeout=BUILD_TIMEOUT_SECONDS)
    assert status_b == "completed", (
        f"Build B did not finish.\n{describe(build_a)}\n{describe(build_b)}"
    )


@pytest.mark.budget(80)
def test_a_cancelled_builds_worker_stops_itself(deployment: Deployment) -> None:
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

    What the checkpoint reads: the build's unended executions (``GET
    /builds/{id}/executions``). A cancel releases the build's claims, so
    this execution's ``claim_released_at`` is set in the same transaction
    that makes the build CANCELLED -- ``still_wanted`` is false, and the
    worker stops at its next checkpoint. Nothing takes the task over here;
    that answer has its own unit coverage.

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
            if event["event_type"] == "task_started" and event["report_applied"]
        )

    wait_until(
        lambda: _starts() >= 3,
        build_id=build_id,
        timeout=STATUS_TIMEOUT_SECONDS,
        what="the worker to report its own start from inside the container",
    )
    current = current_execution(deployment, worker_task.id)
    ref = current.executor_ref if current is not None else None
    assert ref is not None, describe(build_id)

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
