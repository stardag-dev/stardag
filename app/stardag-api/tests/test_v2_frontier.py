"""The frontier of a build's active plan (``api-pg`` tier).

Written from design.md, "The runnable rule" and "Registration" (closure is
kept as a mechanism), before the service. Each test names the scenario it
pins where the scenario table has one.
"""

from __future__ import annotations

import pytest

from stardag_api.services import frontier as frontier_service
from tests.v2_support import Harness, item, observed, task_ids, unexpanded


@pytest.fixture
def h(session_factory) -> Harness:
    return Harness(session_factory)


async def test_runnable_discovery_and_running_follow_the_predicates(h: Harness):
    """``discovery_job`` = not COMPLETED, not excluded, unexpanded;
    ``runnable`` = ACTIONABLE, not excluded, expanded, every upstream task
    COMPLETED; ``running`` = RUNNING with a live claim. ACTIONABLE includes
    RUNNING with a lapsed claim (S21), which is therefore runnable and not
    running."""
    leaf = item("Leaf")
    mid = item("Mid", upstreams=[leaf])
    root = item("Root", upstreams=[mid])
    build, plan = await h.planned([root])

    frontier = await h.frontier(build)
    assert frontier.plan_id == plan.id and not frontier.sealed
    assert task_ids(frontier.discovery_jobs) == {root.task_id}
    assert not frontier.runnable and not frontier.running

    await h.register(plan.id, [leaf, mid, root])
    frontier = await h.frontier(build)
    assert task_ids(frontier.runnable) == {leaf.task_id}
    assert not frontier.discovery_jobs
    assert frontier.runnable[0].body == leaf.body

    first = await h.start(plan.id, leaf)
    frontier = await h.frontier(build)
    assert task_ids(frontier.running) == {leaf.task_id}
    assert not frontier.runnable

    await h.lapse_claim(leaf)
    frontier = await h.frontier(build)
    assert task_ids(frontier.runnable) == {leaf.task_id}
    assert not frontier.running

    second = await h.start(plan.id, leaf)
    assert second != first
    from stardag_api.services.transitions import Transition

    await h.transition(plan.id, leaf, Transition.complete(second))
    frontier = await h.frontier(build)
    assert task_ids(frontier.runnable) == {mid.task_id}


async def test_plan_complete_needs_the_seal_and_every_member_completed(h: Harness):
    """``plan_complete`` (diagnostic) = sealed, and every non-excluded
    member COMPLETED: an unsealed plan is a request not yet fully stated."""
    root = item("Root")
    build, plan = await h.planned([root], [root])
    await h.run(plan.id, root)
    assert not (await h.frontier(build)).plan_complete
    await h.seal(plan.id)
    assert (await h.frontier(build)).plan_complete


async def test_s23_closure_admits_upstreams_another_plan_expanded(h: Harness):
    """S23 — B admitted X unexpanded (pruned-complete); X is invalidated
    and A expands it first. B's closure step admits X's new upstream U, B's
    discovery job for X is skipped (the shared ``expanded_at`` is set), and
    X is gated in B on a member B holds (closure + shared flag)."""
    deployment = await h.new_deployment()
    u = item("U")
    x = item("X", upstreams=[u])
    rb = item("RB", upstreams=[x])
    build_b, plan_b = await h.planned(
        [rb], [observed(unexpanded(x), True), rb], deployment_id=deployment
    )
    # A's discovery finds X's target missing and expands it: X -> U.
    ra = item("RA", upstreams=[x])
    await h.planned([ra], [u, observed(x, False), ra], deployment_id=deployment)
    assert (await h.task(x))["status"] == "pending"

    frontier = await h.frontier(build_b)
    assert frontier.closure is not None and frontier.closure.admitted == 1
    members = await h.members(plan_b.id)
    assert members[u.task_id]["admitted_by"] == "closure"
    assert x.task_id not in task_ids(frontier.discovery_jobs)
    assert x.task_id not in task_ids(frontier.runnable)
    assert u.task_id in task_ids(frontier.runnable)


async def test_closure_conflict_fails_the_build_naming_both_members(h: Harness):
    """A conflict found by the closure step (the plan holds U1, an edge
    now reaches U2 of the same completion) fails the build naming both
    instances: ``BUILD_FAILED`` is recorded and the frontier reports it."""
    deployment = await h.new_deployment()
    u1 = item("U", extra={"mode": "fast"})
    u2 = item("U", extra={"mode": "slow"})
    x = item("X", upstreams=[u2])
    build_a, plan_a = await h.planned(
        [item("RA", upstreams=[x])],
        [u1, observed(unexpanded(x), True)],
        deployment_id=deployment,
    )
    await h.planned([item("RB", upstreams=[x])], [u2, x], deployment_id=deployment)

    frontier = await h.frontier(build_a)
    assert frontier.closure is not None and frontier.closure.build_failed
    (conflict,) = frontier.closure.conflicts
    assert conflict.task_id == u1.task_id
    assert conflict.fields == ["mode"]
    assert (await h.build(build_a))["status"] == "failed"
    (failed,) = await h.events(build_id=build_a, types=["build_failed"])
    for instance in (conflict.member_instance_id, conflict.other_instance_id):
        assert str(instance) in failed["error_message"]
    assert plan_a.id  # the plan stays; the build failed


@pytest.mark.xfail(reason="v2: I3", strict=True)
async def test_skip_blocked_over_instance_edges(h: Harness):
    """Blocked-by-failure propagation over instance edges within the plan
    (design.md, "The runnable rule"): lands with I3."""
    leaf = item("Leaf")
    root = item("Root", upstreams=[leaf])
    _, plan = await h.planned([root], [leaf, root])
    skip_blocked = getattr(frontier_service, "skip_blocked")
    await skip_blocked(plan.id)


@pytest.mark.xfail(reason="v2: I3", strict=True)
async def test_exclusion_cascades_to_the_downstream_closure(h: Harness):
    """An excluded member's downstream closure within the plan is excluded
    (``upstream_excluded``) and an excluded root fails the build (S18,
    S34): lands with I3."""
    leaf = item("Leaf")
    root = item("Root", upstreams=[leaf])
    _, plan = await h.planned([root], [leaf, root])
    exclude_member = getattr(frontier_service, "exclude_member")
    await exclude_member(plan.id, leaf.task_id)
