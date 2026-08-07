"""Per-build execution attempt counts (``attempt_count``).

The number a scheduler needs to run its own retry policy: how many times
has execution been started for this task *in this build*. Three properties
carry the whole feature, and each is easy to break without noticing:

- **An attempt is not a TASK_STARTED event.** Engines emit several starts
  per execution — an acquiring start for the claim/limit slot before the
  spawn (no executor ref), a second one once the spawn returns a ref, and,
  when the executor self-reports lifecycle, the worker's own start minutes
  later. Counting events would make one attempt look like two or three and
  exhaust any budget on the first try. The tests below pin the reactive
  (two starts) and resident (one, and three) shapes to the same answer.
- **The scope is the build, not the environment.** Unlike the
  denormalised ``latest_*`` fields it travels with, a count that spanned
  builds would let a task that failed twice last week arrive in a fresh
  trigger with no budget left.
- **A retry does not reset it.** A scheduler retries a failed task
  *through* ``POST .../retry``, so a resetting counter would be cleared by
  the very act it exists to bound, and ``max_attempts`` would never bind.

Postgres gets its own pass over the first and third: the derivation uses a
window function and a null-safe inequality, which render differently on
the two dialects.
"""

import pytest
from httpx import AsyncClient

BUILDS = "/api/v1/builds"


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


async def _frontier_attempts(client: AsyncClient, build_id: str) -> dict[str, int]:
    """``task_id -> attempt_count`` across every frontier list.

    Merged deliberately: the three lists are populated by three different
    code paths and a regression that drops the field from one of them
    should not be able to hide behind the others.
    """
    frontier = (await client.get(f"{BUILDS}/{build_id}/frontier")).json()
    return {
        ref["task_id"]: ref["attempt_count"]
        for key in ("actionable", "running", "roots")
        for ref in frontier[key]
    }


async def _listed_attempts(client: AsyncClient, build_id: str) -> dict[str, int]:
    tasks = (await client.get(f"{BUILDS}/{build_id}/tasks")).json()
    return {t["task_id"]: t["attempt_count"] for t in tasks}


# --- The double-start rule ----------------------------------------------


@pytest.mark.asyncio
async def test_reactive_acquire_then_spawn_is_one_attempt(client: AsyncClient):
    """The reactive shape: a claiming start with no ref, then a start
    carrying the ref once the spawn returned. Two events, one execution."""
    build_id = await _new_build(client)
    await _register_task(client, build_id, "react-1")

    # Acquiring start: arbitration happens before the spawn, so there is
    # nothing to reference yet.
    acquiring = await client.post(
        f"{BUILDS}/{build_id}/tasks/react-1/start", params={"claim": True}
    )
    assert acquiring.status_code == 200
    assert acquiring.json()["attempt_count"] == 1

    # Post-spawn start: same execution, now with a ref. NOT a claiming
    # start — a second claim would be denied as already-running, which is
    # exactly why the engine records the ref with a plain one.
    with_ref = await client.post(
        f"{BUILDS}/{build_id}/tasks/react-1/start",
        params={"executor": "modal", "executor_ref": "fc-1"},
    )
    assert with_ref.status_code == 200
    assert with_ref.json()["attempt_count"] == 1

    assert await _frontier_attempts(client, build_id) == {"react-1": 1}


@pytest.mark.asyncio
async def test_resident_single_start_is_one_attempt(client: AsyncClient):
    """The resident/sequential shape: one start, one attempt. The rule must
    not assume starts come in pairs."""
    build_id = await _new_build(client)
    await _register_task(client, build_id, "resident-1")

    response = await client.post(f"{BUILDS}/{build_id}/tasks/resident-1/start")
    assert response.json()["attempt_count"] == 1
    assert await _frontier_attempts(client, build_id) == {"resident-1": 1}


@pytest.mark.asyncio
async def test_resident_claim_spawn_and_worker_self_report_is_one_attempt(
    client: AsyncClient,
):
    """The widest shape: claim start, engine's ref-carrying start, and then
    the worker's own start once its container is finally up. Three events,
    still one execution — the rule collapses a run of starts however long."""
    build_id = await _new_build(client)
    await _register_task(client, build_id, "resident-3")

    await client.post(
        f"{BUILDS}/{build_id}/tasks/resident-3/start", params={"claim": True}
    )
    await client.post(
        f"{BUILDS}/{build_id}/tasks/resident-3/start",
        params={"executor": "modal", "executor_ref": "fc-3"},
    )
    worker = await client.post(
        f"{BUILDS}/{build_id}/tasks/resident-3/start",
        params={"executor": "modal", "executor_ref": "fc-3"},
    )
    assert worker.json()["attempt_count"] == 1
    assert await _frontier_attempts(client, build_id) == {"resident-3": 1}


@pytest.mark.asyncio
async def test_status_neutral_event_between_starts_does_not_split_the_attempt(
    client: AsyncClient,
):
    """A status-neutral event landing in the acquire→spawn window must not
    break the run of starts in two. It cannot change the task's status, so
    it cannot end an execution either."""
    build_id = await _new_build(client)
    await _register_task(client, build_id, "neutral-1")

    await client.post(
        f"{BUILDS}/{build_id}/tasks/neutral-1/start", params={"claim": True}
    )
    await client.post(f"{BUILDS}/{build_id}/tasks/neutral-1/waiting-for-lock")
    ref_start = await client.post(
        f"{BUILDS}/{build_id}/tasks/neutral-1/start",
        params={"executor": "modal", "executor_ref": "fc-n"},
    )
    assert ref_start.json()["attempt_count"] == 1


@pytest.mark.asyncio
async def test_second_execution_after_failure_is_a_second_attempt(
    client: AsyncClient,
):
    """The rule collapses *consecutive* starts only — a start after a
    terminal event is a genuinely new execution."""
    build_id = await _new_build(client)
    await _register_task(client, build_id, "fail-then-run")

    await client.post(
        f"{BUILDS}/{build_id}/tasks/fail-then-run/start", params={"claim": True}
    )
    await client.post(
        f"{BUILDS}/{build_id}/tasks/fail-then-run/start",
        params={"executor": "modal", "executor_ref": "fc-a"},
    )
    failed = await client.post(f"{BUILDS}/{build_id}/tasks/fail-then-run/fail")
    # The failure ends attempt 1; it does not start attempt 2.
    assert failed.json()["attempt_count"] == 1

    await client.post(f"{BUILDS}/{build_id}/tasks/fail-then-run/retry")
    second = await client.post(
        f"{BUILDS}/{build_id}/tasks/fail-then-run/start", params={"claim": True}
    )
    assert second.json()["attempt_count"] == 2


@pytest.mark.asyncio
async def test_suspend_then_resume_counts_the_re_execution(client: AsyncClient):
    """A task suspended for dynamic dependencies and then re-spawned really
    did execute twice, and the count says so. Documented rather than
    special-cased: ``attempt_count`` is executions, and a policy that wants
    to charge only failures needs a different number."""
    build_id = await _new_build(client)
    await _register_task(client, build_id, "dyn-1")

    await client.post(f"{BUILDS}/{build_id}/tasks/dyn-1/start", params={"claim": True})
    await client.post(f"{BUILDS}/{build_id}/tasks/dyn-1/suspend")
    resumed = await client.post(f"{BUILDS}/{build_id}/tasks/dyn-1/start")
    assert resumed.json()["attempt_count"] == 2


# --- Retry does not reset -----------------------------------------------


@pytest.mark.asyncio
async def test_retry_does_not_reset_the_count(client: AsyncClient):
    """``max_attempts`` is a per-build lifetime budget.

    A scheduler enforcing one retries *through* this endpoint — a failed
    task is not in the frontier's actionable set until something flips it
    back to PENDING — so a count that reset here would be reset by every
    enforcement of the budget it defines, and the build would loop forever.
    """
    build_id = await _new_build(client)
    await _register_task(client, build_id, "budget-1")

    for attempt in range(1, 4):
        started = await client.post(
            f"{BUILDS}/{build_id}/tasks/budget-1/start", params={"claim": True}
        )
        assert started.json()["attempt_count"] == attempt
        await client.post(f"{BUILDS}/{build_id}/tasks/budget-1/fail")
        retried = await client.post(f"{BUILDS}/{build_id}/tasks/budget-1/retry")
        # The retry itself is not an attempt, and does not erase the ones
        # already spent.
        assert retried.json()["attempt_count"] == attempt

    assert await _frontier_attempts(client, build_id) == {"budget-1": 3}


# --- Per-build scoping --------------------------------------------------


@pytest.mark.asyncio
async def test_two_builds_over_one_task_keep_separate_counts(client: AsyncClient):
    """The escape hatch a lifetime budget needs: a re-trigger is a new
    build, and a new build starts the task's budget over. If this leaked
    across builds, a task that exhausted its retries once would be
    unschedulable in every future build in the environment."""
    build_a = await _new_build(client)
    await _register_task(client, build_a, "shared-1")
    await client.post(
        f"{BUILDS}/{build_a}/tasks/shared-1/start", params={"claim": True}
    )
    await client.post(
        f"{BUILDS}/{build_a}/tasks/shared-1/start",
        params={"executor": "modal", "executor_ref": "fc-a"},
    )
    await client.post(f"{BUILDS}/{build_a}/tasks/shared-1/fail")

    # A fresh trigger: same task, new build.
    build_b = await _new_build(client)
    await _register_task(client, build_b, "shared-1")
    await client.post(f"{BUILDS}/{build_b}/tasks/shared-1/retry")
    started_b = await client.post(
        f"{BUILDS}/{build_b}/tasks/shared-1/start", params={"claim": True}
    )
    assert started_b.json()["attempt_count"] == 1

    # ...and build A's own record is untouched by build B's attempt.
    assert await _listed_attempts(client, build_a) == {"shared-1": 1}
    assert await _listed_attempts(client, build_b) == {"shared-1": 1}


@pytest.mark.asyncio
async def test_task_never_attempted_in_this_build_reports_zero(client: AsyncClient):
    """A root cached from an earlier build shows the build's own count —
    zero — not the global history that ``latest_status`` beside it
    reflects."""
    build_a = await _new_build(client)
    await _register_task(client, build_a, "cached-1")
    await client.post(
        f"{BUILDS}/{build_a}/tasks/cached-1/start", params={"claim": True}
    )
    await client.post(f"{BUILDS}/{build_a}/tasks/cached-1/complete")

    build_b = await _new_build(client, roots=["cached-1"])
    frontier = (await client.get(f"{BUILDS}/{build_b}/frontier")).json()
    root = frontier["roots"][0]
    # Globally completed (that scope is unchanged)...
    assert root["latest_status"] == "completed"
    # ...but this build has attempted nothing.
    assert root["attempt_count"] == 0


# --- Frontier and listing exposure --------------------------------------


@pytest.mark.asyncio
async def test_frontier_exposes_counts_on_actionable_running_and_roots(
    client: AsyncClient,
):
    """All three ref lists carry the field, with the counts kept apart."""
    build_id = await _new_build(client, roots=["front-root"])
    await client.post(f"{BUILDS}/{build_id}/tasks", json=_register("front-dep"))
    await client.post(
        f"{BUILDS}/{build_id}/tasks",
        json=_register("front-root", deps=["front-dep"]),
    )

    # One attempt on the dep, recorded the reactive way (two starts).
    await client.post(
        f"{BUILDS}/{build_id}/tasks/front-dep/start", params={"claim": True}
    )
    await client.post(
        f"{BUILDS}/{build_id}/tasks/front-dep/start",
        params={"executor": "modal", "executor_ref": "fc-d"},
    )

    frontier = (await client.get(f"{BUILDS}/{build_id}/frontier")).json()
    assert {t["task_id"]: t["attempt_count"] for t in frontier["actionable"]} == {
        "front-dep": 1
    }
    assert {t["task_id"]: t["attempt_count"] for t in frontier["running"]} == {
        "front-dep": 1
    }
    # The root is gated behind the running dep and has never been started.
    assert {t["task_id"]: t["attempt_count"] for t in frontier["roots"]} == {
        "front-root": 0
    }

    assert await _listed_attempts(client, build_id) == {
        "front-dep": 1,
        "front-root": 0,
    }


# --- Postgres parity ----------------------------------------------------


@pytest.mark.asyncio
async def test_attempt_counts_on_postgres(pg_client: AsyncClient):
    """The derivation is a LAG window function plus a null-safe inequality
    (``IS DISTINCT FROM`` on Postgres, ``IS NOT`` on SQLite), so the
    dialect the product actually runs on gets its own pass over the two
    rules that matter: consecutive starts collapse, and a retry does not
    reset."""
    build_id = await _new_build(pg_client)
    await _register_task(pg_client, build_id, "pg-1")

    await pg_client.post(
        f"{BUILDS}/{build_id}/tasks/pg-1/start", params={"claim": True}
    )
    with_ref = await pg_client.post(
        f"{BUILDS}/{build_id}/tasks/pg-1/start",
        params={"executor": "modal", "executor_ref": "fc-pg"},
    )
    assert with_ref.json()["attempt_count"] == 1
    assert await _frontier_attempts(pg_client, build_id) == {"pg-1": 1}

    await pg_client.post(f"{BUILDS}/{build_id}/tasks/pg-1/fail")
    await pg_client.post(f"{BUILDS}/{build_id}/tasks/pg-1/retry")
    second = await pg_client.post(
        f"{BUILDS}/{build_id}/tasks/pg-1/start", params={"claim": True}
    )
    assert second.json()["attempt_count"] == 2
    assert await _frontier_attempts(pg_client, build_id) == {"pg-1": 2}
    assert await _listed_attempts(pg_client, build_id) == {"pg-1": 2}
