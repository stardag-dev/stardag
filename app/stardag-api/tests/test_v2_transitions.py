"""``transition_task()``: claims, reports, the ledger (``api-pg`` tier).

Written from design.md, "The runnable rule", "Claim × plan invariants" and
the ``execution`` entity, before the service. Each test names its scenario
id and the rule that decides it.
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from stardag_api.services import reactive, transitions
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
