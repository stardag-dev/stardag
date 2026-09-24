"""Skip-blocked and the exclusion cascade (``api-pg`` tier).

Written from design.md, "The runnable rule" (skip-blocked, exclusion, a
failed discovery job) and the ``plan_member`` entity; S18 and S34 in the
scenario table.
"""

from __future__ import annotations

import asyncio

import pytest
from httpx import AsyncClient

from stardag_api.models import ExclusionReason
from stardag_api.services import builds, exclusion
from stardag_api.services.errors import Conflict
from stardag_api.services.transitions import (
    Transition,
    TransitionKind,
    member_task_pk,
    transition_task,
)
from tests.v2_support import (
    ENV,
    Harness,
    item,
    observed,
    task_ids,
    unexpanded,
    utcnow,
)


@pytest.fixture
def h(session_factory) -> Harness:
    return Harness(session_factory)


async def _fail(h: Harness, plan_id, it) -> None:
    execution = await h.start(plan_id, it)
    await h.transition(plan_id, it, Transition.fail(execution, "boom"))


async def test_skip_blocked_walks_instance_edges_within_the_plan(h: Harness):
    """The blocked closure of a FAILED member — downstream over instance
    edges, through PENDING / SUSPENDED / INTERRUPTED / FAILED / CANCELLED /
    SKIPPED members — is SKIPPED; an independent sibling is not, and a
    COMPLETED member stops the walk. A re-delivery skips nothing."""
    leaf, ok = item("Leaf"), item("Ok")
    mid = item("Mid", upstreams=[leaf])
    done = item("Done", upstreams=[leaf])
    after_done = item("AfterDone", upstreams=[done])
    root = item("Root", upstreams=[mid, ok])
    build, plan = await h.planned(
        [root], [leaf, ok, mid, observed(done, True), after_done, root], seal=True
    )
    await _fail(h, plan.id, leaf)

    result = await h.skip_blocked(build)
    assert result.plan_id == plan.id
    assert set(result.skipped) == {mid.task_id, root.task_id}
    for it in (mid, root):
        assert (await h.task(it))["status"] == "skipped"
    assert (await h.task(ok))["status"] == "pending"
    assert (await h.task(after_done))["status"] == "pending"
    assert (await h.skip_blocked(build)).skipped == []
    assert task_ids((await h.frontier(build)).runnable) == {
        ok.task_id,
        after_done.task_id,
    }


async def test_skip_blocked_is_a_no_op_on_a_cancelled_build(h: Harness):
    """A cancel releases the build's claims, leaving those tasks CANCELLED;
    a late skip-blocked from a driver that raced the cancel must not read
    them as failures and skip their downstream: a build that did not fail
    has no failure to propagate."""
    leaf = item("Leaf")
    root = item("Root", upstreams=[leaf])
    build, plan = await h.planned([root], [leaf, root], seal=True)
    await h.start(plan.id, leaf)
    async with h.sf() as s:
        await builds.cancel_build(s, ENV, build)
    assert (await h.task(leaf))["status"] == "cancelled"
    async with h.sf() as s:
        result = await exclusion.skip_blocked(s, ENV, build)
    assert result.skipped == []
    assert (await h.task(root))["status"] == "pending"


async def test_s18_an_operator_exclusion_cascades_and_leaves_the_global_status(
    h: Harness,
):
    """S18 — the operator gives up on a member: ``excluded_at`` set, not
    scheduled, not gating completion, and cascaded (``upstream_excluded``)
    to its downstream closure within the plan — but not past a COMPLETED
    member, which needs nothing. The global status is untouched, and the
    same completion in another plan is unaffected."""
    deployment = await h.new_deployment()
    bad, ok = item("Bad"), item("Ok")
    mid = item("Mid", upstreams=[bad])
    side = item("Side", upstreams=[bad])
    root = item("Root", upstreams=[mid, ok, side])
    build, plan = await h.planned(
        [root],
        [bad, ok, mid, observed(side, True), root],
        deployment_id=deployment,
        seal=True,
    )
    _, other = await h.planned([bad], [bad], deployment_id=deployment)

    result = await h.exclude(plan.id, bad)
    assert result.excluded == [bad.task_id, *sorted([mid.task_id, root.task_id])]
    assert result.roots_excluded == [root.task_id]  # the cascade reached it
    assert result.build_failed
    members = await h.members(plan.id)
    assert members[bad.task_id]["excluded_reason"] == "operator"
    assert members[mid.task_id]["excluded_reason"] == "upstream_excluded"
    assert members[side.task_id]["excluded_at"] is None
    assert members[ok.task_id]["excluded_at"] is None
    assert (await h.task(bad))["status"] == "pending"
    assert (await h.members(other.id))[bad.task_id]["excluded_at"] is None
    events = await h.events(types=["task_excluded"], plan_id=plan.id)
    assert len(events) == 3

    again = await h.exclude(plan.id, bad)
    # A re-delivery excludes nothing, so it reached no root and failed
    # nothing: the result describes this call, not the plan.
    assert again.excluded == [] and again.roots_excluded == []
    assert not again.build_failed
    assert len(await h.events(types=["task_excluded"], plan_id=plan.id)) == 3


async def test_s18_an_excluded_non_root_does_not_gate_completion(h: Harness):
    """S18 (member half) — an excluded member off the root's path is not
    scheduled and the build completes without it."""
    bad = item("Bad")
    dangling = item("Dangling", upstreams=[bad])
    root = item("Root")
    build, plan = await h.planned([root], [root, bad, dangling], seal=True)
    result = await h.exclude(plan.id, bad)
    assert set(result.excluded) == {bad.task_id, dangling.task_id}
    assert not result.build_failed
    await h.run(plan.id, root)
    frontier = await h.frontier(build)
    assert frontier.plan_complete and frontier.runnable == []
    async with h.sf() as s:
        completed = await builds.complete_build(s, ENV, build)
    assert completed.status == "completed"


async def test_s18_an_excluded_root_fails_the_build(h: Harness):
    """S18 (root half) — an excluded root is a request that cannot be met:
    the build is FAILED (``root_excluded``), releasing its claims."""
    root = item("Root")
    build, plan = await h.planned([root], [root], seal=True)
    result = await h.exclude(plan.id, root)
    assert result.build_failed and result.roots_excluded == [root.task_id]
    assert (await h.build(build))["status"] == "failed"
    (failed,) = await h.events(build_id=build, types=["build_failed"])
    assert failed["event_metadata"]["reason"] == "root_excluded"


async def test_a_later_exclusion_reports_only_what_it_did(h: Harness):
    """Once a root is excluded (the build FAILED), excluding a completed
    leaf off every root's path cascades nowhere: the result names no root
    and says it failed nothing, and no second ``build_failed`` is written."""
    leaf, bad = item("Leaf"), item("Bad")
    root = item("Root", upstreams=[bad])
    build, plan = await h.planned([root], [observed(leaf, True), bad, root], seal=True)
    first = await h.exclude(plan.id, bad)
    assert first.roots_excluded == [root.task_id] and first.build_failed

    later = await h.exclude(plan.id, leaf)
    assert later.excluded == [leaf.task_id]
    assert later.roots_excluded == [] and not later.build_failed
    assert len(await h.events(build_id=build, types=["build_failed"])) == 1


async def test_a_root_exclusion_leaves_a_terminal_build_as_it_is(h: Harness):
    """A terminal build status is sticky: an exclusion that reaches a root
    of a CANCELLED build records the exclusion and says which root it
    reached, but does not move the build to FAILED."""
    root = item("Root")
    build, plan = await h.planned([root], [root], seal=True)
    async with h.sf() as s:
        await builds.cancel_build(s, ENV, build)
    result = await h.exclude(plan.id, root)
    assert result.roots_excluded == [root.task_id] and not result.build_failed
    assert (await h.build(build))["status"] == "cancelled"
    assert await h.events(build_id=build, types=["build_failed"]) == []


async def test_s34_a_failed_discovery_job_excludes_the_member(h: Harness):
    """S34 — a discovery job for a class the tick cannot import: the member
    is excluded with ``discovery_failed`` and the error, the exclusion
    cascades to the root and fails the build, and the member is never a
    discovery job again."""
    missing = item("Missing")
    root = item("Root", upstreams=[missing])
    build, plan = await h.planned([root], [unexpanded(missing), root], seal=True)
    assert task_ids((await h.frontier(build)).discovery_jobs) == {missing.task_id}

    error = "UnknownTaskClassError: no class Missing in this deployment"
    result = await h.exclude(
        plan.id,
        missing,
        reason=ExclusionReason.DISCOVERY_FAILED,
        error_message=error,
    )
    assert result.excluded == [missing.task_id, root.task_id]
    assert result.build_failed
    members = await h.members(plan.id)
    assert members[missing.task_id]["excluded_reason"] == "discovery_failed"
    assert members[root.task_id]["excluded_reason"] == "upstream_excluded"
    (event, _) = await h.events(types=["task_excluded"], plan_id=plan.id)
    assert event["error_message"] == error
    assert (await h.build(build))["status"] == "failed"
    assert (await h.frontier(build)).discovery_jobs == []
    assert (await h.task(missing))["status"] == "pending"


async def test_an_excluded_member_cannot_be_claimed(h: Harness):
    bad = item("Bad")
    root = item("Root")
    _, plan = await h.planned([root], [root, bad], seal=True)
    await h.exclude(plan.id, bad)
    with pytest.raises(Conflict) as exc:
        await h.start(plan.id, bad)
    assert exc.value.code == "member_excluded"


async def test_the_cascade_waits_for_a_status_move_on_a_member_it_reaches(
    h: Harness,
):
    """Two sessions: an observation holds a downstream member's task row
    with its completion not yet committed. The exclusion cascade locks the
    rows it may reach (``FOR NO KEY UPDATE``, ``task_id`` order) before it
    reads a status, so it waits, then sees the member COMPLETED and leaves
    it — rather than excluding it on the status read before the commit."""
    up = item("Up")
    down = item("Down", upstreams=[up])
    _, plan = await h.planned([down], [up, down])
    async with h.sf() as held:
        task_pk = await member_task_pk(held, ENV, plan.id, down.task_id)
        await transition_task(
            held,
            ENV,
            task_pk=task_pk,
            plan_id=None,
            transition=Transition(
                TransitionKind.OBSERVE_COMPLETE, observed_at=utcnow()
            ),
            now=utcnow(),
        )
        excluding = asyncio.create_task(h.exclude(plan.id, up))
        assert await h.blocked_or_done(excluding)
        await held.commit()
    result = await excluding
    assert result.excluded == [up.task_id]
    members = await h.members(plan.id)
    assert members[down.task_id]["excluded_at"] is None
    assert (await h.task(down))["status"] == "completed"


async def test_exclusion_on_a_superseded_plan_is_refused(h: Harness):
    deployment = await h.new_deployment()
    root = item("Root")
    build = await h.new_build([root])
    p1 = await h.plan(build, deployment, [root])
    await h.register(p1.id, [root])
    await h.seal(p1.id)
    p2 = await h.plan(build, deployment, [root], settings={"S": "2"})
    await h.register(p2.id, [root])
    await h.seal(p2.id)
    with pytest.raises(Conflict) as exc:
        await h.exclude(p1.id, root)
    assert exc.value.code == "plan_superseded"


async def test_skip_blocked_and_exclusion_over_http(client: AsyncClient, h: Harness):
    leaf = item("Leaf")
    root = item("Root", upstreams=[leaf])
    other, third = item("Other"), item("Third")
    build, plan = await h.planned(
        [root], [leaf, root, other, unexpanded(third)], seal=True
    )
    await _fail(h, plan.id, leaf)
    skipped = await client.post(f"/api/v2/builds/{build}/skip-blocked")
    assert skipped.status_code == 200 and skipped.json()["skipped"] == [root.task_id]

    base = f"/api/v2/plans/{plan.id}/members/{other.task_id}"
    excluded = await client.post(f"{base}/exclude", json={"reason": "flaky input"})
    assert excluded.status_code == 200 and excluded.json()["excluded"] == [
        other.task_id
    ]
    (event,) = await h.events(other, types=["task_excluded"])
    assert event["event_metadata"]["note"] == "flaky input"

    failed = await client.post(
        f"/api/v2/plans/{plan.id}/members/{third.task_id}/discovery-failed",
        json={"error": "ImportError"},
    )
    assert failed.status_code == 200 and not failed.json()["build_failed"]
    assert failed.json()["roots_excluded"] == []
