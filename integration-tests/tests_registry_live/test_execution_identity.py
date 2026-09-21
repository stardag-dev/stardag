"""A superseded execution cannot take its task back, and a retried claim
is not a second attempt.

Both rules are arbitrated by the deployed API on a row it holds
``FOR UPDATE``, and both were unreachable from the unit tests for the
same reason the cancel-path defects were: the double decided them, and a
double written from the same understanding as the code cannot contradict
it. Here the arbiter is a real Postgres reached over the network by
builds that share nothing but it.

**The takeover is produced, not simulated.** Build A claims and runs the
shared task; a cascading cancel releases A's claim -- which is what lets
the next build have the task, and is exactly how the production incident
began -- and B claims and runs its own execution. From that moment A's
execution is superseded while A's container is, as far as anything here
knows, still going: the server cannot stop anything.

What is synthesised is only the last step, A's worker checking in. A
real one would need a Modal preemption whose restart arrives after the
claim lapsed, which cannot be forced from a test; the start itself is an
ordinary non-claiming ``task_start``, byte for byte what that restarted
worker sends. The registry cannot tell the difference, which is the
point -- and it is the registry's answer under a genuine concurrent
takeover that this scenario exists to pin.
"""

from __future__ import annotations

import uuid

import pytest

from stardag.exceptions import APIError
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
# cancel lands, and that B's own copy is still running when A's
# superseded start arrives.
SHARED_SLEEP_SECONDS = 60

A_LINGER_SECONDS = 30
B_LINGER_SECONDS = 180

STATUS_TIMEOUT_SECONDS = 300
BUILD_TIMEOUT_SECONDS = 600


def test_a_superseded_workers_start_cannot_take_the_task_back() -> None:
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


def test_a_retried_claim_is_granted_to_the_execution_that_won_it() -> None:
    """The lost-response case, against the real arbiter.

    The registry client retries a POST whose response never arrived, so a
    claiming start that succeeded can be delivered twice. Refused, the
    second delivery tells the worker that somebody else holds the task --
    a correct reason to stand down, and it does, while itself holding the
    claim, leaving the task claimed and not running until the claim
    expires.

    Driven directly rather than through a build: reproducing a lost
    response against a live API is not something a test can arrange, and
    the delivery the client would repeat is this exact call.
    """
    import asyncio

    from stardag.registry import registry_provider
    from stardag_integration_tests.registry_live.tasks import get_range

    salt = uuid.uuid4().hex
    task = get_range(limit=3, salt=salt)
    registry = registry_provider.get()

    # No Modal here, deliberately. What this pins is the API's
    # arbitration over a real Postgres row it holds FOR UPDATE, and a
    # spawned worker would only add a second thing that could fail.
    build_id = registry.build_start([task], description="STA-50 retried claim")
    registry.task_register(build_id, task)
    execution_id = uuid.uuid4()

    async def _claim(eid: uuid.UUID):
        return await registry.task_start_claim_aio(build_id, task, execution_id=eid)

    first = asyncio.run(_claim(execution_id))
    assert first.started, f"the first claim was denied: {first}"
    assert first.execution_id == str(execution_id), (
        "The registry did not echo the execution identity, so it predates "
        f"the field and this scenario proves nothing: {first}"
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
        "The denial did not name the execution that holds the claim."
    )
