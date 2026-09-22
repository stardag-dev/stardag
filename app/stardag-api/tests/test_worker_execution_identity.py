"""What the worker's own identity buys, on the server side.

Three rules, all reading the same ``tasks.latest_execution_id`` the claim
mints, and all written against one invariant:

    A report is about the current execution **unless an identity both
    sides carry contradicts it**. Absence on either side is no opinion,
    never a mismatch.

The rules are: a *non-claiming* start naming a superseded execution is
refused (STA-49); an interruption or preemption report is honoured only
while the task still holds the execution it names; and a worker can ask
whether it is still wanted at all.

Two things these tests are deliberately built to catch, because both have
already happened once on this surface.

**What stops being written.** Making a request newly-refused is the mirror
of making one newly-granted, and #361's one regression was the latter: the
success path ran on inputs it had never seen and blanked a field. So the
refusal tests assert *surviving state* — the live holder's ref, owner and
identity, and its attempt count — rather than only the status code.

**The replays must agree with the row.** The task row answers the
environment-global question and the two per-build replays answer the one
the UI and the frontier read. A report the row refuses but a replay
applies shows one task as INTERRUPTED in the UI and RUNNING in the
frontier, and nothing in the code makes them agree — only a test can.
Ownership is the one axis where they are *meant* to part, because a
build-scoped walk cannot see a takeover; that is pinned too, so the gap
is not closed by mistake.
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


async def _report(
    client: AsyncClient, build_id: str, task_id: str, kind: str, **params
) -> Response:
    return await client.post(
        f"{BUILDS}/{build_id}/tasks/{task_id}/{kind}", params=params
    )


async def _registered(client: AsyncClient, task_id: str) -> str:
    build_id = await _new_build(client)
    await client.post(f"{BUILDS}/{build_id}/tasks", json=_register(task_id))
    return build_id


async def _running(
    client: AsyncClient, task_id: str, execution_id: str | None, ref: str | None = None
) -> str:
    """A task claimed and spawned by a fresh build — the live-holder shape.

    Claim first (no ref, as the tick does it), then the post-spawn start
    that records the ref under the same identity. Doing both is what makes
    the refusal tests meaningful: a row with a ref *and* an id is what a
    superseded start would overwrite.
    """
    build_id = await _registered(client, task_id)
    params: dict = {"claim": "true"}
    if execution_id is not None:
        params["execution_id"] = execution_id
    assert (await _start(client, build_id, task_id, **params)).status_code == 200
    if ref is not None:
        spawn: dict = {"executor": "modal", "executor_ref": ref}
        if execution_id is not None:
            spawn["execution_id"] = execution_id
        assert (await _start(client, build_id, task_id, **spawn)).status_code == 200
    return build_id


async def _task_row(session: AsyncSession, task_id: str) -> Task:
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


# --- STA-49: a superseded start cannot take the task back ----------------


async def test_a_superseded_non_claiming_start_is_refused(
    client: AsyncClient, async_session: AsyncSession
):
    """The STA-49 sequence, end to end on the server.

    Build A claims and spawns; its claim lapses; build B takes the task
    over with its own identity. A's restarted worker then reports its own
    start — non-claiming, as every worker's is — naming the execution it
    really is. Before this rule that start rewrote the status, the owner,
    the executor fields and the claim expiry, and two containers ran one
    task.
    """
    a_execution, b_execution = _eid(), _eid()
    build_a = await _running(client, "sta49", a_execution, ref="fc-a")
    await _expire(async_session, "sta49")

    build_b = await _registered(client, "sta49")
    assert (
        await _start(client, build_b, "sta49", claim="true", execution_id=b_execution)
    ).status_code == 200
    assert (
        await _start(
            client,
            build_b,
            "sta49",
            executor="modal",
            executor_ref="fc-b",
            execution_id=b_execution,
        )
    ).status_code == 200

    refused = await _start(
        client,
        build_a,
        "sta49",
        executor="modal",
        executor_ref="fc-a",
        execution_id=a_execution,
    )

    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["error_code"] == "execution_superseded"
    assert refused.json()["detail"]["execution_id"] == b_execution

    # Everything the refusal protects, asserted as *surviving* rather than
    # inferred from the status code. A refusal that rolled back the wrong
    # things would look identical from the response alone.
    row = await _task_row(async_session, "sta49")
    assert str(row.latest_execution_id) == b_execution
    assert str(row.latest_status_build_id) == build_b
    assert row.latest_executor_ref == "fc-b"
    assert row.latest_status_expires_at is not None


async def test_a_refused_start_records_nothing_and_spends_no_attempt(
    client: AsyncClient, async_session: AsyncSession
):
    """A 409 rolls the transaction back — no event, so no attempt.

    The counterpart of the lesson STA-44 left: an event recorded but
    refused needs bookkeeping so it does not spend a budget it should
    not. Here nothing is recorded at all, which is simpler *and* has to
    be checked in both directions — a start that vanishes is as capable
    of breaking a downstream count as one that lands. The attempt count
    is what the frontier's retry budget is measured against, and on
    STA-44 a refused report made that budget silently *grow*.
    """
    from stardag_api.services.status import get_task_status_in_build

    a_execution, b_execution = _eid(), _eid()
    build_a = await _running(client, "no-attempt", a_execution, ref="fc-a")
    build_b = await _registered(client, "no-attempt")

    before = (await client.get(f"{BUILDS}/{build_b}/events")).json()

    # B is not the holder, so B's *own* worker naming a foreign execution
    # is the refused shape here.
    refused = await _start(
        client,
        build_b,
        "no-attempt",
        executor="modal",
        executor_ref="fc-b",
        execution_id=b_execution,
    )
    assert refused.status_code == 409, refused.text

    after = (await client.get(f"{BUILDS}/{build_b}/events")).json()
    assert len(after) == len(before), "the refused start left an event behind"

    row = await _task_row(async_session, "no-attempt")
    _, _, _, _, b_attempts = await get_task_status_in_build(
        async_session, uuid.UUID(build_b), row.id
    )
    assert b_attempts == 0, "the refused start spent one of B's attempts"

    # And the build that does hold it still reads as RUNNING, on the one
    # attempt its own claim spent.
    status, _, _, _, a_attempts = await get_task_status_in_build(
        async_session, uuid.UUID(build_a), row.id
    )
    assert status.value == "running"
    assert a_attempts == 1


async def test_a_restart_under_the_same_identity_is_accepted(
    client: AsyncClient, async_session: AsyncSession
):
    """The case the refusal must not catch.

    Modal restarts a preempted input under the *same* call id, and the
    restarted worker re-sends the identity its claim was taken with. By
    construction that is still the same execution, so it is accepted —
    and it has to be, because refusing it leaves a task that looks
    unstarted.
    """
    execution_id = _eid()
    build_id = await _running(client, "restart", execution_id, ref="fc-1")

    restarted = await _start(
        client,
        build_id,
        "restart",
        executor="modal",
        executor_ref="fc-1",
        execution_id=execution_id,
    )

    assert restarted.status_code == 200, restarted.text
    row = await _task_row(async_session, "restart")
    assert str(row.latest_execution_id) == execution_id


async def test_a_start_with_no_identity_is_accepted(client: AsyncClient):
    """Version skew is not a mismatch.

    An SDK predating the id sends none, and the whole population of tasks
    claimed before the column existed has none either. Refusing on either
    absence would turn a rolling deploy into tasks that look unstarted.
    """
    build_id = await _running(client, "no-identity", _eid(), ref="fc-1")

    old_sdk = await _start(
        client, build_id, "no-identity", executor="modal", executor_ref="fc-1"
    )

    assert old_sdk.status_code == 200, old_sdk.text


async def test_a_start_naming_an_id_against_a_task_holding_none(
    client: AsyncClient, async_session: AsyncSession
):
    """The ratified residual gap, pinned so a change to it is deliberate.

    A report naming an id, against a task that carries *neither* an id nor
    a ref, is accepted: there is nothing recorded to contradict it. That
    population is a pre-identity SDK's claim (a current SDK's claim always
    writes the id, even when it writes none) or the limiter's enforced
    start. Accepting admits only a caller who already knows the build and
    task ids and who displaces no live claim, because there is none
    recorded to displace.
    """
    build_id = await _registered(client, "no-holder-id")
    assert (
        await _start(client, build_id, "no-holder-id", claim="true")
    ).status_code == 200
    row = await _task_row(async_session, "no-holder-id")
    assert row.latest_execution_id is None and row.latest_executor_ref is None

    accepted = await _start(
        client,
        build_id,
        "no-holder-id",
        executor="modal",
        executor_ref="fc-1",
        execution_id=_eid(),
    )

    assert accepted.status_code == 200, accepted.text


async def test_a_lapsed_claim_does_not_protect_anything(
    client: AsyncClient, async_session: AsyncSession
):
    """No live claim, nothing to supersede.

    A task past its expiry is up for grabs, and a non-claiming start
    taking it over is the ordinary retry and self-heal path. Gating the
    refusal on RUNNING alone instead of on a *live* claim would refuse
    every legitimate re-run.
    """
    build_id = await _running(client, "lapsed", _eid(), ref="fc-1")
    await _expire(async_session, "lapsed")

    taken_over = await _start(
        client,
        build_id,
        "lapsed",
        executor="modal",
        executor_ref="fc-2",
        execution_id=_eid(),
    )

    assert taken_over.status_code == 200, taken_over.text


async def test_a_claiming_start_is_not_subject_to_the_refusal(client: AsyncClient):
    """A claiming start is exactly the event that may change the holder.

    It goes through claim arbitration instead — which answers 409
    ``task_already_running``, a different question with a different
    answer for the caller.
    """
    build_a = await _running(client, "claiming", _eid(), ref="fc-a")
    build_b = await _registered(client, "claiming")

    denied = await _start(
        client, build_b, "claiming", claim="true", execution_id=_eid()
    )

    assert denied.status_code == 409
    assert denied.json()["detail"]["error_code"] == "task_already_running"
    assert build_a  # the holder, named in the denial below
    assert denied.json()["detail"]["executor_ref"] == "fc-a"


# --- The report rules read the same identity -----------------------------


async def test_an_interruption_naming_a_superseded_execution_is_refused(
    client: AsyncClient, async_session: AsyncSession
):
    """The authority rule, now with the identity as its sharper half.

    The ref could only answer this once a spawn had recorded one; the id
    exists from the claim onward. Here the *ref* is even reused, so only
    the identity can tell the two executions apart.
    """
    a_execution, b_execution = _eid(), _eid()
    await _running(client, "stale-report", a_execution, ref="fc-shared")
    await _expire(async_session, "stale-report")
    build_b = await _registered(client, "stale-report")
    assert (
        await _start(
            client, build_b, "stale-report", claim="true", execution_id=b_execution
        )
    ).status_code == 200
    assert (
        await _start(
            client,
            build_b,
            "stale-report",
            executor="modal",
            executor_ref="fc-shared",
            execution_id=b_execution,
        )
    ).status_code == 200

    # B's own worker reports, but names A's dead execution.
    await _report(
        client,
        build_b,
        "stale-report",
        "interrupt",
        reason="timeout",
        executor_ref="fc-shared",
        execution_id=a_execution,
    )

    row = await _task_row(async_session, "stale-report")
    assert row.latest_status == "running", (
        "a report about a superseded execution moved the live one"
    )


async def test_a_report_naming_the_current_execution_applies(
    client: AsyncClient, async_session: AsyncSession
):
    execution_id = _eid()
    build_id = await _running(client, "live-report", execution_id, ref="fc-1")

    await _report(
        client,
        build_id,
        "live-report",
        "interrupt",
        reason="timeout",
        executor_ref="fc-1",
        execution_id=execution_id,
    )

    row = await _task_row(async_session, "live-report")
    assert row.latest_status == "interrupted"


async def test_a_report_that_agrees_on_the_id_but_not_the_ref_is_refused(
    client: AsyncClient, async_session: AsyncSession
):
    """Both identities get a vote when both sides carry both.

    This is not belt-and-braces. A start carrying no id leaves the
    recorded one in place, so an id-less *replacement* inside the same
    build inherits its predecessor's id along with its owner — and then
    only the ref has moved on.
    """
    execution_id = _eid()
    build_id = await _running(client, "ref-disagrees", execution_id, ref="fc-current")

    await _report(
        client,
        build_id,
        "ref-disagrees",
        "interrupt",
        reason="timeout",
        executor_ref="fc-gone",
        execution_id=execution_id,
    )

    row = await _task_row(async_session, "ref-disagrees")
    assert row.latest_status == "running"


# --- The replays must give the row's answer ------------------------------


@pytest.mark.parametrize(
    "reported_execution,reported_ref,expected",
    [
        pytest.param("current", "fc-1", "interrupted", id="both-match"),
        pytest.param("other", "fc-1", "running", id="id-is-stale"),
        pytest.param("current", "fc-gone", "running", id="ref-is-stale"),
        pytest.param(None, "fc-1", "interrupted", id="no-id-sent"),
        pytest.param(None, None, "interrupted", id="nothing-sent"),
        pytest.param("current", None, "interrupted", id="id-only"),
    ],
)
async def test_the_replays_agree_with_the_row(
    client: AsyncClient,
    async_session: AsyncSession,
    reported_execution: str | None,
    reported_ref: str | None,
    expected: str,
):
    """One rule, three readers, and nothing but this makes them agree.

    ``_reports_on_the_current_execution`` folds the task row;
    ``_replay_report_applies`` answers the per-build view twice over, once
    per task and once for the whole build. They share
    ``_names_the_execution`` precisely so they cannot drift — but sharing
    a helper is not the same as calling it with the same inputs, and the
    replays track their ``current_execution_id`` off the event stream
    rather than reading a column. A divergence here is invisible in
    production except as a task showing one status in the UI and another
    in the frontier.
    """
    task_id = f"replay-{reported_execution}-{reported_ref}"
    execution_id = _eid()
    build_id = await _running(client, task_id, execution_id, ref="fc-1")

    params: dict = {"reason": "timeout"}
    if reported_ref is not None:
        params["executor_ref"] = reported_ref
    if reported_execution == "current":
        params["execution_id"] = execution_id
    elif reported_execution == "other":
        params["execution_id"] = _eid()
    reported = await _report(client, build_id, task_id, "interrupt", **params)
    assert reported.status_code == 200, reported.text

    row = await _task_row(async_session, task_id)
    assert row.latest_status == expected, "the task row's fold disagrees"

    # The per-task replay. Every task-event response carries its answer in
    # ``status``, which is how the SDK and the UI learn what an event did
    # — so a divergence here is visible to a caller immediately.
    assert reported.json()["status"] == expected, (
        "get_task_status_in_build disagrees with the row it must match"
    )


async def test_the_replays_survive_a_claim_redelivery(
    client: AsyncClient, async_session: AsyncSession
):
    """The sequence my first agreement test did not cover, and should have.

    Claim, post-spawn start recording the ref, then the claim's lost answer
    re-delivered and now granted. That last event carries the id the task
    already holds and no executor of its own, and the row's fold
    deliberately **preserves** the ref through it — otherwise nothing could
    re-attach to the live worker.

    A replay that clears there instead diverges in a way that only shows up
    on the *next* report: one naming a stale ref is refused by the row
    (ref mismatch) and applied by the replay (no current ref to contradict
    it), which is one task INTERRUPTED in the UI and RUNNING in the
    frontier. The parametrised test above misses it because every case
    there reaches the report in one start.
    """
    execution_id = _eid()
    build_id = await _running(client, "redelivered", execution_id, ref="fc-1")

    # The claim's answer was lost; the client re-sends it, and it is now
    # granted rather than refused — which is what STA-50 bought.
    redelivered = await _start(
        client, build_id, "redelivered", claim="true", execution_id=execution_id
    )
    assert redelivered.status_code == 200, redelivered.text

    row = await _task_row(async_session, "redelivered")
    assert row.latest_executor_ref == "fc-1", (
        "the re-delivered claim erased the ref the spawn recorded"
    )

    # Now a report from an execution that is gone, naming the same identity
    # but the reference it used to have.
    reported = await _report(
        client,
        build_id,
        "redelivered",
        "interrupt",
        reason="timeout",
        executor_ref="fc-stale",
        execution_id=execution_id,
    )

    row = await _task_row(async_session, "redelivered")
    assert row.latest_status == "running", "the row applied a stale report"
    assert reported.json()["status"] == "running", (
        "the per-task replay applied a report the row refused"
    )


async def test_a_replay_keeps_its_own_view_after_a_takeover(
    client: AsyncClient, async_session: AsyncSession
):
    """Where the agreement stops, pinned so nobody closes it by mistake.

    The row has a test a replay cannot make: "still RUNNING under the
    *reporting* build". A build-scoped walk never sees another build's
    events, so after a takeover the row refuses this build's late report
    and the replay applies it, and the two disagree. That is the one axis
    where they are allowed to, and it looks enough like a hole that it has
    already been reported as one.

    It is the right answer, and the direction is what makes it so. The
    views disagree *before* the report lands — A's walk says RUNNING about
    an execution that is dead, the row says RUNNING about B's — so
    applying the report is what ends the disagreement that matters,
    leaving A a true statement about A's own execution. Skipping refused
    reports here, which is the obvious "fix", would freeze A's view at
    RUNNING for an execution nothing will ever finish: the silent-stall
    class STA-44 exists to remove, reintroduced in the per-build view.

    The refusal marker is still honoured where it decides something — the
    attempt tally — which ``test_a_refused_start_records_nothing_and_
    spends_no_attempt`` and the STA-44 suite cover.
    """
    from stardag_api.services.status import get_task_status_in_build

    a_execution, b_execution = _eid(), _eid()
    build_a = await _running(client, "takeover", a_execution, ref="fc-a")
    await _expire(async_session, "takeover")
    build_b = await _running(client, "takeover", b_execution, ref="fc-b")

    # A's worker, restarted late, reports the end of the execution it was
    # running. The row refuses it: B holds the task now.
    reported = await _report(
        client,
        build_a,
        "takeover",
        "interrupt",
        reason="timeout",
        executor_ref="fc-a",
        execution_id=a_execution,
    )
    assert reported.status_code == 200, reported.text

    row = await _task_row(async_session, "takeover")
    assert row.latest_status == "running", "A's report evicted the live holder"
    assert str(row.latest_status_build_id) == build_b
    assert str(row.latest_execution_id) == b_execution
    assert row.latest_executor_ref == "fc-b"

    # A's own view moves, and that is the intent.
    assert reported.json()["status"] == "interrupted"
    a_status, _, _, _, _ = await get_task_status_in_build(
        async_session, uuid.UUID(build_a), row.id
    )
    assert a_status.value == "interrupted"

    # B's is untouched by a report about somebody else's execution.
    b_status, _, _, _, _ = await get_task_status_in_build(
        async_session, uuid.UUID(build_b), row.id
    )
    assert b_status.value == "running"


# --- Cooperative cancellation's question ---------------------------------


async def test_execution_status_says_current_while_all_is_well(client: AsyncClient):
    execution_id = _eid()
    build_id = await _running(client, "still-mine", execution_id, ref="fc-1")

    answer = (
        await client.get(
            f"{BUILDS}/{build_id}/tasks/still-mine/execution-status",
            params={"execution_id": execution_id},
        )
    ).json()

    assert answer["still_current"] is True
    assert answer["reason"] is None
    assert answer["build_status"] == "running"
    assert answer["latest_execution_id"] == execution_id


async def test_execution_status_reports_a_cancelled_build(client: AsyncClient):
    """The case cooperative cancellation mostly exists for.

    Note what it does *not* depend on: cancelling a build releases its
    claims but replaces nobody, so the task may still name this very
    execution. A check that only compared identities would answer "still
    current" here and the worker would run on.
    """
    execution_id = _eid()
    build_id = await _running(client, "cancelled-build", execution_id, ref="fc-1")
    await client.post(f"{BUILDS}/{build_id}/cancel")

    answer = (
        await client.get(
            f"{BUILDS}/{build_id}/tasks/cancelled-build/execution-status",
            params={"execution_id": execution_id},
        )
    ).json()

    assert answer["still_current"] is False
    assert answer["reason"] == "build_not_running"


async def test_execution_status_reports_a_cancelled_task(client: AsyncClient):
    """The cascade's shape, which the identity comparison cannot see.

    Releasing a task's claim is what lets the next build have it — and
    until one does, the row still names this very execution, so
    ``superseded`` is false and the build is still RUNNING. Without this
    third reason a cascaded worker would run on to completion.
    """
    execution_id = _eid()
    build_id = await _running(client, "cancelled-task", execution_id, ref="fc-1")

    cancelled = await client.post(f"{BUILDS}/{build_id}/tasks/cancelled-task/cancel")
    assert cancelled.status_code == 200, cancelled.text

    answer = (
        await client.get(
            f"{BUILDS}/{build_id}/tasks/cancelled-task/execution-status",
            params={"execution_id": execution_id},
        )
    ).json()

    assert answer["still_current"] is False
    assert answer["reason"] == "task_cancelled"
    assert answer["build_status"] == "running", (
        "the build was not the thing that stopped, so this is not the "
        "build_not_running case wearing a different name"
    )


@pytest.mark.parametrize("kind", ["interrupt", "suspend", "fail"])
async def test_a_workers_own_report_is_not_a_reason_to_stop_itself(
    client: AsyncClient, kind: str
):
    """Every other non-RUNNING status is something this worker just wrote.

    Reading its own report back as "you are no longer wanted" would have a
    worker cancel itself the moment it checkpointed an interruption — and
    the interruption path exists precisely so the task can be resumed.
    """
    execution_id = _eid()
    build_id = await _running(client, f"self-report-{kind}", execution_id, ref="fc-1")

    await _report(
        client,
        build_id,
        f"self-report-{kind}",
        kind,
        **(
            {"executor_ref": "fc-1", "execution_id": execution_id}
            if kind == "interrupt"
            else {}
        ),
    )

    answer = (
        await client.get(
            f"{BUILDS}/{build_id}/tasks/self-report-{kind}/execution-status",
            params={"execution_id": execution_id},
        )
    ).json()

    assert answer["still_current"] is True, (
        f"a {kind} report made the worker that sent it stop itself: {answer}"
    )


async def test_a_cancel_while_the_container_is_queued_is_not_undone(
    client: AsyncClient, async_session: AsyncSession
):
    """The hole a task-level cancel leaves in the queued window.

    Cancelling a task releases its claim, so there is no *live* claim for
    the supersession rule to protect and the identity still matches — the
    late worker's start is accepted, the fold turns CANCELLED back into
    RUNNING, and the pre-run checkpoint then reads a task that is running
    under exactly this execution. The worker runs a cancelled task to
    completion, which is the case cooperative cancellation is most often
    for.

    Reviving a task a build has declared not-to-be-run is a *claim's* job,
    after a reset. It is never a report's.
    """
    execution_id = _eid()
    build_id = await _running(client, "queued-cancel", execution_id, ref="fc-1")
    assert (
        await client.post(f"{BUILDS}/{build_id}/tasks/queued-cancel/cancel")
    ).status_code == 200

    # The container was queued through all of that, and now starts.
    late = await _start(
        client,
        build_id,
        "queued-cancel",
        executor="modal",
        executor_ref="fc-1",
        execution_id=execution_id,
    )

    assert late.status_code == 409, (
        "a cancelled task was revived by its own late worker's start"
    )
    assert late.json()["detail"]["error_code"] == "task_cancelled"

    row = await _task_row(async_session, "queued-cancel")
    assert row.latest_status == "cancelled"

    # And the checkpoint the worker takes next still says stop.
    answer = (
        await client.get(
            f"{BUILDS}/{build_id}/tasks/queued-cancel/execution-status",
            params={"execution_id": execution_id},
        )
    ).json()
    assert answer["still_current"] is False
    assert answer["reason"] == "task_cancelled"


@pytest.mark.parametrize(
    "params,what",
    [
        pytest.param({}, "the limiter's slot-occupying start", id="limiter"),
        pytest.param(
            {"executor": "modal", "executor_ref": "fc-1"},
            "a detached spawn from a claim=False build",
            id="claimless-detached",
        ),
    ],
)
async def test_a_start_with_no_identity_is_not_caught_by_that(
    client: AsyncClient, params: dict, what: str
):
    """The refusal is scoped to an identity, and only to an identity.

    Both of these describe real callers that must keep working. The
    limiter records a start to occupy slots and names nothing at all. A
    ``claim=False`` build still spawns detached, so its post-spawn start
    names an executor and a reference — and **that is the case an earlier
    version of this refusal broke**: picking up a task an earlier build
    left CANCELLED would have been refused, and under the default
    FAIL_FAST the whole build aborted, where before it simply ran the
    task.

    Absence of an identity is no opinion, which is the line every other
    rule here draws. Keying on the executor or the reference instead is
    what produced the regression.
    """
    task_id = f"no-identity-{len(params)}"
    build_id = await _registered(client, task_id)
    assert (
        await client.post(f"{BUILDS}/{build_id}/tasks/{task_id}/cancel")
    ).status_code == 200

    accepted = await _start(client, build_id, task_id, **params)

    assert accepted.status_code == 200, f"{what} was refused: {accepted.text}"


async def test_a_claiming_start_still_revives_a_cancelled_task(
    client: AsyncClient,
):
    """The healing path must stay open.

    A build that finds a task another build cancelled resets it and runs
    it. That is a claiming start, and claiming starts are arbitrated by
    the claim rather than by this refusal.
    """
    build_id = await _running(client, "revive", _eid(), ref="fc-1")
    assert (
        await client.post(f"{BUILDS}/{build_id}/tasks/revive/cancel")
    ).status_code == 200

    reclaimed = await _start(
        client, build_id, "revive", claim="true", execution_id=_eid()
    )

    assert reclaimed.status_code == 200, reclaimed.text


async def test_execution_status_reports_a_takeover(
    client: AsyncClient, async_session: AsyncSession
):
    a_execution, b_execution = _eid(), _eid()
    build_a = await _running(client, "taken-over", a_execution, ref="fc-a")
    await _expire(async_session, "taken-over")
    build_b = await _registered(client, "taken-over")
    assert (
        await _start(
            client, build_b, "taken-over", claim="true", execution_id=b_execution
        )
    ).status_code == 200

    answer = (
        await client.get(
            f"{BUILDS}/{build_a}/tasks/taken-over/execution-status",
            params={"execution_id": a_execution},
        )
    ).json()

    assert answer["still_current"] is False
    assert answer["reason"] == "superseded"
    assert answer["latest_execution_id"] == b_execution


async def test_execution_status_without_an_id_still_answers_the_build_half(
    client: AsyncClient,
):
    """A worker that was never given an identity is not left uncovered.

    The non-detached submission path and an orchestrator predating the id
    both produce one. It cannot be told it was superseded — there is
    nothing to compare — but it can be told its build has stopped, which
    is the case a user cancelling a build produces.
    """
    build_id = await _running(client, "id-less", None, ref="fc-1")

    while_running = (
        await client.get(f"{BUILDS}/{build_id}/tasks/id-less/execution-status")
    ).json()
    assert while_running["still_current"] is True

    await client.post(f"{BUILDS}/{build_id}/cancel")
    after_cancel = (
        await client.get(f"{BUILDS}/{build_id}/tasks/id-less/execution-status")
    ).json()

    assert after_cancel["still_current"] is False
    assert after_cancel["reason"] == "build_not_running"


async def test_execution_status_never_says_stop_on_a_missing_identity(
    client: AsyncClient,
):
    """An id on one side only is no opinion, not a mismatch.

    Same asymmetry as the report rules, and the same reason: the answer
    here is a *permission to stop*, and inventing one out of absence
    would kill healthy workers during a rolling deploy.
    """
    build_id = await _running(client, "half-identity", None, ref="fc-1")

    answer = (
        await client.get(
            f"{BUILDS}/{build_id}/tasks/half-identity/execution-status",
            params={"execution_id": _eid()},
        )
    ).json()

    assert answer["still_current"] is True


# --- The identity is readable ---------------------------------------------


async def test_the_task_read_models_surface_the_identity(client: AsyncClient):
    """**Every** task read, because each one is built separately.

    Three of them, and they do not share a constructor. The two under
    ``/tasks`` validate straight off the row, so a field added to
    ``TaskResponse`` appears on both for free. The build-scoped one
    hand-builds ``TaskWithStatusResponse`` — which *inherits* that field
    — so a new column is silently null there alone. That is exactly what
    happened to this one, and it survived seven review rounds and a
    hand-written audit before an eighth caught it.
    """
    execution_id = _eid()
    build_id = await _running(client, "surfaced", execution_id, ref="fc-1")

    detail = (await client.get("/api/v1/tasks/surfaced")).json()
    listing = (await client.get("/api/v1/tasks", params={"page_size": 100})).json()
    listed = next(t for t in listing["tasks"] if t["task_id"] == "surfaced")
    in_build = (await client.get(f"{BUILDS}/{build_id}/tasks")).json()
    scoped = next(t for t in in_build if t["task_id"] == "surfaced")

    assert detail["latest_execution_id"] == execution_id
    assert listed["latest_execution_id"] == execution_id
    assert scoped["latest_execution_id"] == execution_id, (
        "the build-scoped task read hand-builds its response, so an "
        "inherited field is null there unless someone adds it"
    )


# --- Dialect --------------------------------------------------------------


@pytest.mark.skipif(
    not os.environ.get("STARDAG_API_TEST_DATABASE_URL"),
    reason="PostgreSQL test database URL not configured",
)
async def test_the_refusal_on_postgres(
    pg_client: AsyncClient, pg_session: AsyncSession
):
    """The rule, on the dialect production runs.

    The same trap the claim rule has: ``latest_execution_id`` is a native
    ``uuid`` on Postgres and text on SQLite, while the event metadata
    carrying the reported id is JSONB against JSON. A comparison written
    for one dialect can pass there and silently never match on the other
    — and *never matching* here means never refusing, so the SQLite pass
    would be green while production kept the hole open.
    """
    a_execution, b_execution = _eid(), _eid()
    build_a = await _new_build(pg_client)
    await pg_client.post(f"{BUILDS}/{build_a}/tasks", json=_register("pg-supersede"))
    assert (
        await _start(
            pg_client, build_a, "pg-supersede", claim="true", execution_id=a_execution
        )
    ).status_code == 200
    assert (
        await _start(
            pg_client,
            build_a,
            "pg-supersede",
            executor="modal",
            executor_ref="fc-a",
            execution_id=a_execution,
        )
    ).status_code == 200
    await pg_session.execute(
        update(Task)
        .where(Task.task_id == "pg-supersede")
        .values(
            latest_status_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)
        )
    )
    await pg_session.commit()

    build_b = await _new_build(pg_client)
    assert (
        await _start(
            pg_client, build_b, "pg-supersede", claim="true", execution_id=b_execution
        )
    ).status_code == 200

    refused = await _start(
        pg_client,
        build_a,
        "pg-supersede",
        executor="modal",
        executor_ref="fc-a",
        execution_id=a_execution,
    )

    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["error_code"] == "execution_superseded"
