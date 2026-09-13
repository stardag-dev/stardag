"""Platform interruptions: the INTERRUPTED status and its own budget.

An interruption is the execution backend taking a container away for a
reason unrelated to the task — a function timeout, a reclaimed instance.
The worker reports it in the grace window it gets before the kill. Four
properties carry the feature, and each is a distinct way to get it wrong:

- **It is not a failure.** A FAILED task kills a FAIL_FAST build on the
  next scheduler pass. That is exactly what must not happen when the
  platform, not the task, ended the run — and it is why this is a status
  of its own rather than a ``retryable`` flag on ``/fail``: a
  worker-recorded failure has no window in which to be retried before the
  next frontier snapshot sees it.
- **It frees the claim and the slots.** The whole reason to report it at
  all: the task stops being unschedulable and stops occupying its
  concurrency-limit slots the moment the worker says so, rather than when
  something later notices the execution is gone.
- **It does not spend an attempt.** A task built to be killed and resumed
  until it converges would otherwise exhaust a budget meant for genuine
  failures and fail the build for the one reason it was designed to
  survive. It is bounded separately, by ``interrupt_count``.
- **It is still schedulable.** The frontier must list it as actionable and
  a re-trigger must reset it, or the build stalls on a task nobody owns.

Postgres gets its own pass: the attempt rule's null-safe inequality grew a
second term for this, and the interruption counter is a new grouped query
with the same correlated round cutoff.
"""

import pytest
from httpx import AsyncClient

BUILDS = "/api/v1/builds"
LIMITS = "/api/v1/concurrency-limits"


def _register(task_id: str, deps: list[str] | None = None) -> dict:
    return {
        "task_id": task_id,
        "task_namespace": "",
        "task_name": "T",
        "task_data": {},
        "dependency_task_ids": deps or [],
    }


async def _new_build(client: AsyncClient, roots: list[str] | None = None) -> str:
    body = {"root_task_ids": roots} if roots else {}
    return (await client.post(BUILDS, json=body)).json()["id"]


async def _register_task(client: AsyncClient, build_id: str, task_id: str) -> None:
    response = await client.post(f"{BUILDS}/{build_id}/tasks", json=_register(task_id))
    assert response.status_code == 201, response.text


async def _task(client: AsyncClient, task_id: str) -> dict:
    """Environment-global task row: the denormalised ``latest_*`` columns."""
    response = await client.get(f"/api/v1/tasks/{task_id}")
    assert response.status_code == 200, response.text
    return response.json()


async def _replayed(client: AsyncClient, build_id: str, task_id: str) -> dict:
    """The build-scoped row, whose status is *replayed* from this build's
    events rather than read off the denormalised columns.

    Worth reading separately: the two derivations live in different
    functions (``_apply_event_to_task`` and the per-build replay beside it)
    and a new event type has to be taught to both. A test that only ever
    reads one of them cannot tell that it was.
    """
    rows = (await client.get(f"{BUILDS}/{build_id}/tasks")).json()
    match = [row for row in rows if row["task_id"] == task_id]
    assert match, f"{task_id} not in {[r['task_id'] for r in rows]}"
    return match[0]


async def _frontier(client: AsyncClient, build_id: str) -> dict:
    return (await client.get(f"{BUILDS}/{build_id}/frontier")).json()


async def _counts(client: AsyncClient, build_id: str) -> dict[str, tuple[int, int]]:
    """``task_id -> (attempt_count, interrupt_count)`` across every list."""
    frontier = await _frontier(client, build_id)
    return {
        ref["task_id"]: (ref["attempt_count"], ref["interrupt_count"])
        for key in ("actionable", "running", "roots")
        for ref in frontier[key]
    }


# --- It is not a failure ------------------------------------------------


@pytest.mark.asyncio
async def test_interrupt_sets_interrupted_not_failed(client: AsyncClient):
    build_id = await _new_build(client)
    await _register_task(client, build_id, "t-1")
    await client.post(f"{BUILDS}/{build_id}/tasks/t-1/start", params={"claim": True})

    response = await client.post(
        f"{BUILDS}/{build_id}/tasks/t-1/interrupt",
        params={"reason": "function timeout"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "interrupted"
    assert response.json()["latest_status"] == "interrupted"

    # The reason is readable, like a failure's — the same question gets
    # asked of both.
    replayed = await _replayed(client, build_id, "t-1")
    assert replayed["status"] == "interrupted"
    assert replayed["error_message"] == "function timeout"


@pytest.mark.asyncio
async def test_interruption_is_not_an_ending(client: AsyncClient):
    """``latest_completed_at`` stays empty: a pause, not a result. A
    build's terminal detection and every "when did this finish" reader
    keys off that field, and an interrupted task has not finished."""
    build_id = await _new_build(client)
    await _register_task(client, build_id, "t-1")
    await client.post(f"{BUILDS}/{build_id}/tasks/t-1/start", params={"claim": True})
    await client.post(f"{BUILDS}/{build_id}/tasks/t-1/interrupt")

    assert (await _replayed(client, build_id, "t-1"))["completed_at"] is None


# --- It frees the claim and the slots -----------------------------------


@pytest.mark.asyncio
async def test_interrupt_releases_the_execution_claim(client: AsyncClient):
    """A claiming start is denied while a live claim exists; after an
    interruption it must succeed. This is the point of reporting."""
    build_id = await _new_build(client)
    await _register_task(client, build_id, "t-1")
    await client.post(
        f"{BUILDS}/{build_id}/tasks/t-1/start",
        params={"claim": True, "claim_ttl_seconds": 3600},
    )
    denied = await client.post(
        f"{BUILDS}/{build_id}/tasks/t-1/start", params={"claim": True}
    )
    assert denied.status_code == 409

    await client.post(f"{BUILDS}/{build_id}/tasks/t-1/interrupt")

    reclaimed = await client.post(
        f"{BUILDS}/{build_id}/tasks/t-1/start", params={"claim": True}
    )
    assert reclaimed.status_code == 200, reclaimed.text

    # ...and the expiry went with the claim, rather than being left behind
    # to make a fresh claim look already-lapsed.
    task = await _task(client, "t-1")
    assert task["latest_status"] == "running"


@pytest.mark.asyncio
async def test_interrupt_frees_concurrency_limit_slots(client: AsyncClient):
    """A slot is a RUNNING task holding a key row. Leaving INTERRUPTED
    tasks counted would starve the key environment-wide for as long as a
    checkpointing task keeps being resumed — i.e. permanently."""
    await client.put(f"{LIMITS}/gpu", json={"max_concurrent": 1})
    build_id = await _new_build(client)
    await _register_task(client, build_id, "holder")
    await _register_task(client, build_id, "waiter")

    held = await client.post(
        f"{BUILDS}/{build_id}/tasks/holder/start",
        params={"claim": True, "limit_key": ["gpu"], "enforce_limits": True},
    )
    assert held.status_code == 200, held.text
    blocked = await client.post(
        f"{BUILDS}/{build_id}/tasks/waiter/start",
        params={"claim": True, "limit_key": ["gpu"], "enforce_limits": True},
    )
    assert blocked.status_code == 409

    await client.post(f"{BUILDS}/{build_id}/tasks/holder/interrupt")

    admitted = await client.post(
        f"{BUILDS}/{build_id}/tasks/waiter/start",
        params={"claim": True, "limit_key": ["gpu"], "enforce_limits": True},
    )
    assert admitted.status_code == 200, admitted.text


# --- It does not spend an attempt ---------------------------------------


@pytest.mark.asyncio
async def test_interruption_does_not_open_a_new_attempt(client: AsyncClient):
    """start → interrupt → start is ONE attempt and TWO... no: one attempt
    and one interruption. The second start continues work the platform
    took away, so charging it to the retry budget would fail a
    checkpointing task's build for the one reason it was built to survive.
    """
    build_id = await _new_build(client)
    await _register_task(client, build_id, "t-1")

    first = await client.post(
        f"{BUILDS}/{build_id}/tasks/t-1/start", params={"claim": True}
    )
    assert first.json()["attempt_count"] == 1

    await client.post(f"{BUILDS}/{build_id}/tasks/t-1/interrupt")
    second = await client.post(
        f"{BUILDS}/{build_id}/tasks/t-1/start", params={"claim": True}
    )
    assert second.json()["attempt_count"] == 1

    assert await _counts(client, build_id) == {"t-1": (1, 1)}


@pytest.mark.asyncio
async def test_a_failure_still_opens_a_new_attempt(client: AsyncClient):
    """The control for the test above: the exemption must be specific to
    interruptions, not a general "any event between two starts is free"."""
    build_id = await _new_build(client)
    await _register_task(client, build_id, "t-1")

    await client.post(f"{BUILDS}/{build_id}/tasks/t-1/start", params={"claim": True})
    await client.post(f"{BUILDS}/{build_id}/tasks/t-1/fail")
    await client.post(f"{BUILDS}/{build_id}/tasks/t-1/retry")
    second = await client.post(
        f"{BUILDS}/{build_id}/tasks/t-1/start", params={"claim": True}
    )
    assert second.json()["attempt_count"] == 2


@pytest.mark.asyncio
async def test_repeated_interruptions_accumulate_their_own_count(
    client: AsyncClient,
):
    """Three resumes of a checkpointing task: still one attempt, three
    interruptions. Without the separate counter there would be no bound at
    all on a task that times out forever."""
    build_id = await _new_build(client)
    await _register_task(client, build_id, "trainer")

    for _ in range(3):
        await client.post(
            f"{BUILDS}/{build_id}/tasks/trainer/start", params={"claim": True}
        )
        await client.post(f"{BUILDS}/{build_id}/tasks/trainer/interrupt")

    assert await _counts(client, build_id) == {"trainer": (1, 3)}


@pytest.mark.asyncio
async def test_a_resume_resets_the_interruption_count(client: AsyncClient):
    """Same round window as attempts, for the same reason: re-triggering a
    build is how a user asks for another go, and a budget they cannot reset
    is a budget that eventually wedges the build for good."""
    build_id = await _new_build(client)
    await _register_task(client, build_id, "trainer")
    await client.post(
        f"{BUILDS}/{build_id}/tasks/trainer/start", params={"claim": True}
    )
    await client.post(f"{BUILDS}/{build_id}/tasks/trainer/interrupt")
    assert (await _counts(client, build_id))["trainer"] == (1, 1)

    await client.post(f"{BUILDS}/{build_id}/resume")
    assert (await _counts(client, build_id))["trainer"] == (0, 0)


# --- It is still schedulable --------------------------------------------


@pytest.mark.asyncio
async def test_frontier_lists_an_interrupted_task_as_actionable(
    client: AsyncClient,
):
    """Leave it out and the build looks finished while a task still needs
    running — the same argument that puts SUSPENDED in the set."""
    build_id = await _new_build(client, roots=["t-1"])
    await _register_task(client, build_id, "t-1")
    await client.post(f"{BUILDS}/{build_id}/tasks/t-1/start", params={"claim": True})
    await client.post(f"{BUILDS}/{build_id}/tasks/t-1/interrupt")

    frontier = await _frontier(client, build_id)
    assert [ref["task_id"] for ref in frontier["actionable"]] == ["t-1"]
    assert frontier["actionable"][0]["latest_status"] == "interrupted"


@pytest.mark.asyncio
async def test_interrupt_keeps_the_executor_ref_for_probing(client: AsyncClient):
    build_id = await _new_build(client)
    await _register_task(client, build_id, "t-1")
    await client.post(
        f"{BUILDS}/{build_id}/tasks/t-1/start",
        params={"claim": True, "executor": "modal", "executor_ref": "fc-1"},
    )
    await client.post(f"{BUILDS}/{build_id}/tasks/t-1/interrupt")

    task = await _task(client, "t-1")
    assert task["latest_executor"] == "modal"
    assert task["latest_executor_ref"] == "fc-1"


@pytest.mark.asyncio
async def test_retry_resets_an_interrupted_task(client: AsyncClient):
    """A re-trigger runs discovery with ``retry_failed=True``, which resets
    the retryable statuses. Omit INTERRUPTED and a task abandoned
    mid-interruption is unschedulable forever — the dead end SUSPENDED used
    to be."""
    build_id = await _new_build(client)
    await _register_task(client, build_id, "t-1")
    await client.post(
        f"{BUILDS}/{build_id}/tasks/t-1/start",
        params={"claim": True, "executor": "modal", "executor_ref": "fc-1"},
    )
    await client.post(f"{BUILDS}/{build_id}/tasks/t-1/interrupt")

    retried = await client.post(f"{BUILDS}/{build_id}/tasks/t-1/retry")
    assert retried.status_code == 200, retried.text
    assert retried.json()["latest_status"] == "pending"

    # A retry re-runs from scratch, so the ref of the execution that will
    # never resume must not survive it (unlike the interruption itself).
    task = await _task(client, "t-1")
    assert task["latest_executor_ref"] is None


@pytest.mark.asyncio
async def test_completed_stays_completed(client: AsyncClient):
    """COMPLETED is sticky environment-wide. A late interruption report
    from a worker whose output another build already observed must not
    un-complete the task."""
    build_id = await _new_build(client)
    await _register_task(client, build_id, "t-1")
    await client.post(f"{BUILDS}/{build_id}/tasks/t-1/start", params={"claim": True})
    await client.post(f"{BUILDS}/{build_id}/tasks/t-1/complete")

    await client.post(f"{BUILDS}/{build_id}/tasks/t-1/interrupt")
    task = await _task(client, "t-1")
    assert task["latest_status"] == "completed"


# --- It applies only to the claim the reporting build holds -------------
#
# A worker cannot tell a deliberate cancel from a function timeout: the
# execution backend delivers both as the same exception, with the same
# message. So the worker reports either way and the registry decides —
# it is the one that initiated the cancel, and the one that knows whose
# claim the task is currently under.


@pytest.mark.asyncio
async def test_interrupt_after_a_cancel_is_a_no_op(client: AsyncClient):
    """The cancel is usually *what* interrupted the worker. Letting the
    report land would resurrect a task the build just cancelled — the
    frontier lists INTERRUPTED as actionable, so a tick would start it
    again."""
    build_id = await _new_build(client)
    await _register_task(client, build_id, "t-1")
    await client.post(f"{BUILDS}/{build_id}/tasks/t-1/start", params={"claim": True})
    await client.post(f"{BUILDS}/{build_id}/tasks/t-1/cancel")

    response = await client.post(
        f"{BUILDS}/{build_id}/tasks/t-1/interrupt",
        params={"reason": "Input was cancelled by user"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "cancelled"
    assert response.json()["latest_status"] == "cancelled"

    assert (await _task(client, "t-1"))["latest_status"] == "cancelled"
    assert (await _replayed(client, build_id, "t-1"))["status"] == "cancelled"


@pytest.mark.asyncio
async def test_the_no_op_still_records_the_event(client: AsyncClient):
    """It happened, and it is the only trace that this execution ended at
    all. Only the *status* transition is refused."""
    build_id = await _new_build(client)
    await _register_task(client, build_id, "t-1")
    await client.post(f"{BUILDS}/{build_id}/tasks/t-1/start", params={"claim": True})
    await client.post(f"{BUILDS}/{build_id}/tasks/t-1/cancel")
    await client.post(f"{BUILDS}/{build_id}/tasks/t-1/interrupt")

    events = (await client.get(f"{BUILDS}/{build_id}/events")).json()
    assert "task_interrupted" in [event["event_type"] for event in events]


@pytest.mark.asyncio
async def test_a_refused_report_does_not_spend_the_interruption_budget(
    client: AsyncClient,
):
    """A worker cannot tell a cancel from a timeout, so it reports both —
    and a cancel's report is always refused. Counting those would let
    cancelling and retrying a task inside one build round eat the budget a
    genuine interruption needs, and the task would then fail on its first
    real one."""
    build_id = await _new_build(client)
    await _register_task(client, build_id, "t-1")
    await client.post(f"{BUILDS}/{build_id}/tasks/t-1/start", params={"claim": True})
    await client.post(f"{BUILDS}/{build_id}/tasks/t-1/cancel")

    # The dying worker reports, not knowing it was cancelled.
    await client.post(f"{BUILDS}/{build_id}/tasks/t-1/interrupt")
    await client.post(f"{BUILDS}/{build_id}/tasks/t-1/retry")
    await client.post(f"{BUILDS}/{build_id}/tasks/t-1/start", params={"claim": True})

    assert (await _counts(client, build_id))["t-1"] == (2, 0)

    # ...and a genuine one still counts.
    await client.post(f"{BUILDS}/{build_id}/tasks/t-1/interrupt")
    assert (await _counts(client, build_id))["t-1"] == (2, 1)


@pytest.mark.asyncio
async def test_a_report_after_a_completion_does_not_spend_the_budget(
    client: AsyncClient,
):
    """The second way to be refused, and it arrives by a different route:
    sticky-COMPLETED returns before the report's own branch is reached, so
    the refusal has to be marked there too or the event is counted as a
    real interruption.

    Declared as a root so the counts stay readable: a completed task is in
    no other frontier list."""
    build_id = await _new_build(client, roots=["t-1"])
    await _register_task(client, build_id, "t-1")
    await client.post(f"{BUILDS}/{build_id}/tasks/t-1/start", params={"claim": True})
    await client.post(f"{BUILDS}/{build_id}/tasks/t-1/complete")

    # A worker whose output another build already observed reports late.
    await client.post(f"{BUILDS}/{build_id}/tasks/t-1/interrupt")

    assert (await _task(client, "t-1"))["latest_status"] == "completed"
    assert (await _counts(client, build_id))["t-1"] == (1, 0)


@pytest.mark.asyncio
async def test_either_interleaving_of_cancel_and_interrupt_ends_cancelled(
    client: AsyncClient,
):
    """The worker reports inside its grace window while the canceller is
    still writing, so both orders are reachable. One is refused by the rule
    above; the other is simply overwritten, because nothing makes
    INTERRUPTED sticky. They must agree, or the outcome of a cancel would
    depend on a race."""
    build_id = await _new_build(client)
    await _register_task(client, build_id, "t-1")
    await client.post(f"{BUILDS}/{build_id}/tasks/t-1/start", params={"claim": True})

    await client.post(f"{BUILDS}/{build_id}/tasks/t-1/interrupt")
    await client.post(f"{BUILDS}/{build_id}/tasks/t-1/cancel")

    assert (await _task(client, "t-1"))["latest_status"] == "cancelled"


@pytest.mark.asyncio
async def test_interrupt_does_not_evict_another_builds_claim(client: AsyncClient):
    """A worker whose claim lapsed and was re-taken is reporting about a
    dead execution. Applying it would evict the live holder and hand the
    task to a scheduler while the new execution runs on — the duplicate
    execution claims exist to prevent."""
    first = await _new_build(client)
    second = await _new_build(client)
    await _register_task(client, first, "t-1")
    await _register_task(client, second, "t-1")

    await client.post(f"{BUILDS}/{first}/tasks/t-1/start", params={"claim": True})
    # The re-claim, expressed the way the reactive engine expresses a
    # non-arbitrating start: the second build is now the status holder.
    await client.post(
        f"{BUILDS}/{second}/tasks/t-1/start",
        params={"executor": "modal", "executor_ref": "fc-2"},
    )

    await client.post(f"{BUILDS}/{first}/tasks/t-1/interrupt")

    task = await _task(client, "t-1")
    assert task["latest_status"] == "running"
    assert task["latest_executor_ref"] == "fc-2"


# --- A preemption is not an interruption --------------------------------
#
# The backend restarts the same execution itself, so the task keeps its
# status, its claim and the executor ref the restart reuses. All the event
# records is that a restart is now *due* — which nothing could see before,
# because a restart that never arrived was indistinguishable from an
# execution running happily.


@pytest.mark.asyncio
async def test_preempt_leaves_the_task_running_and_claimed(client: AsyncClient):
    build_id = await _new_build(client)
    await _register_task(client, build_id, "t-1")
    await client.post(
        f"{BUILDS}/{build_id}/tasks/t-1/start",
        params={"claim": True, "executor": "modal", "executor_ref": "fc-1"},
    )

    response = await client.post(
        f"{BUILDS}/{build_id}/tasks/t-1/preempt",
        params={"reason": "container reclaimed"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["latest_status"] == "running"
    assert response.json()["status"] == "running"

    task = await _task(client, "t-1")
    assert task["latest_status"] == "running"
    # The ref the restart will reuse, and no error text: nothing is wrong
    # with a task whose container is coming back, so the reason stays on
    # the event row rather than becoming the task's error message.
    assert task["latest_executor_ref"] == "fc-1"
    assert (await _replayed(client, build_id, "t-1"))["error_message"] is None


@pytest.mark.asyncio
async def test_preempt_shortens_the_claim_rather_than_releasing_it(
    client: AsyncClient,
):
    """The point of the whole event. A worker timeout of a day means a
    claim of a day, so a restart that never comes used to wedge the task
    for that long. The claim survives — releasing it would invite a second
    execution of a task that is about to resume — but only for as long as
    the restart is plausible."""
    build_id = await _new_build(client)
    await _register_task(client, build_id, "t-1")
    await client.post(
        f"{BUILDS}/{build_id}/tasks/t-1/start",
        params={"claim": True, "claim_ttl_seconds": 86400},
    )
    granted = (await _task(client, "t-1"))["latest_status_expires_at"]

    await client.post(f"{BUILDS}/{build_id}/tasks/t-1/preempt")
    task = await _task(client, "t-1")

    assert task["latest_status_expires_at"] is not None
    assert task["latest_status_expires_at"] < granted
    assert task["latest_preempted_at"] is not None


@pytest.mark.asyncio
async def test_the_restart_re_grants_the_full_claim(client: AsyncClient):
    """And with it, "a restart is outstanding" — which is derived, never
    stored — becomes false on its own."""
    build_id = await _new_build(client)
    await _register_task(client, build_id, "t-1")
    await client.post(
        f"{BUILDS}/{build_id}/tasks/t-1/start",
        params={"claim": True, "claim_ttl_seconds": 86400},
    )
    await client.post(f"{BUILDS}/{build_id}/tasks/t-1/preempt")
    shortened = (await _task(client, "t-1"))["latest_status_expires_at"]

    # Modal restarts the input on the same call id; the container reports
    # its own start.
    await client.post(
        f"{BUILDS}/{build_id}/tasks/t-1/start",
        params={
            "executor": "modal",
            "executor_ref": "fc-1",
            "claim_ttl_seconds": 86400,
        },
    )

    task = await _task(client, "t-1")
    assert task["latest_status_expires_at"] > shortened
    assert task["latest_preempted_at"] < task["latest_status_at"]


@pytest.mark.asyncio
async def test_preemption_spends_neither_budget(client: AsyncClient):
    """Two consecutive TASK_STARTEDs are what make the backend's own
    restart free, and an event between them would silently start charging
    for it. A preemption is also not a stardag resumption, so it must not
    spend the interruption budget either."""
    build_id = await _new_build(client)
    await _register_task(client, build_id, "t-1")
    await client.post(f"{BUILDS}/{build_id}/tasks/t-1/start", params={"claim": True})
    assert (await _counts(client, build_id))["t-1"] == (1, 0)

    await client.post(f"{BUILDS}/{build_id}/tasks/t-1/preempt")
    await client.post(f"{BUILDS}/{build_id}/tasks/t-1/start")

    assert (await _counts(client, build_id))["t-1"] == (1, 0)


@pytest.mark.asyncio
async def test_a_report_does_not_apply_to_a_replacement_execution(
    client: AsyncClient,
):
    """The build can replace its *own* execution — its claim lapses, it
    retries, it starts again — and the build id alone cannot see that. A
    report naming the execution it is about is only honoured while the task
    still holds that one."""
    build_id = await _new_build(client)
    await _register_task(client, build_id, "t-1")
    await client.post(
        f"{BUILDS}/{build_id}/tasks/t-1/start",
        params={"claim": True, "executor": "modal", "executor_ref": "fc-old"},
    )
    # The claim lapsed and the same build started a replacement.
    await client.post(
        f"{BUILDS}/{build_id}/tasks/t-1/start",
        params={"executor": "modal", "executor_ref": "fc-new"},
    )

    # The first execution's report finally lands.
    await client.post(
        f"{BUILDS}/{build_id}/tasks/t-1/interrupt",
        params={"executor_ref": "fc-old"},
    )

    task = await _task(client, "t-1")
    assert task["latest_status"] == "running"
    assert task["latest_executor_ref"] == "fc-new"


@pytest.mark.asyncio
async def test_a_missing_current_ref_is_not_a_wildcard(client: AsyncClient):
    """The replacement's *claiming* start carries no ref — the spawn has not
    happened yet — and clears the one the dead execution left. If a missing
    current ref matched anything, the whole acquire→spawn gap would accept
    the dead execution's report, which is precisely the window a
    replacement is most likely to be in."""
    build_id = await _new_build(client)
    await _register_task(client, build_id, "t-1")
    await client.post(
        f"{BUILDS}/{build_id}/tasks/t-1/start",
        params={"claim": True, "executor": "modal", "executor_ref": "fc-old"},
    )
    # The claim lapsed; the replacement has acquired but not yet spawned.
    await client.post(f"{BUILDS}/{build_id}/tasks/t-1/start")

    await client.post(
        f"{BUILDS}/{build_id}/tasks/t-1/interrupt",
        params={"executor_ref": "fc-old"},
    )

    assert (await _task(client, "t-1"))["latest_status"] == "running"


@pytest.mark.asyncio
async def test_the_replay_agrees_with_the_row_about_a_stale_report(
    client: AsyncClient,
):
    """The two derivations answer the same question for different readers —
    the per-build view and the environment-global row. A report the row
    refuses but a replay applies would show one task as INTERRUPTED in one
    place and RUNNING in the other.

    Read off the **event response**, which is the one thing that returns
    ``get_task_status_in_build``'s answer: ``GET /builds/{id}/tasks`` is
    backed by the denormalised row, so asserting there would pass with the
    replay rule removed entirely."""
    build_id = await _new_build(client)
    await _register_task(client, build_id, "t-1")
    await client.post(
        f"{BUILDS}/{build_id}/tasks/t-1/start",
        params={"claim": True, "executor": "modal", "executor_ref": "fc-old"},
    )
    await client.post(
        f"{BUILDS}/{build_id}/tasks/t-1/start",
        params={"executor": "modal", "executor_ref": "fc-new"},
    )

    stale = await client.post(
        f"{BUILDS}/{build_id}/tasks/t-1/interrupt",
        params={"executor_ref": "fc-old"},
    )

    assert stale.json()["status"] == "running", stale.text
    assert stale.json()["latest_status"] == "running"
    assert (await _task(client, "t-1"))["latest_status"] == "running"


@pytest.mark.asyncio
async def test_a_retry_clears_the_ref_the_replay_matches_against(
    client: AsyncClient,
):
    """A retry re-runs from scratch, so the row clears the executor ref with
    it. The replay has to clear the ref it tracks at the same point, or a
    delayed report from the abandoned execution is accepted there after a
    resume while the row refuses it — the divergence the ref rule exists to
    close, reintroduced one branch over."""
    build_id = await _new_build(client)
    await _register_task(client, build_id, "t-1")
    await client.post(
        f"{BUILDS}/{build_id}/tasks/t-1/start",
        params={"claim": True, "executor": "modal", "executor_ref": "fc-old"},
    )
    await client.post(f"{BUILDS}/{build_id}/tasks/t-1/fail")
    await client.post(f"{BUILDS}/{build_id}/tasks/t-1/retry")
    await client.post(f"{BUILDS}/{build_id}/tasks/t-1/resume")

    stale = await client.post(
        f"{BUILDS}/{build_id}/tasks/t-1/interrupt",
        params={"executor_ref": "fc-old"},
    )

    assert stale.json()["status"] == "running", stale.text
    assert stale.json()["latest_status"] == "running"


@pytest.mark.asyncio
async def test_a_report_naming_the_current_execution_still_applies(
    client: AsyncClient,
):
    """The control. The ref narrows the rule; it must not disable it."""
    build_id = await _new_build(client)
    await _register_task(client, build_id, "t-1")
    await client.post(
        f"{BUILDS}/{build_id}/tasks/t-1/start",
        params={"claim": True, "executor": "modal", "executor_ref": "fc-1"},
    )

    await client.post(
        f"{BUILDS}/{build_id}/tasks/t-1/interrupt", params={"executor_ref": "fc-1"}
    )

    assert (await _task(client, "t-1"))["latest_status"] == "interrupted"


@pytest.mark.asyncio
async def test_a_report_with_no_ref_still_applies(client: AsyncClient):
    """An SDK predating the ref sends none. Refusing its reports would turn
    a version skew into exactly the silent stall this whole path removes."""
    build_id = await _new_build(client)
    await _register_task(client, build_id, "t-1")
    await client.post(
        f"{BUILDS}/{build_id}/tasks/t-1/start",
        params={"claim": True, "executor": "modal", "executor_ref": "fc-1"},
    )

    await client.post(f"{BUILDS}/{build_id}/tasks/t-1/interrupt")

    assert (await _task(client, "t-1"))["latest_status"] == "interrupted"


@pytest.mark.asyncio
async def test_preempt_never_extends_a_claim(client: AsyncClient):
    """A claim shorter than the restart grace — a 60s worker preempted near
    its deadline — must not be *extended* by a report whose entire purpose
    is to shorten it."""
    build_id = await _new_build(client)
    await _register_task(client, build_id, "t-1")
    await client.post(
        f"{BUILDS}/{build_id}/tasks/t-1/start",
        params={"claim": True, "claim_ttl_seconds": 60},
    )
    granted = (await _task(client, "t-1"))["latest_status_expires_at"]

    await client.post(f"{BUILDS}/{build_id}/tasks/t-1/preempt")

    # 60s is well inside the 300s grace, so the grace is the *later* of the
    # two and must lose.
    assert (await _task(client, "t-1"))["latest_status_expires_at"] == granted


@pytest.mark.asyncio
async def test_preempt_does_not_resurrect_a_lapsed_claim(
    client: AsyncClient, monkeypatch
):
    """An interruption applied to a lapsed claim merely releases something
    already released. A preemption *grants* a window, so applying it to a
    lapsed claim would make a task re-claimable a moment ago deniable
    again — by a corpse."""
    from stardag_api.services import status as status_module

    build_id = await _new_build(client)
    await _register_task(client, build_id, "t-1")
    await client.post(
        f"{BUILDS}/{build_id}/tasks/t-1/start",
        params={"claim": True, "claim_ttl_seconds": 60},
    )

    # The claim lapses without anyone writing anything — which is the whole
    # point of an expiry, and the only way to reach this state. Patched
    # rather than waited out: the minimum TTL the server accepts is 60s.
    monkeypatch.setattr(status_module, "claim_is_live", lambda task, now=None: False)

    await client.post(f"{BUILDS}/{build_id}/tasks/t-1/preempt")

    task = await _task(client, "t-1")
    assert task["latest_preempted_at"] is None


@pytest.mark.asyncio
async def test_preempt_obeys_the_same_authority_rule(client: AsyncClient):
    """A cancelled task must not have its claim quietly re-granted for
    another five minutes by a worker that has not noticed yet."""
    build_id = await _new_build(client)
    await _register_task(client, build_id, "t-1")
    await client.post(f"{BUILDS}/{build_id}/tasks/t-1/start", params={"claim": True})
    await client.post(f"{BUILDS}/{build_id}/tasks/t-1/cancel")

    await client.post(f"{BUILDS}/{build_id}/tasks/t-1/preempt")

    task = await _task(client, "t-1")
    assert task["latest_status"] == "cancelled"
    assert task["latest_status_expires_at"] is None
    assert task["latest_preempted_at"] is None


# --- Postgres parity ----------------------------------------------------


@pytest.mark.asyncio
async def test_interruption_counting_on_postgres(pg_client: AsyncClient):
    """The attempt rule's null-safe inequality gained a second term for
    this, and the interruption counter is a new grouped query with the same
    correlated round cutoff — both render differently per dialect, so the
    one the product runs on gets its own pass."""
    build_id = await _new_build(pg_client)
    await _register_task(pg_client, build_id, "pg-1")

    await pg_client.post(
        f"{BUILDS}/{build_id}/tasks/pg-1/start", params={"claim": True}
    )
    await pg_client.post(f"{BUILDS}/{build_id}/tasks/pg-1/interrupt")
    resumed = await pg_client.post(
        f"{BUILDS}/{build_id}/tasks/pg-1/start", params={"claim": True}
    )
    assert resumed.json()["attempt_count"] == 1
    assert await _counts(pg_client, build_id) == {"pg-1": (1, 1)}

    # A failure between two starts still opens an attempt — the exemption
    # must not have widened into "any event".
    await pg_client.post(f"{BUILDS}/{build_id}/tasks/pg-1/fail")
    await pg_client.post(f"{BUILDS}/{build_id}/tasks/pg-1/retry")
    second = await pg_client.post(
        f"{BUILDS}/{build_id}/tasks/pg-1/start", params={"claim": True}
    )
    assert second.json()["attempt_count"] == 2

    await pg_client.post(f"{BUILDS}/{build_id}/resume")
    assert await _counts(pg_client, build_id) == {"pg-1": (0, 0)}


@pytest.mark.asyncio
async def test_preemption_is_invisible_to_the_sql_attempt_count(
    pg_client: AsyncClient,
):
    """The SQL twin of ``starts_new_attempt`` filters on the *ordering*
    event types before it looks at predecessors, so leaving TASK_PREEMPTED
    out of that tuple is what keeps the backend's own restart free. That is
    a property of a LAG over a WHERE, not of the Python rule, so it gets
    asserted against the dialect that runs it."""
    build_id = await _new_build(pg_client)
    await _register_task(pg_client, build_id, "pg-1")

    await pg_client.post(
        f"{BUILDS}/{build_id}/tasks/pg-1/start", params={"claim": True}
    )
    await pg_client.post(f"{BUILDS}/{build_id}/tasks/pg-1/preempt")
    restarted = await pg_client.post(f"{BUILDS}/{build_id}/tasks/pg-1/start")

    assert restarted.json()["attempt_count"] == 1
    assert await _counts(pg_client, build_id) == {"pg-1": (1, 0)}


@pytest.mark.asyncio
async def test_refused_reports_are_excluded_from_the_count_on_postgres(
    pg_client: AsyncClient,
):
    """The exclusion is a JSON predicate on the event metadata, and JSON
    accessors are the most dialect-specific thing in this query — SQLite
    reads it through ``JSON_EXTRACT`` and Postgres through ``->``. Both
    have to agree that a key which is *absent* still counts."""
    build_id = await _new_build(pg_client)
    await _register_task(pg_client, build_id, "pg-1")
    await pg_client.post(
        f"{BUILDS}/{build_id}/tasks/pg-1/start", params={"claim": True}
    )
    await pg_client.post(f"{BUILDS}/{build_id}/tasks/pg-1/cancel")
    # The dying worker reports, not knowing it was cancelled. Refused, so
    # audited but not counted. (Asserted after the retry below: a cancelled
    # task is in no frontier list, so there is nothing to read counts off
    # until it is schedulable again.)
    await pg_client.post(f"{BUILDS}/{build_id}/tasks/pg-1/interrupt")
    await pg_client.post(f"{BUILDS}/{build_id}/tasks/pg-1/retry")
    await pg_client.post(
        f"{BUILDS}/{build_id}/tasks/pg-1/start", params={"claim": True}
    )

    assert await _counts(pg_client, build_id) == {"pg-1": (2, 0)}

    # ...and a genuine one still counts, so the key's absence is not being
    # read as "excluded".
    await pg_client.post(f"{BUILDS}/{build_id}/tasks/pg-1/interrupt")
    assert await _counts(pg_client, build_id) == {"pg-1": (2, 1)}
