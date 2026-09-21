"""The execution's own identity, and the two things it settles.

An execution claim is taken *before* the spawn — the claim and any
concurrency-limit slots have to be acquired in one transaction, so a
denied task never occupies a worker — which means there is no executor
ref at claim time and never was. ``execution_id`` is the identity that
exists anyway, minted by the caller, and it closes two holes the ref
could not.

**A retried claiming start is the same execution asking again.** The
registry client retries a POST whose response was lost, so a claiming
start that succeeded can be delivered twice. Refused, the second delivery
tells a worker somebody else holds the task, so it stands down from one
it holds the claim on and the task sits claimed and not running until the
claim expires — the worst outcome available at that endpoint. The id
distinguishes the retry from a genuine second attempt, which the build id
alone cannot (two attempts of one build are legitimately distinct) and
the ref cannot (there is not one yet).

**A start from a superseded execution must not evict the live holder.**
A worker's own start is non-claiming and used to be folded in
unconditionally. A preemption brings the claim expiry forward to a short
restart grace; if the restart is late the claim lapses, a neighbour
claims and spawns, and then the original restart lands and its worker's
start takes the task back. Two executions of one task.

Both rules are written so that **absence is never a mismatch**: an SDK
that mints no id behaves exactly as it did before the column existed, and
so does a task claimed before it. "Time passes" is simulated by rewriting
``latest_status_expires_at``, as in ``test_claim_expiry``.
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

    The shape both engines actually produce: the claim goes in before the
    spawn, so the only identity on the row is the minted one.
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
    from its identity map with the values it had then. Reading the same
    row twice around a request therefore returns the same object and the
    same attributes, and an assertion that something *changed* passes
    vacuously — which is exactly how it failed here first.
    """
    session.expire_all()
    return (
        await session.execute(select(Task).where(Task.task_id == task_id))
    ).scalar_one()


async def _expire(session: AsyncSession, task_id: str) -> None:
    """Bring a claim's expiry into the past — the restart that never came."""
    await session.execute(
        update(Task)
        .where(Task.task_id == task_id)
        .values(
            latest_status_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)
        )
    )
    await session.commit()


# --- The retried claim (STA-56) -----------------------------------------


async def test_a_retried_ref_less_claim_is_granted(client: AsyncClient):
    """The case the pair could never answer, because a claim has no ref.

    Same build, same minted id, no executor ref anywhere: this is the
    reactive and resident engines' actual claim, re-delivered. Before the
    id it fell through to a 409 and the winner stood down.
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
    distinct executions, and granting on the build alone would hand out
    real double-claims."""
    build_id, _ = await _claimed(client, "second-attempt", _eid())

    again = await _start(
        client, build_id, "second-attempt", claim="true", execution_id=_eid()
    )

    assert again.status_code == 409, again.text
    assert again.json()["detail"]["error_code"] == "task_already_running"


async def test_a_second_attempt_after_a_failed_spawn_is_still_refused(
    client: AsyncClient,
):
    """A failed spawn leaves the claim held and no ref recorded — the state
    a "same build, nothing recorded yet" rule would have granted. It is
    still a second execution, and the id says so where the absent ref
    could not."""
    build_id, _ = await _claimed(client, "failed-spawn", _eid())

    again = await _start(
        client, build_id, "failed-spawn", claim="true", execution_id=_eid()
    )

    assert again.status_code == 409, again.text
    assert again.json()["detail"]["error_code"] == "task_already_running"


async def test_another_build_with_the_same_id_is_still_refused(
    client: AsyncClient,
):
    """Build ownership is tested first, so a neighbour that somehow named
    the winner's execution is refused like any other second claimant."""
    execution_id = _eid()
    await _claimed(client, "shared-id", execution_id)
    other_build = await _new_build(client)

    denied = await _start(
        client, other_build, "shared-id", claim="true", execution_id=execution_id
    )

    assert denied.status_code == 409, denied.text
    assert denied.json()["detail"]["error_code"] == "task_already_running"


async def test_a_superseded_start_is_told_which_execution_holds_the_task(
    client: AsyncClient, async_session: AsyncSession
):
    """A refused worker can log what superseded it rather than only that
    something did — which is the signal cooperative cancellation reads."""
    build_a, a_execution = await _claimed(client, "denial-names", _eid())
    await _expire(async_session, "denial-names")
    build_b = await _new_build(client)
    b_execution = _eid()
    await _start(
        client, build_b, "denial-names", claim="true", execution_id=b_execution
    )

    refused = await _start(client, build_a, "denial-names", execution_id=a_execution)

    detail = refused.json()["detail"]
    assert detail["execution_id"] == b_execution
    assert detail["latest_status_build_id"] == build_b


async def test_a_claim_with_no_id_keeps_the_pair_rule(client: AsyncClient):
    """An SDK that mints no id is unaffected: the ``(executor, ref)`` pair
    still decides, and a request with neither is still refused."""
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


# --- The superseded start (STA-49) --------------------------------------


async def test_a_start_from_a_superseded_execution_is_refused(
    client: AsyncClient, async_session: AsyncSession
):
    """The whole sequence, which is the one that produced two executions.

    A claims and spawns; the restart it was promised is late; the claim
    lapses; B claims and spawns its own; A's original worker finally
    checks in. Its non-claiming start must not take the task back.
    """
    a_execution = _eid()
    build_a, _ = await _claimed(client, "superseded", a_execution)
    await _expire(async_session, "superseded")

    b_execution = _eid()
    build_b = await _new_build(client)
    claimed_by_b = await _start(
        client, build_b, "superseded", claim="true", execution_id=b_execution
    )
    assert claimed_by_b.status_code == 200, claimed_by_b.text

    late = await _start(
        client,
        build_a,
        "superseded",
        execution_id=a_execution,
        executor="modal",
        executor_ref="fc-a",
    )

    assert late.status_code == 409, late.text
    detail = late.json()["detail"]
    assert detail["error_code"] == "execution_superseded"
    assert detail["execution_id"] == b_execution

    row = await _task_row(async_session, "superseded")
    assert str(row.latest_status_build_id) == build_b, "A took the task back"
    assert str(row.latest_execution_id) == b_execution
    assert row.latest_executor_ref is None, "A's ref was recorded over B's claim"


async def test_the_same_execution_re_recording_itself_is_accepted(
    client: AsyncClient, async_session: AsyncSession
):
    """The hot path this must not break: after the claim, the tick records
    the ref and the worker self-reports, both under the id the claim was
    taken with. Three starts, one execution."""
    execution_id = _eid()
    build_id, _ = await _claimed(client, "re-recording", execution_id)

    with_ref = await _start(
        client,
        build_id,
        "re-recording",
        execution_id=execution_id,
        executor="modal",
        executor_ref="fc-1",
    )
    worker = await _start(
        client,
        build_id,
        "re-recording",
        execution_id=execution_id,
        executor="modal",
        executor_ref="fc-1",
    )

    assert with_ref.status_code == 200, with_ref.text
    assert worker.status_code == 200, worker.text
    row = await _task_row(async_session, "re-recording")
    assert str(row.latest_execution_id) == execution_id
    assert row.latest_executor_ref == "fc-1"


async def test_a_lapsed_claim_does_not_refuse_a_new_execution(
    client: AsyncClient, async_session: AsyncSession
):
    """A claim past its expiry protects nothing, so the ordinary
    self-heal — a non-claiming start taking a dead holder's task over —
    keeps working. Gating on RUNNING alone instead of on a live claim
    would have broken exactly this."""
    build_id, _ = await _claimed(client, "lapsed-takeover", _eid())
    await _expire(async_session, "lapsed-takeover")

    fresh = _eid()
    taken = await _start(client, build_id, "lapsed-takeover", execution_id=fresh)

    assert taken.status_code == 200, taken.text
    row = await _task_row(async_session, "lapsed-takeover")
    assert str(row.latest_execution_id) == fresh


async def test_a_retry_lets_a_new_execution_start(
    client: AsyncClient, async_session: AsyncSession
):
    """The commonest shape of all: the task failed, was retried, and runs
    again under a new identity. Refusing on "the ids differ" alone would
    have made every second attempt unstartable."""
    execution_id = _eid()
    build_id, _ = await _claimed(client, "retry-new-exec", execution_id)
    await client.post(f"{BUILDS}/{build_id}/tasks/retry-new-exec/fail")
    await client.post(f"{BUILDS}/{build_id}/tasks/retry-new-exec/retry")

    row = await _task_row(async_session, "retry-new-exec")
    assert row.latest_execution_id is None, "a retry leaves the dead id behind"

    again = await _start(client, build_id, "retry-new-exec", execution_id=_eid())
    assert again.status_code == 200, again.text


async def test_a_start_with_no_id_is_never_refused(
    client: AsyncClient, async_session: AsyncSession
):
    """Half a rolling deploy: the task was claimed with an id and the
    reporter has none. Nothing to compare is not a mismatch — refusing
    would turn a version skew into tasks that look unstarted."""
    build_id, _ = await _claimed(client, "no-id-start", _eid())

    old_sdk = await _start(
        client, build_id, "no-id-start", executor="modal", executor_ref="fc-1"
    )

    assert old_sdk.status_code == 200, old_sdk.text
    row = await _task_row(async_session, "no-id-start")
    assert row.latest_execution_id is None, "a start with no id records none"


async def test_a_start_with_an_id_on_a_task_holding_none_is_accepted(
    client: AsyncClient, async_session: AsyncSession
):
    """The other half of the same deploy: a task claimed before the column
    meant anything, met by a reporter that mints one. There is no recorded
    execution to contradict, so the id is simply adopted."""
    build_id = await _registered(client, "adopts-id")
    assert (
        await _start(client, build_id, "adopts-id", claim="true")
    ).status_code == 200

    fresh = _eid()
    adopted = await _start(client, build_id, "adopts-id", execution_id=fresh)

    assert adopted.status_code == 200, adopted.text
    row = await _task_row(async_session, "adopts-id")
    assert str(row.latest_execution_id) == fresh


async def test_a_claiming_start_is_never_refused_as_superseded(
    client: AsyncClient, async_session: AsyncSession
):
    """The supersession rule is for *non-claiming* starts only. A claiming
    start meeting a live claim is arbitrated — 409 ``task_already_running``
    — and a claiming start that wins is the healing mechanism and must
    replace whatever identity was there."""
    build_a, _ = await _claimed(client, "claim-not-superseded", _eid())

    denied = await _start(
        client, build_a, "claim-not-superseded", claim="true", execution_id=_eid()
    )

    assert denied.status_code == 409
    assert denied.json()["detail"]["error_code"] == "task_already_running", (
        "a claiming start must be arbitrated, not refused as superseded"
    )


# --- Reports name their execution ---------------------------------------


async def test_an_interruption_from_a_superseded_execution_is_refused(
    client: AsyncClient, async_session: AsyncSession
):
    """Same authority rule as the start, reached through the report path:
    a worker whose claim was taken over must not move the live holder's
    task to INTERRUPTED."""
    build_a, a_execution = await _claimed(client, "stale-interrupt", _eid())
    await _expire(async_session, "stale-interrupt")
    build_b = await _new_build(client)
    await _start(client, build_b, "stale-interrupt", claim="true", execution_id=_eid())

    await client.post(
        f"{BUILDS}/{build_a}/tasks/stale-interrupt/interrupt",
        params={"execution_id": a_execution, "reason": "preempted"},
    )

    row = await _task_row(async_session, "stale-interrupt")
    assert row.latest_status == "running", "a dead execution ended a live one"


async def test_an_interruption_naming_the_current_execution_applies(
    client: AsyncClient, async_session: AsyncSession
):
    build_id, execution_id = await _claimed(client, "live-interrupt", _eid())

    await client.post(
        f"{BUILDS}/{build_id}/tasks/live-interrupt/interrupt",
        params={"execution_id": execution_id, "reason": "timeout"},
    )

    row = await _task_row(async_session, "live-interrupt")
    assert row.latest_status == "interrupted"


async def test_an_interruption_naming_an_id_the_task_has_none_for_applies(
    client: AsyncClient, async_session: AsyncSession
):
    """A missing *current* id is no opinion, unlike a missing current ref.

    The ref has a real gap — a replacement's claiming start clears it
    before the spawn records the new one — so NULL there cannot be a
    wildcard. A replacement mints its id *before* claiming, so that gap
    does not exist, and NULL means only that the running execution
    predates the identity. Refusing there would drop the report and leave
    the task RUNNING behind a claim nobody releases.
    """
    build_id = await _registered(client, "no-current-id")
    await _start(client, build_id, "no-current-id", claim="true")

    await client.post(
        f"{BUILDS}/{build_id}/tasks/no-current-id/interrupt",
        params={"execution_id": _eid(), "reason": "timeout"},
    )

    row = await _task_row(async_session, "no-current-id")
    assert row.latest_status == "interrupted"


async def test_a_preemption_restart_under_the_same_id_re_grants_the_claim(
    client: AsyncClient, async_session: AsyncSession
):
    """Modal restarts a preempted input under the same call id, and the
    restarted worker re-sends the same execution id. That is the same
    execution by construction, so it must be accepted and get its full
    claim back rather than be read as a superseding one."""
    build_id, execution_id = await _claimed(client, "preempt-restart", _eid())
    await client.post(
        f"{BUILDS}/{build_id}/tasks/preempt-restart/preempt",
        params={"execution_id": execution_id},
    )
    row = await _task_row(async_session, "preempt-restart")
    shortened = row.latest_status_expires_at
    assert shortened is not None, "the preemption did not shorten the claim"

    restart = await _start(
        client, build_id, "preempt-restart", execution_id=execution_id
    )

    assert restart.status_code == 200, restart.text
    row = await _task_row(async_session, "preempt-restart")
    assert row.latest_status == "running"
    re_granted = row.latest_status_expires_at
    assert re_granted is not None and re_granted > shortened, (
        "the restart did not re-grant the full claim"
    )


# --- Dialect ------------------------------------------------------------


@pytest.mark.skipif(
    not os.environ.get("STARDAG_API_TEST_DATABASE_URL"),
    reason="PostgreSQL test database URL not configured",
)
async def test_execution_identity_on_postgres(
    pg_client: AsyncClient, pg_session: AsyncSession
):
    """Both rules, on the dialect production runs.

    Worth its own test rather than trusting the SQLite pass: the column is
    a native ``uuid`` on Postgres and text on SQLite, while the event
    metadata carrying the id is JSONB on one and JSON on the other. So the
    comparison is between a ``UUID`` object and a string there and between
    two strings here, and a rule written against one can pass while the
    other silently never matches — which would grant every retry *and*
    refuse nothing.
    """
    build_id = await _new_build(pg_client)
    await pg_client.post(f"{BUILDS}/{build_id}/tasks", json=_register("pg-exec"))
    execution_id = _eid()
    assert (
        await _start(
            pg_client, build_id, "pg-exec", claim="true", execution_id=execution_id
        )
    ).status_code == 200

    retry = await _start(
        pg_client, build_id, "pg-exec", claim="true", execution_id=execution_id
    )
    second = await _start(
        pg_client, build_id, "pg-exec", claim="true", execution_id=_eid()
    )
    superseding = await _start(pg_client, build_id, "pg-exec", execution_id=_eid())

    assert retry.status_code == 200, retry.text
    assert second.status_code == 409, second.text
    assert superseding.status_code == 409, superseding.text
    assert superseding.json()["detail"]["error_code"] == "execution_superseded"

    row = (
        await pg_session.execute(select(Task).where(Task.task_id == "pg-exec"))
    ).scalar_one()
    assert str(row.latest_execution_id) == execution_id
