"""``transition_task()``: claims, reports, the ledger (``api-pg`` tier).

Written from design.md, "The runnable rule", "Claim × plan invariants" and
the ``execution`` entity, before the service. Each test names its scenario
id and the rule that decides it.
"""

from __future__ import annotations

import asyncio
import re
from datetime import timedelta
from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine

from stardag_api.services import builds, reactive, transitions
from stardag_api.services.errors import Conflict, NotFound
from stardag_api.services.transitions import Transition, TransitionKind
from tests.v2_support import ENV, Harness, item, observed, task_ids, utcnow


@pytest.fixture
def h(session_factory) -> Harness:
    return Harness(session_factory)


async def test_s36_retried_granted_start_after_a_seal_is_a_no_op(h: Harness):
    """S36 — a claiming start whose first delivery was granted is retried
    after a replacement plan's seal superseded its plan: the same execution
    already holds the claim, so it is granted (a no-op), not 409
    ``plan_superseded``. The ordering of the two checks decides it; a
    different execution through the superseded plan is refused."""
    deployment = await h.new_deployment()
    t, w = item("T"), item("W")
    root = item("Root", upstreams=[t, w])
    build = await h.new_build([root])
    p1 = await h.plan(build, deployment, [root])
    await h.register(p1.id, [t, w, root])
    await h.seal(p1.id)
    execution = await h.start(p1.id, t)

    p2 = await h.plan(build, deployment, [root], settings={"S": "2"})
    await h.register(p2.id, [t, w, root])
    await h.seal(p2.id)  # p1 is superseded

    retried = await h.transition(p1.id, t, Transition.start(execution))
    assert not retried.applied
    assert retried.status == "running" and retried.execution_id == execution
    with pytest.raises(Conflict) as exc:
        await h.start(p1.id, w)
    assert exc.value.code == "plan_superseded"


async def test_s39_claim_rechecks_upstreams_under_the_row_lock(h: Harness):
    """S39 — a tick reads D as runnable, then an observation invalidates
    D's upstream T: the claiming start for D re-checks its upstreams and is
    409 ``upstream_incomplete``. The frontier is a hint; the claim decides."""
    t = item("T")
    d = item("D", upstreams=[t])
    build, plan = await h.planned([item("Root", upstreams=[d])], [t, d])
    await h.run(plan.id, t)
    assert d.task_id in task_ids((await h.frontier(build)).runnable)

    await h.register(plan.id, [observed(t, False)])
    with pytest.raises(Conflict) as exc:
        await h.start(plan.id, d)
    assert exc.value.code == "upstream_incomplete"
    assert (await h.task(d))["status"] == "pending"


async def test_s19_stale_report_is_recorded_and_refused(h: Harness):
    """S19 — a stale worker's ``/complete`` for a task another build now
    runs: applied only if its ``execution_id`` is current and its claim not
    yet released; otherwise its ledger end is written, the report recorded
    with ``report_applied = false``, and refused (409). Here the stale
    execution's lapsed claim was taken over, which is what makes it late."""
    deployment = await h.new_deployment()
    t = item("T")
    _, plan_a = await h.planned([t], [t], deployment_id=deployment)
    _, plan_b = await h.planned([t], [t], deployment_id=deployment)
    stale = await h.start(plan_a.id, t)
    await h.lapse_claim(t)
    current = await h.start(plan_b.id, t)

    with pytest.raises(Conflict) as exc:
        await h.transition(plan_a.id, t, Transition.complete(stale))
    assert exc.value.code == "execution_not_current"
    task = await h.task(t)
    assert task["status"] == "running" and task["execution_id"] == current
    ledger = await h.execution(stale)
    assert ledger["outcome"] == "completed" and ledger["ended_at"] is not None
    refused = [e for e in await h.events(t) if not e["report_applied"]]
    assert [(e["type"], e["execution_id"]) for e in refused] == [
        ("task_completed", stale)
    ]

    stranger = uuid4()
    with pytest.raises(Conflict) as exc:
        await h.transition(plan_b.id, t, Transition.complete(stranger))
    assert exc.value.code == "unknown_execution"
    assert len([e for e in await h.events(t) if not e["report_applied"]]) == 2
    assert (await h.task(t))["execution_id"] == current


async def test_s21_lapsed_claim_is_taken_over_and_recorded(h: Harness):
    """S21 — a worker dies without reporting: its claim lapses, the task is
    ACTIONABLE, the next claiming start takes it over and writes
    ``claim_outcome = taken_over`` on the old execution, whose ``ended_at``
    stays NULL (no report of it ending has arrived)."""
    t = item("T")
    build, plan = await h.planned([t], [t])
    dead = await h.start(plan.id, t)
    await h.lapse_claim(t)
    assert t.task_id in task_ids((await h.frontier(build)).runnable)

    successor = await h.start(plan.id, t)
    old = await h.execution(dead)
    assert old["claim_outcome"] == "taken_over"
    assert old["claim_released_at"] is not None and old["ended_at"] is None
    task = await h.task(t)
    assert task["execution_id"] == successor and task["claim_plan_id"] == plan.id
    assert task["claim_expires_at"] > utcnow()


async def test_s35_duplicate_fail_after_a_retry_is_not_applied(h: Harness):
    """S35 — a duplicate delayed ``/fail`` after an operator ``retry``: the
    execution already has ``ended_at``, so the duplicate is recorded with
    ``report_applied = false`` and the retried task stays PENDING (one
    terminal report per execution)."""
    t = item("T")
    _, plan = await h.planned([t], [t])
    execution = await h.start(plan.id, t)
    await h.transition(plan.id, t, Transition.fail(execution, "boom"))
    assert (await h.task(t))["status"] == "failed"
    ledger = await h.execution(execution)
    assert (ledger["outcome"], ledger["claim_outcome"]) == ("failed", "failed")

    await h.transition(plan.id, t, Transition.retry())
    assert (await h.task(t))["status"] == "pending"
    with pytest.raises(Conflict) as exc:
        await h.transition(plan.id, t, Transition.fail(execution, "boom"))
    assert exc.value.code == "execution_already_ended"
    assert (await h.task(t))["status"] == "pending"
    types = [(e["type"], e["report_applied"]) for e in await h.events(t)]
    assert types[-2:] == [("task_retried", True), ("task_failed", False)]


async def test_s30_observation_racing_a_claiming_start(h: Harness):
    """S30 — an ``observed_complete: false`` chunk racing a claiming start:
    both lock the task row, and the loser sees the winner's state. A start
    against COMPLETED is 409 ``task_already_completed``; a start queued
    behind an invalidation's lock claims the now-PENDING task; an
    observation against a live claim changes nothing."""
    t = item("T")
    _, plan = await h.planned([t], [t])
    await h.run(plan.id, t)

    with pytest.raises(Conflict) as exc:
        await h.start(plan.id, t)
    assert exc.value.code == "task_already_completed"

    task_pk = (await h.task(t))["id"]
    async with h.sf() as observing:
        await transitions.transition_task(
            observing,
            ENV,
            task_pk=task_pk,
            plan_id=plan.id,
            transition=Transition(TransitionKind.INVALIDATE, observed_at=utcnow()),
            now=utcnow(),
        )
        claiming = asyncio.create_task(h.start(plan.id, t))
        await asyncio.sleep(0.3)
        assert not claiming.done(), "the start must wait on the row lock"
        await observing.commit()
    execution = await claiming
    task = await h.task(t)
    assert task["status"] == "running" and task["execution_id"] == execution

    await h.register(plan.id, [observed(t, False)])
    assert (await h.task(t))["status"] == "running"


async def test_renewal_only_for_the_live_holder(h: Harness):
    """``claim/renew`` is granted only to the execution holding the live
    claim, under the same row lock a claiming start takes."""
    t = item("T")
    _, plan = await h.planned([t], [t])
    holder = await h.start(plan.id, t, claim_ttl_seconds=60)
    before = (await h.task(t))["claim_expires_at"]
    renewed = await h.renew(t, holder)
    assert renewed.claim_expires_at is not None and renewed.claim_expires_at > before

    with pytest.raises(Conflict) as exc:
        await h.renew(t, uuid4())
    assert exc.value.code == "claim_not_held"
    await h.lapse_claim(t)
    with pytest.raises(Conflict) as exc:
        await h.renew(t, holder)
    assert exc.value.code == "claim_not_held"
    assert exc.value.detail["claim_outcome"] is None  # lapsed, not yet moved


async def test_a_renewal_refused_after_a_build_release_says_so(h: Harness):
    """A resident driver renewing after an operator ``cancel`` learns its
    claim was ``released`` (its build stopped), not taken over."""
    t = item("T")
    build, plan = await h.planned([t], [t])
    holder = await h.start(plan.id, t, claim_ttl_seconds=60)
    async with h.sf() as s:
        await builds.cancel_build(s, ENV, build)
    with pytest.raises(Conflict) as exc:
        await h.renew(t, holder)
    assert exc.value.code == "claim_not_held"
    assert exc.value.detail["claim_outcome"] == "released"


async def test_one_terminal_report_per_execution(h: Harness):
    t = item("T")
    _, plan = await h.planned([t], [t])
    execution = await h.start(plan.id, t)
    await h.transition(plan.id, t, Transition.complete(execution))
    task = await h.task(t)
    assert task["status"] == "completed" and task["claim_expires_at"] is None
    with pytest.raises(Conflict) as exc:
        await h.transition(plan.id, t, Transition.complete(execution))
    assert exc.value.code == "execution_already_ended"


async def test_claim_refusals(h: Harness):
    """A live claim held by another execution is 409
    ``task_already_running``; a task outside the plan is 404."""
    t = item("T")
    _, plan = await h.planned([t], [t])
    await h.start(plan.id, t)
    with pytest.raises(Conflict) as exc:
        await h.start(plan.id, t)
    assert exc.value.code == "task_already_running"
    with pytest.raises(NotFound) as missing:
        await h.start(plan.id, item("Elsewhere"))
    assert missing.value.code == "not_a_member"


async def test_a_report_comes_through_the_plan_holding_the_claim(h: Harness):
    """A task shared by two builds, claimed through plan A: its execution's
    report, and its self-report start, under plan B are 409
    ``not_claim_holder`` — lapsed or not — and leave no trace (the task
    RUNNING, the execution not ended, no event), so the same report through
    plan A still applies."""
    deployment = await h.new_deployment()
    t = item("T")
    _, plan_a = await h.planned([t], [t], deployment_id=deployment)
    _, plan_b = await h.planned([t], [t], deployment_id=deployment)
    execution = await h.start(plan_a.id, t)
    events_before = len(await h.events(t))

    for transition in (
        Transition.start(execution, claim=False, executor="x"),
        Transition.complete(execution),
    ):
        with pytest.raises(Conflict) as exc:
            await h.transition(plan_b.id, t, transition)
        assert exc.value.code == "not_claim_holder"
        assert exc.value.detail["claim_plan_id"] == str(plan_a.id)
    await h.lapse_claim(t)
    for transition in (
        # The self-report start decides the plan before the live-claim
        # refusal: a lapsed, unreleased claim still names its execution.
        Transition.start(execution, claim=False, executor="x"),
        Transition.fail(execution, "boom"),
    ):
        with pytest.raises(Conflict) as exc:
            await h.transition(plan_b.id, t, transition)
        assert exc.value.code == "not_claim_holder"

    assert (await h.task(t))["status"] == "running"
    assert (await h.execution(execution))["ended_at"] is None
    assert len(await h.events(t)) == events_before

    outcome = await h.transition(plan_a.id, t, Transition.complete(execution))
    assert outcome.applied and outcome.status.value == "completed"


async def test_a_claiming_start_refuses_a_status_outside_actionable(h: Harness):
    """A claiming start is decided by ACTIONABLE, not only by COMPLETED and
    a live claim: a FAILED task is 409 ``task_not_actionable`` (the fail
    mode decides, through ``retry``), writes nothing, and is startable
    again once a retry makes it PENDING."""
    t = item("T")
    _, plan = await h.planned([t], [t])
    first = await h.start(plan.id, t)
    await h.transition(plan.id, t, Transition.fail(first, "boom"))
    events_before = len(await h.events(t))

    with pytest.raises(Conflict) as exc:
        await h.start(plan.id, t)
    assert exc.value.code == "task_not_actionable"
    assert exc.value.detail["status"] == "failed"
    assert (await h.task(t))["status"] == "failed"
    assert len(await h.events(t)) == events_before

    await h.transition(plan.id, t, Transition.retry())
    await h.start(plan.id, t)
    assert (await h.task(t))["status"] == "running"

    done = item("Done")
    _, plan = await h.planned([done], [done])
    await h.run(plan.id, done)
    with pytest.raises(Conflict) as exc:
        await h.start(plan.id, done)
    assert exc.value.code == "task_already_completed"


async def test_non_claiming_start_is_the_holders_self_report(h: Harness):
    """A non-claiming start names the claim's execution and records the
    executor details the claim could not know (the spawn came after); one
    naming another execution is recorded and refused."""
    t = item("T")
    _, plan = await h.planned([t], [t])
    execution = await h.start(plan.id, t)
    await h.transition(
        plan.id,
        t,
        Transition.start(execution, claim=False, executor="modal", executor_ref="fc-1"),
    )
    ledger = await h.execution(execution)
    assert (ledger["executor"], ledger["executor_ref"]) == ("modal", "fc-1")
    with pytest.raises(Conflict) as exc:
        await h.transition(plan.id, t, Transition.start(uuid4(), claim=False))
    assert exc.value.code == "unknown_execution"


async def test_suspend_releases_the_claim_and_is_actionable(h: Harness):
    """SUSPENDED holds no claim and is ACTIONABLE (run from scratch once its
    dynamic children are COMPLETED)."""
    t = item("T")
    build, plan = await h.planned([t], [t])
    execution = await h.start(plan.id, t)
    await h.transition(plan.id, t, Transition.suspend(execution))
    task = await h.task(t)
    assert task["status"] == "suspended" and task["claim_plan_id"] is None
    ledger = await h.execution(execution)
    assert (ledger["claim_outcome"], ledger["outcome"]) == ("suspended", "suspended")
    assert t.task_id in task_ids((await h.frontier(build)).runnable)


async def test_retry_is_idempotent_by_state(h: Harness):
    """A retry of a task already PENDING changes nothing and writes no
    event; a retry of a live claim is refused."""
    t = item("T")
    _, plan = await h.planned([t], [t])
    outcome = await h.transition(plan.id, t, Transition.retry())
    assert not outcome.applied
    assert not await h.events(t, types=["task_retried"])
    await h.start(plan.id, t)
    with pytest.raises(Conflict) as exc:
        await h.transition(plan.id, t, Transition.retry())
    assert exc.value.code == "task_already_running"


async def test_observed_completion_closes_a_lapsed_claim_and_spares_a_live_one(
    h: Harness,
):
    """Whatever moves a task off RUNNING closes the current claim: an
    observed completion of a task whose claim lapsed writes ``claim_outcome
    = lapsed`` (``ended_at`` stays NULL — no report arrived); against a live
    claim the observation changes nothing (the holder will report)."""
    t = item("T")
    _, plan = await h.planned([t], [t])
    held = await h.start(plan.id, t)
    await h.register(plan.id, [observed(t, True)])
    assert (await h.task(t))["status"] == "running"

    await h.lapse_claim(t)
    await h.register(plan.id, [observed(t, True)])
    task = await h.task(t)
    assert task["status"] == "completed" and task["claim_plan_id"] is None
    ledger = await h.execution(held)
    assert ledger["claim_outcome"] == "lapsed" and ledger["ended_at"] is None


async def test_report_after_the_claim_lapsed_but_before_takeover_is_applied(
    h: Harness,
):
    """The authority rule keys on the execution, not on the clock: a worker
    whose claim lapsed seconds before it reports completion still names the
    task's current execution (nothing has taken the claim over), so its
    completion is applied — the target exists, discarding it would cost a
    re-run for nothing. The ledger closes as ``completed``, not ``lapsed``."""
    t = item("T")
    _, plan = await h.planned([t], [t])
    execution = await h.start(plan.id, t)
    await h.lapse_claim(t)
    outcome = await h.transition(plan.id, t, Transition.complete(execution))
    assert outcome.applied and outcome.status == "completed"
    ledger = await h.execution(execution)
    assert (ledger["claim_outcome"], ledger["outcome"]) == ("completed", "completed")
    assert all(e["report_applied"] for e in await h.events(t))


async def test_limit_keys_are_written_at_claim_and_replaced_on_every_claim(
    h: Harness,
):
    """Limit keys travel with the claiming start (the tick computes them
    from the instance body) and replace the task's keys on every claim."""
    t = item("T")
    _, plan = await h.planned([t], [t])
    await h.start(plan.id, t, limit_keys=["gpu", "db", "gpu"])
    rows = await h._rows(
        "SELECT key FROM task_limit_key WHERE task_pk = :t ORDER BY key",
        t=(await h.task(t))["id"],
    )
    assert [r["key"] for r in rows] == ["db", "gpu"]

    await h.lapse_claim(t)
    await h.start(plan.id, t, limit_keys=["cpu"])
    rows = await h._rows(
        "SELECT key FROM task_limit_key WHERE task_pk = :t",
        t=(await h.task(t))["id"],
    )
    assert [r["key"] for r in rows] == ["cpu"]


async def test_a_transition_flags_the_other_builds_holding_the_task(h: Harness):
    """A status write flags the reactive builds whose active plans hold the
    task (``plan_member``, ``SKIP LOCKED``); the writing build is not
    flagged by its own transition. (The relation in detail:
    ``test_v2_wakeups.py``.)"""
    deployment = await h.new_deployment()
    t = item("T")
    build_a, plan_a = await h.planned([t], [t], deployment_id=deployment)
    build_b, _ = await h.planned([t], [t], deployment_id=deployment)
    for build in (build_a, build_b):
        async with h.sf() as s:
            await reactive.set_reactive_meta(
                s, ENV, build, app_name="app", tick_kwargs=None
            )
    await h.run(plan_a.id, t)
    assert (await h.build(build_b))["needs_tick_at"] is not None
    assert (await h.build(build_a))["needs_tick_at"] is None


async def test_s21_race_two_starts_on_a_lapsed_claim_grant_exactly_one(
    h: Harness, async_engine: AsyncEngine
):
    """S21-race — two claiming starts race on one lapsed claim: the task row
    lock is ``FOR NO KEY UPDATE`` (not ``FOR KEY SHARE``, which two starts
    could hold at once), so the second waits for the first to commit, sees
    its live claim, and is 409 ``task_already_running``."""
    t = item("T")
    _, plan = await h.planned([t], [t])
    dead = await h.start(plan.id, t)
    await h.lapse_claim(t)

    statements: list[str] = []

    def capture(conn, cursor, statement, *args):  # noqa: ARG001
        statements.append(statement)

    event.listen(async_engine.sync_engine, "before_cursor_execute", capture)
    try:
        results = await asyncio.gather(
            *(h.start(plan.id, t) for _ in range(4)), return_exceptions=True
        )
    finally:
        event.remove(async_engine.sync_engine, "before_cursor_execute", capture)

    granted = [r for r in results if not isinstance(r, BaseException)]
    refused = [r for r in results if isinstance(r, BaseException)]
    assert len(granted) == 1
    assert {r.code for r in refused if isinstance(r, Conflict)} == {
        "task_already_running"
    }
    assert len(refused) == 3
    task = await h.task(t)
    assert task["execution_id"] == granted[0]
    assert (await h.execution(dead))["claim_outcome"] == "taken_over"

    task_locks = [
        s for s in statements if re.search(r"\bFROM task\b(?!_)", s) and " FOR " in s
    ]
    assert task_locks and all("FOR NO KEY UPDATE" in s for s in task_locks)
    assert not [s for s in task_locks if "FOR KEY SHARE" in s]


# -- the remaining transitions (I0 step 3b) -----------------------------------


async def test_interrupt_releases_the_claim_and_is_actionable(h: Harness):
    """An interruption (the platform took the execution away) moves the
    task to INTERRUPTED — not a failure, not terminal — releasing the claim
    (``claim_outcome = interrupted``) and ending the execution; the task is
    ACTIONABLE and the message is kept."""
    t = item("T")
    build, plan = await h.planned([t], [t])
    execution = await h.start(plan.id, t)
    await h.transition(plan.id, t, Transition.interrupt(execution, "timeout"))
    task = await h.task(t)
    assert task["status"] == "interrupted" and task["claim_plan_id"] is None
    assert task["error_message"] == "timeout"
    ledger = await h.execution(execution)
    assert (ledger["claim_outcome"], ledger["outcome"]) == (
        "interrupted",
        "interrupted",
    )
    assert t.task_id in task_ids((await h.frontier(build)).runnable)


async def test_s21_a_late_interrupt_after_takeover_is_recorded_not_applied(
    h: Harness,
):
    """S21 — the dead worker's lapsed claim was taken over (``taken_over``);
    if it reports after all, the report writes its own ledger end and is
    recorded ``report_applied = false``; the successor's claim is untouched."""
    t = item("T")
    _, plan = await h.planned([t], [t])
    dead = await h.start(plan.id, t)
    await h.lapse_claim(t)
    successor = await h.start(plan.id, t)
    with pytest.raises(Conflict) as exc:
        await h.transition(plan.id, t, Transition.interrupt(dead, "oom"))
    assert exc.value.code == "execution_not_current"
    old = await h.execution(dead)
    assert (old["claim_outcome"], old["outcome"]) == ("taken_over", "interrupted")
    task = await h.task(t)
    assert task["status"] == "running" and task["execution_id"] == successor
    (late,) = await h.events(t, types=["task_interrupted"])
    assert not late["report_applied"]


async def test_s35_duplicate_interrupt_after_a_retry_is_not_applied(h: Harness):
    """S35 — one terminal report per execution, for an interruption too: a
    delayed duplicate after an operator retry is recorded, not applied."""
    t = item("T")
    _, plan = await h.planned([t], [t])
    execution = await h.start(plan.id, t)
    await h.transition(plan.id, t, Transition.interrupt(execution))
    await h.transition(plan.id, t, Transition.retry())
    with pytest.raises(Conflict) as exc:
        await h.transition(plan.id, t, Transition.interrupt(execution))
    assert exc.value.code == "execution_already_ended"
    assert (await h.task(t))["status"] == "pending"


async def test_preempt_is_status_neutral_and_pulls_the_expiry_to_a_grace(
    h: Harness,
):
    """A preemption keeps the task RUNNING under the same claim, sets
    ``preempted_at`` and pulls the expiry forward to the restart grace —
    never back (a claim shorter than the grace keeps its expiry). The
    restart's own non-claiming start re-grants the TTL. Not an end: the
    ledger's ``ended_at`` stays NULL and the restart completes normally."""
    t = item("T")
    _, plan = await h.planned([t], [t])
    execution = await h.start(plan.id, t, claim_ttl_seconds=86400)
    before = (await h.task(t))["claim_expires_at"]
    outcome = await h.transition(plan.id, t, Transition.preempt(execution))
    assert outcome.status == "running" and outcome.execution_id == execution
    task = await h.task(t)
    assert task["preempted_at"] is not None
    assert task["claim_expires_at"] < before
    assert task["claim_expires_at"] <= utcnow() + timedelta(seconds=901)
    assert (await h.execution(execution))["ended_at"] is None

    await h.transition(
        plan.id,
        t,
        Transition.start(execution, claim=False, claim_ttl_seconds=7200),
    )
    task = await h.task(t)
    assert task["preempted_at"] is None
    assert task["claim_expires_at"] > utcnow() + timedelta(seconds=7000)
    await h.transition(plan.id, t, Transition.complete(execution))
    assert (await h.task(t))["status"] == "completed"

    short = item("Short")
    _, plan2 = await h.planned([short], [short])
    brief = await h.start(plan2.id, short, claim_ttl_seconds=60)
    expiry = (await h.task(short))["claim_expires_at"]
    await h.transition(plan2.id, short, Transition.preempt(brief))
    assert (await h.task(short))["claim_expires_at"] == expiry


async def test_a_preemption_from_a_stale_execution_is_recorded_not_applied(
    h: Harness,
):
    t = item("T")
    _, plan = await h.planned([t], [t])
    stale = await h.start(plan.id, t)
    await h.lapse_claim(t)
    await h.start(plan.id, t)
    with pytest.raises(Conflict) as exc:
        await h.transition(plan.id, t, Transition.preempt(stale))
    assert exc.value.code == "execution_not_current"
    assert (await h.task(t))["preempted_at"] is None
    (late,) = await h.events(t, types=["task_preempted"])
    assert not late["report_applied"]


async def test_skip_is_a_scheduling_decision_idempotent_by_state(h: Harness):
    """``skip`` names no execution: a PENDING member goes SKIPPED
    (ACTIONABLE once gated open); a re-sent skip is a no-op; refused
    against a live claim, a COMPLETED task and a FAILED one."""
    t = item("T")
    _, plan = await h.planned([t], [t])
    skipped = await h.transition(plan.id, t, Transition.skip())
    assert skipped.applied and skipped.status == "skipped"
    again = await h.transition(plan.id, t, Transition.skip())
    assert not again.applied
    assert len(await h.events(t, types=["task_skipped"])) == 1

    running = await h.start(plan.id, t)
    with pytest.raises(Conflict) as exc:
        await h.transition(plan.id, t, Transition.skip())
    assert exc.value.code == "task_already_running"
    await h.transition(plan.id, t, Transition.fail(running, "x"))
    with pytest.raises(Conflict) as exc:
        await h.transition(plan.id, t, Transition.skip())
    assert exc.value.code == "task_not_skippable"


async def test_cancel_is_for_the_build_holding_the_claim(h: Harness):
    """A single task's cancel: only the build holding the claim (409
    ``not_claim_holder`` for any other, and for a task nobody holds);
    CANCELLED with ``claim_outcome = cancelled``, the execution's
    ``ended_at`` untouched (cooperative); a re-sent cancel is a no-op."""
    deployment = await h.new_deployment()
    t = item("T")
    _, plan_a = await h.planned([t], [t], deployment_id=deployment)
    _, plan_b = await h.planned([t], [t], deployment_id=deployment)
    with pytest.raises(Conflict) as exc:
        await h.transition(plan_a.id, t, Transition.cancel())
    assert exc.value.code == "not_claim_holder"

    execution = await h.start(plan_a.id, t)
    with pytest.raises(Conflict) as exc:
        await h.transition(plan_b.id, t, Transition.cancel())
    assert exc.value.code == "not_claim_holder"
    assert (await h.task(t))["status"] == "running"

    cancelled = await h.transition(plan_a.id, t, Transition.cancel())
    assert cancelled.applied and cancelled.status == "cancelled"
    ledger = await h.execution(execution)
    assert ledger["claim_outcome"] == "cancelled" and ledger["ended_at"] is None
    assert not (await h.transition(plan_a.id, t, Transition.cancel())).applied
    with pytest.raises(Conflict) as late:
        await h.transition(plan_a.id, t, Transition.complete(execution))
    assert late.value.code == "execution_not_current"


async def test_a_non_claiming_start_follows_the_authority_rule(h: Harness):
    """The holder's self-report is applied when it names the task's current
    execution, lapsed or not; late (recorded, refused) only once a claiming
    start took the claim over."""
    t = item("T")
    _, plan = await h.planned([t], [t])
    execution = await h.start(plan.id, t)
    await h.lapse_claim(t)
    applied = await h.transition(
        plan.id, t, Transition.start(execution, claim=False, executor_ref="fc-1")
    )
    assert applied.applied
    assert (await h.execution(execution))["executor_ref"] == "fc-1"

    await h.start(plan.id, t)
    with pytest.raises(Conflict) as exc:
        await h.transition(plan.id, t, Transition.start(execution, claim=False))
    assert exc.value.code == "execution_not_current"


async def test_status_timestamps_are_stamped_after_the_row_lock(h: Harness):
    """``completed_at`` (and every status timestamp) is the time the
    transition took effect under the lock, not the caller's earlier clock —
    so the ``observed_at`` guard compares against the real completion."""
    t = item("T")
    _, plan = await h.planned([t], [t])
    execution = await h.start(plan.id, t)
    task_pk = (await h.task(t))["id"]
    early = utcnow() - timedelta(minutes=5)
    async with h.sf() as s:
        await transitions.transition_task(
            s,
            ENV,
            task_pk=task_pk,
            plan_id=plan.id,
            transition=Transition.complete(execution),
            now=early,
        )
        await s.commit()
    task = await h.task(t)
    assert task["completed_at"] > early + timedelta(minutes=4)
    assert task["status_at"] == task["completed_at"]
    # An observation made after the caller's clock but before the real
    # completion does not invalidate it.
    await h.register(plan.id, [observed(t, False, at=early + timedelta(minutes=1))])
    assert (await h.task(t))["status"] == "completed"


async def test_remaining_transitions_over_http(client: AsyncClient, h: Harness):
    t = item("T")
    _, plan = await h.planned([t], [t])
    base = f"/api/v2/plans/{plan.id}/members/{t.task_id}"
    execution = str(await h.start(plan.id, t))
    preempted = await client.post(f"{base}/preempt", json={"execution_id": execution})
    assert preempted.status_code == 200 and preempted.json()["status"] == "running"
    interrupted = await client.post(
        f"{base}/interrupt", json={"execution_id": execution, "error_message": "x"}
    )
    assert interrupted.json()["status"] == "interrupted"
    skipped = await client.post(f"{base}/skip")
    assert skipped.json()["status"] == "skipped"
    refused = await client.post(f"{base}/cancel")
    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "not_claim_holder"
