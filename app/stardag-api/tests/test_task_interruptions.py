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
