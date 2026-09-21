"""The claim's own identity, and the one thing it settles.

An execution claim is taken *before* the spawn — the claim and any
concurrency-limit slots have to be acquired in one transaction, so a
denied task never occupies a worker — which means there is no executor
ref at claim time and never was. Without one, two requests were
indistinguishable:

**A retried claiming start**, which the registry client sends when a
POST's response is lost, and **a genuine second attempt of the same
build**. Both were refused. A refusal is a correct reason for a worker
to stand down — it means somebody else is running the task — so it did,
while itself holding the claim, and the task then sat claimed and not
running until the claim expired. That is the worst outcome available at
that endpoint.

``execution_id`` separates them: a retry repeats it, a second attempt
mints a new one. The build id cannot, because two attempts of one build
are legitimately distinct; the ref cannot, because there is not one yet.

The rule is written so that **absence is never a mismatch**: a caller
that mints no id behaves exactly as it did before the column existed,
and so does a task claimed before it. "Time passes" is simulated by
rewriting ``latest_status_expires_at``, as in ``test_claim_expiry``.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from stardag_api.models import Task

BUILDS = "/api/v1/builds"

pytestmark = pytest.mark.asyncio


def _eid() -> str:
    return str(uuid.uuid4())


def _register(task_id: str) -> dict:
    return {
        "task_id": task_id,
        "task_namespace": "",
        "task_name": task_id,
        "task_data": {},
    }


async def _new_build(client: AsyncClient) -> str:
    return (await client.post(BUILDS, json={})).json()["id"]


async def _start(
    client: AsyncClient, build_id: str, task_id: str, **params
) -> Response:
    return await client.post(
        f"{BUILDS}/{build_id}/tasks/{task_id}/start", params=params
    )


async def _registered(client: AsyncClient, task_id: str) -> str:
    build_id = await _new_build(client)
    await client.post(f"{BUILDS}/{build_id}/tasks", json=_register(task_id))
    return build_id


async def _claimed(
    client: AsyncClient, task_id: str, execution_id: str
) -> tuple[str, str]:
    """A task claimed by a fresh build under ``execution_id``, no ref yet.

    The shape the reactive tick actually produces: the claim goes in
    before the spawn, so the only identity on the row is the minted one.
    """
    build_id = await _registered(client, task_id)
    first = await _start(
        client, build_id, task_id, claim="true", execution_id=execution_id
    )
    assert first.status_code == 200, first.text
    return build_id, execution_id


async def _task_row(session: AsyncSession, task_id: str) -> Task:
    """The task row as the API has just left it.

    ``expire_all`` first, which is not ceremony: the API writes through
    its own session, so a row this session has already loaded is served
    from its identity map with the values it had then, and an assertion
    that something *changed* would pass vacuously.
    """
    session.expire_all()
    return (
        await session.execute(select(Task).where(Task.task_id == task_id))
    ).scalar_one()


async def _expire(session: AsyncSession, task_id: str) -> None:
    """Bring a claim's expiry into the past."""
    await session.execute(
        update(Task)
        .where(Task.task_id == task_id)
        .values(
            latest_status_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)
        )
    )
    await session.commit()


# --- The retried claim ---------------------------------------------------


async def test_a_retried_ref_less_claim_is_granted(client: AsyncClient):
    """The case the pair could never answer, because a claim has no ref.

    Same build, same minted id, no executor ref anywhere: this is the
    reactive engine's actual claim, re-delivered. Before the id it fell
    through to a 409 and the winner stood down from its own task.
    """
    execution_id = _eid()
    build_id, _ = await _claimed(client, "retried-claim", execution_id)

    again = await _start(
        client, build_id, "retried-claim", claim="true", execution_id=execution_id
    )

    assert again.status_code == 200, again.text
    assert again.json()["execution_id"] == execution_id


async def test_a_second_attempt_of_the_same_build_is_still_refused(
    client: AsyncClient,
):
    """The property the id must not cost: two attempts of one build are
    distinct, and granting on the build alone would hand out real
    double-claims."""
    build_id, _ = await _claimed(client, "second-attempt", _eid())

    again = await _start(
        client, build_id, "second-attempt", claim="true", execution_id=_eid()
    )

    assert again.status_code == 409, again.text
    detail = again.json()["detail"]
    assert detail["error_code"] == "task_already_running"
    # The denial names the claim that holds the task, so a caller can
    # tell "somebody else has this" from "my retry was not recognised".
    # Only the second is a bug here.
    assert detail["execution_id"] is not None


async def test_a_second_attempt_after_a_failed_spawn_is_still_refused(
    client: AsyncClient,
):
    """A failed spawn leaves the claim held and no ref recorded — the
    state a "same build, nothing recorded yet" rule would have granted.
    It is still a second attempt, and the id says so where the absent
    ref could not."""
    build_id, _ = await _claimed(client, "failed-spawn", _eid())

    again = await _start(
        client, build_id, "failed-spawn", claim="true", execution_id=_eid()
    )

    assert again.status_code == 409, again.text
    assert again.json()["detail"]["error_code"] == "task_already_running"


async def test_another_build_with_the_same_id_is_still_refused(client: AsyncClient):
    """Build ownership is tested first, so a neighbour that somehow
    named the holder's claim is refused like any other second
    claimant."""
    execution_id = _eid()
    await _claimed(client, "shared-id", execution_id)
    other_build = await _new_build(client)

    denied = await _start(
        client, other_build, "shared-id", claim="true", execution_id=execution_id
    )

    assert denied.status_code == 409, denied.text
    assert denied.json()["detail"]["error_code"] == "task_already_running"


async def test_a_claim_with_no_id_keeps_the_pair_rule(client: AsyncClient):
    """An SDK that mints no id is unaffected: the ``(executor, ref)``
    pair still decides, and a request with neither is still refused."""
    build_id = await _registered(client, "no-id-claim")
    assert (
        await _start(
            client,
            build_id,
            "no-id-claim",
            claim="true",
            executor="modal",
            executor_ref="fc-1",
        )
    ).status_code == 200

    same = await _start(
        client,
        build_id,
        "no-id-claim",
        claim="true",
        executor="modal",
        executor_ref="fc-1",
    )
    anonymous = await _start(client, build_id, "no-id-claim", claim="true")

    assert same.status_code == 200, same.text
    assert anonymous.status_code == 409, anonymous.text


# --- What the fold does with it ------------------------------------------


async def test_the_ref_recording_start_does_not_erase_the_identity(
    client: AsyncClient, async_session: AsyncSession
):
    """The reason the fold preserves rather than set-or-clears.

    Moments after the claim, the tick records a second start carrying
    the spawn's ref and no identity. Clearing on it would drop the id
    immediately, so a retried claim arriving even slightly late would be
    read as a second attempt and refused — the exact failure this
    column exists to close.
    """
    execution_id = _eid()
    build_id, _ = await _claimed(client, "ref-recording", execution_id)

    with_ref = await _start(
        client,
        build_id,
        "ref-recording",
        executor="modal",
        executor_ref="fc-1",
    )
    assert with_ref.status_code == 200, with_ref.text

    row = await _task_row(async_session, "ref-recording")
    assert str(row.latest_execution_id) == execution_id
    assert row.latest_executor_ref == "fc-1"

    # ...and the retry still works afterwards, which is the point.
    late_retry = await _start(
        client, build_id, "ref-recording", claim="true", execution_id=execution_id
    )
    assert late_retry.status_code == 200, late_retry.text


async def test_a_retry_clears_the_identity(
    client: AsyncClient, async_session: AsyncSession
):
    """A retry re-runs from scratch, so the next attempt is a new claim
    and must not be granted as a repeat of the one that failed."""
    execution_id = _eid()
    build_id, _ = await _claimed(client, "retry-clears", execution_id)
    await client.post(f"{BUILDS}/{build_id}/tasks/retry-clears/fail")
    await client.post(f"{BUILDS}/{build_id}/tasks/retry-clears/retry")

    row = await _task_row(async_session, "retry-clears")
    assert row.latest_execution_id is None

    again = await _start(
        client, build_id, "retry-clears", claim="true", execution_id=_eid()
    )
    assert again.status_code == 200, again.text


async def test_a_lapsed_claim_is_taken_over_whole(
    client: AsyncClient, async_session: AsyncSession
):
    """The healing path is unchanged: an expired claim denies nothing,
    and the new holder's identity replaces the dead one's along with its
    build and expiry."""
    build_id, _ = await _claimed(client, "lapsed-takeover", _eid())
    await _expire(async_session, "lapsed-takeover")

    fresh = _eid()
    other_build = await _new_build(client)
    taken = await _start(
        client, other_build, "lapsed-takeover", claim="true", execution_id=fresh
    )

    assert taken.status_code == 200, taken.text
    row = await _task_row(async_session, "lapsed-takeover")
    assert str(row.latest_execution_id) == fresh
    assert str(row.latest_status_build_id) == other_build


async def test_an_eviction_names_the_claim_it_took(client: AsyncClient):
    """Every endpoint returning this model echoes the identity, and an
    eviction is where an operator most wants it: it names the claim that
    was just taken away."""
    execution_id = _eid()
    build_id = await _registered(client, "evict-echo")
    assert (
        await _start(
            client,
            build_id,
            "evict-echo",
            claim="true",
            execution_id=execution_id,
            limit_key="evict-echo-key",
            enforce_limits="true",
        )
    ).status_code == 200

    evicted = await client.post(
        "/api/v1/concurrency-limits/evict-echo-key/holders/evict-echo/evict"
    )

    assert evicted.status_code == 200, evicted.text
    assert evicted.json()["execution_id"] == execution_id


# --- Dialect -------------------------------------------------------------


@pytest.mark.skipif(
    not os.environ.get("STARDAG_API_TEST_DATABASE_URL"),
    reason="PostgreSQL test database URL not configured",
)
async def test_claim_identity_on_postgres(
    pg_client: AsyncClient, pg_session: AsyncSession
):
    """The rule, on the dialect production runs.

    Worth its own test rather than trusting the SQLite pass: the column
    is a native ``uuid`` on Postgres and text on SQLite, while the event
    metadata carrying the id is JSONB against JSON. So the comparison is
    between a ``UUID`` object and a string there and between two strings
    here, and a rule written against one can pass while the other
    silently never matches — which would grant every second attempt.
    """
    build_id = await _new_build(pg_client)
    await pg_client.post(f"{BUILDS}/{build_id}/tasks", json=_register("pg-claim"))
    execution_id = _eid()
    assert (
        await _start(
            pg_client, build_id, "pg-claim", claim="true", execution_id=execution_id
        )
    ).status_code == 200

    retry = await _start(
        pg_client, build_id, "pg-claim", claim="true", execution_id=execution_id
    )
    second = await _start(
        pg_client, build_id, "pg-claim", claim="true", execution_id=_eid()
    )

    assert retry.status_code == 200, retry.text
    assert second.status_code == 409, second.text

    row = (
        await pg_session.execute(select(Task).where(Task.task_id == "pg-claim"))
    ).scalar_one()
    assert str(row.latest_execution_id) == execution_id
