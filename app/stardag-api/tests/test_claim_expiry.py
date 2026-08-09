"""Expiry of the per-task execution claim.

``Task.latest_status == RUNNING`` is the claim; ``latest_status_expires_at``
is how long it stays believable. The properties worth pinning are the ones
a future refactor could quietly drop:

- a *live* claim still denies a second claimant (the exactly-once guarantee
  is unchanged for everything that has not lapsed);
- an *expired* claim denies nothing and is taken over whole — status, build,
  executor fields and expiry replace the dead holder's together;
- ``NULL`` behaves exactly as the column never existed, while the claims
  that were RUNNING when it landed are backfilled rather than left there;
- the concurrency-limit count uses the same predicate, so an expired claim
  releases its slots. Missing this one preserves the leak in the single
  place nobody reads.

"Time passes" is simulated by rewriting ``latest_status_expires_at``
directly — the same trick ``test_build_cleanup`` uses for build ages.
"""

import os
from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from stardag_api.config import claim_settings
from stardag_api.models import Task

# The revision below the claim-expiry migration. Downgrading to it and back
# re-runs that migration (and anything stacked above it), which is what makes
# the backfill observable. Named explicitly so a later migration cannot
# silently turn these tests into no-ops -- see the note in the first one.
_CLAIM_EXPIRY_DOWN_REVISION = "11716b1f4c1a"

BUILDS = "/api/v1/builds"


def _register(task_id: str, deps: list[str] | None = None) -> dict:
    return {
        "task_id": task_id,
        "task_namespace": "",
        "task_name": "T",
        "task_data": {},
        "dependency_task_ids": deps or [],
    }


async def _new_build(client: AsyncClient) -> str:
    return (await client.post(BUILDS, json={})).json()["id"]


async def _register_task(client: AsyncClient, build_id: str, task_id: str) -> None:
    await client.post(f"{BUILDS}/{build_id}/tasks", json=_register(task_id))


async def _task_row(session: AsyncSession, task_id: str) -> Task:
    return (
        await session.execute(select(Task).where(Task.task_id == task_id))
    ).scalar_one()


async def _set_expiry(
    session: AsyncSession, task_id: str, expires_at: datetime | None
) -> None:
    """Rewrite a claim's expiry — "time passed", or "this claim predates the
    column" when ``None``."""
    await session.execute(
        update(Task)
        .where(Task.task_id == task_id)
        .values(latest_status_expires_at=expires_at)
    )
    await session.commit()


async def _expire(session: AsyncSession, task_id: str) -> None:
    await _set_expiry(
        session, task_id, datetime.now(timezone.utc) - timedelta(seconds=1)
    )


def _utc(value: datetime | None) -> datetime:
    """SQLite hands back naive UTC; Postgres hands back aware.

    Asserts non-null: every caller here has just checked that a claim was
    granted, so a missing timestamp is the failure, not a case to handle.
    """
    assert value is not None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


# --- Granting the expiry ------------------------------------------------


@pytest.mark.asyncio
async def test_start_grants_the_configured_default_expiry(
    client: AsyncClient, async_session: AsyncSession
):
    """A caller that says nothing gets the server default — and gets one at
    all, which is the difference between a claim that can heal and today's
    permanent wedge."""
    build_id = await _new_build(client)
    await _register_task(client, build_id, "default-ttl")
    before = datetime.now(timezone.utc)
    await client.post(f"{BUILDS}/{build_id}/tasks/default-ttl/start")

    task = await _task_row(async_session, "default-ttl")
    expires_at = _utc(task.latest_status_expires_at)
    expected = before + timedelta(seconds=claim_settings.default_ttl_seconds)
    assert timedelta(0) <= expires_at - expected < timedelta(seconds=30)


@pytest.mark.asyncio
async def test_caller_supplied_ttl_wins_over_the_default(
    client: AsyncClient, async_session: AsyncSession
):
    """The caller knows its executor's timeout; the server only has a guess."""
    build_id = await _new_build(client)
    await _register_task(client, build_id, "own-ttl")
    before = datetime.now(timezone.utc)
    response = await client.post(
        f"{BUILDS}/{build_id}/tasks/own-ttl/start",
        params={"claim": "true", "claim_ttl_seconds": 3600},
    )
    assert response.status_code == 200

    task = await _task_row(async_session, "own-ttl")
    delta = _utc(task.latest_status_expires_at) - before
    assert timedelta(seconds=3600) <= delta < timedelta(seconds=3630)


@pytest.mark.asyncio
async def test_absurd_ttls_are_rejected(client: AsyncClient):
    """Both bounds matter: a sub-minute claim can lapse while its own
    executor is still starting, and a multi-year one is the "forever" the
    expiry exists to end."""
    build_id = await _new_build(client)
    await _register_task(client, build_id, "ttl-bounds")
    start = f"{BUILDS}/{build_id}/tasks/ttl-bounds/start"

    assert (
        await client.post(start, params={"claim_ttl_seconds": 1})
    ).status_code == 422
    assert (
        await client.post(start, params={"claim_ttl_seconds": 10 * 365 * 24 * 3600})
    ).status_code == 422
    # The boundary itself is legal.
    assert (
        await client.post(start, params={"claim_ttl_seconds": 60})
    ).status_code == 200


@pytest.mark.asyncio
async def test_leaving_running_clears_the_expiry(
    client: AsyncClient, async_session: AsyncSession
):
    """An expiry outliving the claim it describes would be read as a live
    claim's deadline by anything that forgot to check the status first."""
    build_id = await _new_build(client)
    for task_id, verb in (
        ("gone-complete", "complete"),
        ("gone-fail", "fail"),
        ("gone-suspend", "suspend"),
    ):
        await _register_task(client, build_id, task_id)
        await client.post(f"{BUILDS}/{build_id}/tasks/{task_id}/start")
        assert (await _task_row(async_session, task_id)).latest_status_expires_at
        await client.post(f"{BUILDS}/{build_id}/tasks/{task_id}/{verb}")
        async_session.expire_all()
        task = await _task_row(async_session, task_id)
        assert task.latest_status_expires_at is None, task_id


@pytest.mark.asyncio
async def test_a_restart_extends_the_claim(
    client: AsyncClient, async_session: AsyncSession
):
    """Renewal on traffic that already exists — a re-start (recording an
    executor ref) re-grants the expiry. Note what this does NOT cover: a
    task that emits nothing between start and finish never renews, which is
    why the initial TTL should come from the executor's timeout rather than
    being a short lease."""
    build_id = await _new_build(client)
    await _register_task(client, build_id, "extend-me")
    await client.post(
        f"{BUILDS}/{build_id}/tasks/extend-me/start",
        params={"claim_ttl_seconds": 60},
    )
    first = _utc((await _task_row(async_session, "extend-me")).latest_status_expires_at)

    await client.post(
        f"{BUILDS}/{build_id}/tasks/extend-me/start",
        params={"claim_ttl_seconds": 3600, "executor_ref": "fc-2"},
    )
    async_session.expire_all()
    second = _utc(
        (await _task_row(async_session, "extend-me")).latest_status_expires_at
    )
    assert second > first


# --- Honouring the expiry: the claim check ------------------------------


@pytest.mark.asyncio
async def test_a_live_claim_still_denies_a_second_claimant(client: AsyncClient):
    """Exactly-once is unchanged for every claim that has not lapsed — and
    the denial now says when it stops applying, so the loser can wait
    instead of guessing."""
    build_id = await _new_build(client)
    await _register_task(client, build_id, "live-claim")
    assert (
        await client.post(
            f"{BUILDS}/{build_id}/tasks/live-claim/start",
            params={
                "claim": "true",
                "claim_ttl_seconds": 3600,
                "executor": "modal",
                "executor_ref": "fc-winner",
            },
        )
    ).status_code == 200

    response = await client.post(
        f"{BUILDS}/{build_id}/tasks/live-claim/start", params={"claim": "true"}
    )
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["error_code"] == "task_already_running"
    assert detail["executor_ref"] == "fc-winner"
    assert detail["latest_status_expires_at"] is not None


@pytest.mark.asyncio
async def test_a_claim_with_no_expiry_denies_forever_as_before(
    client: AsyncClient, async_session: AsyncSession
):
    """NULL is the pre-expiry semantic, and every row written before the
    column existed carries it. Such a claim must keep denying — the
    migration adds a column, it does not silently release anything."""
    build_id = await _new_build(client)
    await _register_task(client, build_id, "legacy-claim")
    await client.post(
        f"{BUILDS}/{build_id}/tasks/legacy-claim/start", params={"claim": "true"}
    )
    await _set_expiry(async_session, "legacy-claim", None)

    response = await client.post(
        f"{BUILDS}/{build_id}/tasks/legacy-claim/start", params={"claim": "true"}
    )
    assert response.status_code == 409
    assert response.json()["detail"]["error_code"] == "task_already_running"
    assert response.json()["detail"]["latest_status_expires_at"] is None


@pytest.mark.asyncio
async def test_an_expired_claim_is_reclaimable_and_the_holder_is_replaced(
    client: AsyncClient, async_session: AsyncSession
):
    """The healing mechanism, end to end: nothing releases the dead claim.
    The next claiming start simply takes it, and takes *all* of it — a
    reader must never be able to pair the new holder's expiry with the dead
    holder's executor ref."""
    build_a = await _new_build(client)
    await _register_task(client, build_a, "abandoned")
    await client.post(
        f"{BUILDS}/{build_a}/tasks/abandoned/start",
        params={
            "claim": "true",
            "executor": "modal",
            "executor_ref": "fc-dead",
            "executor_metadata": '{"kind": "modal", "app_name": "a"}',
        },
    )
    await _expire(async_session, "abandoned")

    # A different build — the case that had no remedy at all before, since
    # build B cannot probe build A's executor or release its claim.
    build_b = await _new_build(client)
    await _register_task(client, build_b, "abandoned")
    response = await client.post(
        f"{BUILDS}/{build_b}/tasks/abandoned/start",
        params={
            "claim": "true",
            "claim_ttl_seconds": 3600,
            "executor": "modal",
            "executor_ref": "fc-live",
        },
    )
    assert response.status_code == 200

    async_session.expire_all()
    task = await _task_row(async_session, "abandoned")
    assert task.latest_status_build_id == UUID(build_b)
    assert task.latest_executor_ref == "fc-live"
    # Cleared, not carried over from the dead holder.
    assert task.latest_executor_metadata is None
    assert _utc(task.latest_status_expires_at) > datetime.now(timezone.utc)

    # ...and the new claim denies the next comer, exactly like any other.
    assert (
        await client.post(
            f"{BUILDS}/{build_b}/tasks/abandoned/start", params={"claim": "true"}
        )
    ).status_code == 409


@pytest.mark.asyncio
async def test_expiry_does_not_override_completion(
    client: AsyncClient, async_session: AsyncSession
):
    """COMPLETED is sticky and content-addressed: an expiry left on the row
    by any means must not make a finished task re-claimable."""
    build_id = await _new_build(client)
    await _register_task(client, build_id, "done-task")
    await client.post(f"{BUILDS}/{build_id}/tasks/done-task/start")
    await client.post(f"{BUILDS}/{build_id}/tasks/done-task/complete")
    await _expire(async_session, "done-task")

    response = await client.post(
        f"{BUILDS}/{build_id}/tasks/done-task/start", params={"claim": "true"}
    )
    assert response.status_code == 409
    assert response.json()["detail"]["error_code"] == "task_already_completed"


# --- Honouring the expiry: concurrency-limit slots ----------------------


@pytest.mark.asyncio
async def test_an_expired_claim_frees_its_concurrency_slot(
    client: AsyncClient, async_session: AsyncSession
):
    """The half that is easiest to forget. If the ``active`` count kept
    counting expired claims, an abandoned task would stop blocking its own
    re-execution while still consuming the cap it was admitted under — the
    same leak, in the one place nobody looks."""
    await client.put("/api/v1/concurrency-limits/gpu", json={"max_concurrent": 1})
    build_id = await _new_build(client)
    for task_id in ("slot-holder", "slot-waiter"):
        await _register_task(client, build_id, task_id)

    assert (
        await client.post(
            f"{BUILDS}/{build_id}/tasks/slot-holder/start",
            params={"limit_key": ["gpu"], "enforce_limits": "true"},
        )
    ).status_code == 200

    # The cap is full while the holder's claim is live.
    denied = await client.post(
        f"{BUILDS}/{build_id}/tasks/slot-waiter/start",
        params={"limit_key": ["gpu"], "enforce_limits": "true"},
    )
    assert denied.status_code == 409
    assert denied.json()["detail"]["error_code"] == "concurrency_limit_reached"

    await _expire(async_session, "slot-holder")

    # No event moved the holder — it is still RUNNING to every status
    # reader — yet the slot it occupied is free.
    assert (await _task_row(async_session, "slot-holder")).latest_status == "running"
    holders = (await client.get("/api/v1/concurrency-limits/gpu/holders")).json()
    assert holders["total"] == 0
    assert (
        await client.post(
            f"{BUILDS}/{build_id}/tasks/slot-waiter/start",
            params={"limit_key": ["gpu"], "enforce_limits": "true"},
        )
    ).status_code == 200


@pytest.mark.asyncio
async def test_an_expired_holder_can_still_be_evicted(
    client: AsyncClient, async_session: AsyncSession
):
    """Eviction deliberately ignores the expiry: the slot is already free,
    but the task still reads RUNNING to the UI and to ``GET /tasks``, and
    recording the event that ends that is what eviction is for."""
    await client.put("/api/v1/concurrency-limits/tpu", json={"max_concurrent": 1})
    build_id = await _new_build(client)
    await _register_task(client, build_id, "evict-expired")
    await client.post(
        f"{BUILDS}/{build_id}/tasks/evict-expired/start",
        params={"limit_key": ["tpu"], "enforce_limits": "true"},
    )
    await _expire(async_session, "evict-expired")

    response = await client.post(
        "/api/v1/concurrency-limits/tpu/holders/evict-expired/evict"
    )
    assert response.status_code == 200
    assert response.json()["status"] == "failed"


# --- Exposing the expiry ------------------------------------------------


@pytest.mark.asyncio
async def test_frontier_surfaces_the_claim_expiry(client: AsyncClient):
    """A scheduler should decide from evidence, not from elapsed time."""
    build_id = await _new_build(client)
    await _register_task(client, build_id, "frontier-task")
    await client.post(
        f"{BUILDS}/{build_id}/tasks/frontier-task/start",
        params={"claim": "true", "claim_ttl_seconds": 3600},
    )

    frontier = (await client.get(f"{BUILDS}/{build_id}/frontier")).json()
    running = frontier["running"]
    assert [t["task_id"] for t in running] == ["frontier-task"]
    assert running[0]["latest_status_expires_at"] is not None
    # Same ref shape in `actionable` (a RUNNING task the scheduler must
    # decide about) — the two lists are built by the same helper, and a
    # scheduler reads whichever one the task lands in.
    actionable = {t["task_id"]: t for t in frontier["actionable"]}
    assert actionable["frontier-task"]["latest_status_expires_at"] is not None


@pytest.mark.asyncio
async def test_expired_cross_build_blocker_is_still_a_blocker(
    client: AsyncClient, async_session: AsyncSession
):
    """The correction worth encoding as a test: proving a blocker dead does
    NOT unblock the build. An upstream that is not in this build's task set
    is not in its plan either, so this build still cannot run it — what the
    expiry buys is certainty in place of inference, not one less blocker."""
    build_a = await _new_build(client)
    await _register_task(client, build_a, "blk-up")
    await client.post(
        f"{BUILDS}/{build_a}/tasks", json=_register("blk-down", ["blk-up"])
    )
    await client.post(
        f"{BUILDS}/{build_a}/tasks/blk-up/start", params={"claim": "true"}
    )
    await _expire(async_session, "blk-up")

    build_b = await _new_build(client)
    await client.post(f"{BUILDS}/{build_b}/tasks", json=_register("blk-down"))

    frontier = (await client.get(f"{BUILDS}/{build_b}/frontier")).json()
    assert frontier["actionable"] == []
    blockers = frontier["blocked_by_external"]
    assert [b["blocking_task_id"] for b in blockers] == ["blk-up"]
    # ...but build B is now *told* the holder is gone, instead of having to
    # infer it from blocking_status_at.
    expires_at = datetime.fromisoformat(blockers[0]["blocking_status_expires_at"])
    assert _utc(expires_at) < datetime.now(timezone.utc)
    assert blockers[0]["blocking_in_build"] is False


# --- Postgres ------------------------------------------------------------


@pytest.mark.asyncio
async def test_migration_backfills_claims_that_are_already_running(
    pg_client: AsyncClient, pg_session: AsyncSession, pg_engine
):
    """The migration's data step, end to end.

    Every task RUNNING when the column lands would otherwise carry NULL —
    and those are exactly the abandoned claims this feature exists to heal.
    A consumer that correctly reads NULL as "no expiry known, so wait"
    would then wait on them forever, i.e. the wedge restored, and the
    feature would ship not fixing the cases that motivated it.

    Here: a claim that has been RUNNING for 90 days, taken back through
    ``downgrade`` / ``upgrade`` so the backfill runs over real rows.

    Targets the revision *below* this one by name rather than ``-1``.
    ``-1`` means "one step back from head", which is this migration only
    while it happens to be the newest — the moment anything stacks on top,
    ``-1`` unwinds that instead, the backfill never re-runs, and this test
    fails without the backfill having changed.
    """
    import asyncio

    from alembic import command

    from tests.conftest import get_alembic_config

    build_a = (await pg_client.post(BUILDS, json={})).json()["id"]
    await pg_client.post(f"{BUILDS}/{build_a}/tasks", json=_register("pg-ancient"))
    await pg_client.post(
        f"{BUILDS}/{build_a}/tasks/pg-ancient/start",
        params={"claim": "true", "executor_ref": "fc-long-gone"},
    )
    # Pre-migration shape: RUNNING, long ago, no expiry recorded.
    long_ago = datetime.now(timezone.utc) - timedelta(days=90)
    await pg_session.execute(
        update(Task)
        .where(Task.task_id == "pg-ancient")
        .values(latest_status_at=long_ago, latest_status_expires_at=None)
    )
    await pg_session.commit()

    # Drop the column and re-add it, running the backfill over that row.
    # Dispose first: asyncpg caches statements per connection, and the DDL
    # changes the shape of `tasks`.
    await pg_engine.dispose()
    pg_url = os.environ["STARDAG_API_TEST_DATABASE_URL"]
    alembic_cfg = get_alembic_config(pg_url)
    await asyncio.to_thread(command.downgrade, alembic_cfg, _CLAIM_EXPIRY_DOWN_REVISION)
    await asyncio.to_thread(command.upgrade, alembic_cfg, "head")

    pg_session.expire_all()
    task = await _task_row(pg_session, "pg-ancient")
    expires_at = _utc(task.latest_status_expires_at)
    # Backfilled to latest_status_at + the default TTL, measured from a
    # start 90 days ago — so already long past, whatever the TTL is set to.
    assert expires_at < datetime.now(timezone.utc)
    assert expires_at == _utc(long_ago) + timedelta(
        seconds=claim_settings.default_ttl_seconds
    )

    # ...which is the whole point: it is claimable again, with no operator
    # action and nothing having moved its status.
    assert (await _task_row(pg_session, "pg-ancient")).latest_status == "running"
    build_b = (await pg_client.post(BUILDS, json={})).json()["id"]
    await pg_client.post(f"{BUILDS}/{build_b}/tasks", json=_register("pg-ancient"))
    response = await pg_client.post(
        f"{BUILDS}/{build_b}/tasks/pg-ancient/start", params={"claim": "true"}
    )
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_migration_leaves_claims_with_no_status_timestamp_alone(
    pg_client: AsyncClient, pg_session: AsyncSession, pg_engine
):
    """A pre-denormalisation row has no timestamp to measure from, so the
    backfill invents nothing — it stays NULL and keeps denying, exactly as
    it did before the column existed."""
    import asyncio

    from alembic import command

    from tests.conftest import get_alembic_config

    build_id = (await pg_client.post(BUILDS, json={})).json()["id"]
    await pg_client.post(f"{BUILDS}/{build_id}/tasks", json=_register("pg-no-ts"))
    await pg_client.post(
        f"{BUILDS}/{build_id}/tasks/pg-no-ts/start", params={"claim": "true"}
    )
    await pg_session.execute(
        update(Task)
        .where(Task.task_id == "pg-no-ts")
        .values(latest_status_at=None, latest_status_expires_at=None)
    )
    await pg_session.commit()

    await pg_engine.dispose()
    alembic_cfg = get_alembic_config(os.environ["STARDAG_API_TEST_DATABASE_URL"])
    await asyncio.to_thread(command.downgrade, alembic_cfg, _CLAIM_EXPIRY_DOWN_REVISION)
    await asyncio.to_thread(command.upgrade, alembic_cfg, "head")

    pg_session.expire_all()
    assert (await _task_row(pg_session, "pg-no-ts")).latest_status_expires_at is None
    denied = await pg_client.post(
        f"{BUILDS}/{build_id}/tasks/pg-no-ts/start", params={"claim": "true"}
    )
    assert denied.status_code == 409
    assert denied.json()["detail"]["error_code"] == "task_already_running"


@pytest.mark.asyncio
async def test_expiry_predicates_on_postgres(
    pg_client: AsyncClient, pg_session: AsyncSession
):
    """The two predicates against real Postgres: a genuine ``SELECT … FOR
    UPDATE`` on the claim row, a real ``timestamptz`` comparison (SQLite
    compares ISO strings), and the concurrency count as an actual SQL
    aggregate rather than the single-connection approximation."""
    await pg_client.put("/api/v1/concurrency-limits/pg-gpu", json={"max_concurrent": 1})
    build_a = (await pg_client.post(BUILDS, json={})).json()["id"]
    await pg_client.post(f"{BUILDS}/{build_a}/tasks", json=_register("pg-claim"))
    await pg_client.post(
        f"{BUILDS}/{build_a}/tasks/pg-claim/start",
        params={
            "claim": "true",
            "claim_ttl_seconds": 3600,
            "limit_key": ["pg-gpu"],
            "enforce_limits": "true",
            "executor_ref": "fc-dead",
        },
    )

    build_b = (await pg_client.post(BUILDS, json={})).json()["id"]
    await pg_client.post(f"{BUILDS}/{build_b}/tasks", json=_register("pg-claim"))
    denied = await pg_client.post(
        f"{BUILDS}/{build_b}/tasks/pg-claim/start", params={"claim": "true"}
    )
    assert denied.status_code == 409
    assert denied.json()["detail"]["error_code"] == "task_already_running"

    await _expire(pg_session, "pg-claim")

    holders = (await pg_client.get("/api/v1/concurrency-limits/pg-gpu/holders")).json()
    assert holders["total"] == 0
    reclaimed = await pg_client.post(
        f"{BUILDS}/{build_b}/tasks/pg-claim/start",
        params={
            "claim": "true",
            "limit_key": ["pg-gpu"],
            "enforce_limits": "true",
            "executor_ref": "fc-live",
        },
    )
    assert reclaimed.status_code == 200

    pg_session.expire_all()
    task = await _task_row(pg_session, "pg-claim")
    assert task.latest_status_build_id == UUID(build_b)
    assert task.latest_executor_ref == "fc-live"
    assert _utc(task.latest_status_expires_at) > datetime.now(timezone.utc)
