"""``/yield``: the dynamic phase (``api-pg`` tier).

Written from design.md, "Registration" (the Dynamic phase block), "Rollover"
(a yield into a superseded plan) and the scenario table. Each test names
its scenario id and the rule that decides it.
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import uuid4

import pytest
from httpx import AsyncClient

from stardag_api.services.errors import BadRequest, Conflict
from stardag_api.services.registration import MAX_CHUNK_ITEMS
from stardag_api.services.transitions import Transition
from tests.v2_support import Harness, item, observed, task_ids


@pytest.fixture
def h(session_factory) -> Harness:
    return Harness(session_factory)


async def test_s4_a_yield_lands_its_children_with_their_closure(h: Harness):
    """S4 — the children and their static closure land in the yield's one
    transaction, expanded, with parent→child edges ``is_dynamic``; the
    parent is SUSPENDED, gated on its children, and runnable again (run
    from scratch) once they are COMPLETED."""
    parent = item("P")
    build, plan = await h.planned([parent], [parent], seal=True)
    execution = await h.start(plan.id, parent)

    u = item("U")
    c = item("C", upstreams=[u])
    result = await h.yield_(plan.id, parent, execution, [u, c], yielded=[c])
    assert not result.replayed and result.status == "suspended"
    assert result.members.members_admitted == 2 and result.dynamic_edges_created == 1

    members = await h.members(plan.id)
    assert members[c.task_id]["admitted_by"] == "dynamic"
    assert members[u.task_id]["admitted_by"] == "static"
    deployment = members[c.task_id]["deployment_id"]
    for child in (u, c):
        instance = await h.instance(deployment, child)
        assert instance is not None and instance["expanded_at"] is not None
    assert (parent.instance_hash, c.instance_hash) in await h.edges(deployment)
    ledger = await h.execution(execution)
    assert (ledger["claim_outcome"], ledger["outcome"]) == ("suspended", "suspended")
    (yielded,) = await h.events(parent, types=["task_yielded"])
    assert yielded["batch_id"] is not None and yielded["report_applied"]

    frontier = await h.frontier(build)
    assert task_ids(frontier.runnable) == {u.task_id}
    await h.run(plan.id, u)
    assert task_ids((await h.frontier(build)).runnable) == {c.task_id}
    await h.run(plan.id, c)
    assert task_ids((await h.frontier(build)).runnable) == {parent.task_id}
    rerun = await h.start(plan.id, parent)
    assert rerun != execution


async def test_resident_yield_keeps_the_claim(h: Harness):
    """``suspend: false`` (the resident engine) leaves the parent RUNNING
    and claimed; a later batch with ``suspend: true`` releases it."""
    parent = item("P")
    _, plan = await h.planned([parent], [parent])
    execution = await h.start(plan.id, parent)
    kept = await h.yield_(plan.id, parent, execution, [item("C1")], suspend=False)
    assert kept.status == "running" and kept.execution_id == execution
    assert (await h.task(parent))["claim_plan_id"] == plan.id
    last = await h.yield_(plan.id, parent, execution, [item("C2")], suspend=True)
    assert last.status == "suspended"


async def test_a_retried_batch_after_a_lost_response_is_replayed(h: Harness):
    """A ``suspend: true`` batch released the claim; its retry (same
    ``execution_id`` and ``batch_id``) is found by the typed ``batch_id``
    under the parent's row lock and replayed with the stored result — not
    refused by the execution check, and nothing is written twice."""
    parent = item("P")
    _, plan = await h.planned([parent], [parent])
    execution = await h.start(plan.id, parent)
    batch = uuid4()
    children = [item("C1"), item("C2")]
    first = await h.yield_(plan.id, parent, execution, children, batch_id=batch)
    events = len(await h.events())

    again = await h.yield_(plan.id, parent, execution, children, batch_id=batch)
    assert again.replayed
    assert (again.members, again.status) == (first.members, first.status)
    assert again.dynamic_edges_created == first.dynamic_edges_created == 2
    assert len(await h.events()) == events

    concurrent = await asyncio.gather(
        *(
            h.yield_(plan.id, parent, execution, children, batch_id=batch)
            for _ in range(3)
        )
    )
    assert all(r.replayed for r in concurrent)
    assert len(await h.events()) == events


async def test_s7_a_yield_into_a_superseded_plan_is_accepted(h: Harness):
    """S7 — an execution started under the old plan yields after a
    replacement superseded it: the deployment matches the old plan, so the
    instances, edges and (inert) membership are accepted and the parent is
    SUSPENDED globally. The new plan's instance of the parent has no
    dynamic edges in its scope, so it is runnable there and restarts."""
    deployment = await h.new_deployment()
    parent = item("P")
    build = await h.new_build([parent])
    old = await h.plan(build, deployment, [parent])
    await h.register(old.id, [parent])
    await h.seal(old.id)
    execution = await h.start(old.id, parent)

    new = await h.plan(build, deployment, [parent], settings={"S": "2"})
    await h.register(new.id, [parent])
    await h.seal(new.id)  # old is superseded

    child = item("C")
    result = await h.yield_(old.id, parent, execution, [child])
    assert result.status == "suspended"
    assert child.task_id in await h.members(old.id)
    assert child.task_id not in await h.members(new.id)
    frontier = await h.frontier(build)
    assert frontier.plan_id == new.id
    assert task_ids(frontier.runnable) == {parent.task_id}
    await h.start(new.id, parent)


async def test_a_worker_never_yields_into_another_scope(h: Harness):
    """``deployment_mismatch``: a worker whose deployment is not the plan's
    is refused before anything is written."""
    parent = item("P")
    _, plan = await h.planned([parent], [parent])
    execution = await h.start(plan.id, parent)
    with pytest.raises(Conflict) as exc:
        await h.yield_(plan.id, parent, execution, [item("C")], deployment_id=uuid4())
    assert exc.value.code == "deployment_mismatch"
    assert set(await h.members(plan.id)) == {parent.task_id}
    assert (await h.task(parent))["status"] == "running"


async def test_a_stale_executions_batch_is_recorded_and_refused(h: Harness):
    """A batch from an execution whose claim was taken over is recorded
    (``report_applied = false``, no typed ``batch_id``) and refused 409;
    nothing lands. An unknown execution is refused likewise."""
    parent = item("P")
    _, plan = await h.planned([parent], [parent])
    stale = await h.start(plan.id, parent)
    await h.lapse_claim(parent)
    await h.start(plan.id, parent)

    with pytest.raises(Conflict) as exc:
        await h.yield_(plan.id, parent, stale, [item("C")])
    assert exc.value.code == "execution_not_current"
    with pytest.raises(Conflict) as unknown:
        await h.yield_(plan.id, parent, uuid4(), [item("C")])
    assert unknown.value.code == "unknown_execution"
    refused = await h.events(parent, types=["task_yielded"])
    assert [(e["report_applied"], e["batch_id"]) for e in refused] == [
        (False, None),
        (False, None),
    ]
    assert set(await h.members(plan.id)) == {parent.task_id}


async def test_a_yield_after_the_claim_lapsed_but_before_takeover_is_applied(
    h: Harness,
):
    """The authority rule keys on the execution, not the clock: a lapsed
    claim still names its execution until a claiming start takes it over."""
    parent = item("P")
    _, plan = await h.planned([parent], [parent])
    execution = await h.start(plan.id, parent)
    await h.lapse_claim(parent)
    result = await h.yield_(plan.id, parent, execution, [item("C")])
    assert result.status == "suspended"


async def test_s11_concurrent_yields_into_one_plan(h: Harness):
    """S11 — two parents yield concurrently into one plan, sharing a child:
    each ``/yield`` is one transaction and membership inserts are ``DO
    NOTHING``, so both apply and the shared child is one member."""
    a, b = item("A"), item("B")
    root = item("Root", upstreams=[a, b])
    _, plan = await h.planned([root], [a, b, root])
    ea, eb = await h.start(plan.id, a), await h.start(plan.id, b)
    shared = item("Shared")
    ra, rb = await asyncio.gather(
        h.yield_(plan.id, a, ea, [shared, item("OnlyA")]),
        h.yield_(plan.id, b, eb, [shared, item("OnlyB")]),
    )
    assert ra.members.members_admitted + rb.members.members_admitted == 3
    assert ra.dynamic_edges_created == rb.dynamic_edges_created == 2
    members = await h.members(plan.id)
    assert {shared.task_id, a.task_id, b.task_id} <= set(members)
    assert (await h.task(a))["status"] == (await h.task(b))["status"] == "suspended"


async def test_s11_a_conflicting_instance_fails_the_yielding_member(h: Harness):
    """S11 (conflict half) — a batch carrying a second instance of a
    completion the plan holds is 409 ``instance_conflict``, non-retryable:
    no item lands and the parent is failed with both instances named."""
    held = item("U", extra={"mode": "fast"})
    parent = item("P")
    root = item("Root", upstreams=[held, parent])
    _, plan = await h.planned([root], [held, parent, root])
    execution = await h.start(plan.id, parent)

    other = item("U", extra={"mode": "slow"})
    with pytest.raises(Conflict) as exc:
        await h.yield_(plan.id, parent, execution, [item("C"), other])
    assert exc.value.code == "instance_conflict"
    assert exc.value.detail["fields"] == ["mode"]
    assert item("C").task_id not in await h.members(plan.id)
    task = await h.task(parent)
    assert task["status"] == "failed"
    assert "another instance" in task["error_message"]
    assert "mode" in task["error_message"]
    ledger = await h.execution(execution)
    assert (ledger["claim_outcome"], ledger["outcome"]) == ("failed", "failed")


async def test_s13_a_yield_that_fails_to_register_lands_nothing(h: Harness):
    """S13 — a registration error mid-batch rolls the whole yield back (no
    partial children, the parent still RUNNING and claimed); the worker
    then reports ``TASK_FAILED`` with the error, which applies."""
    parent = item("P")
    _, plan = await h.planned([parent], [parent])
    execution = await h.start(plan.id, parent)
    good = item("Good")
    orphan = item("Orphan", upstreams=[item("NeverRegistered")])
    with pytest.raises(BadRequest) as exc:
        await h.yield_(plan.id, parent, execution, [good, orphan])
    assert exc.value.code == "unknown_upstream_instance"
    assert set(await h.members(plan.id)) == {parent.task_id}
    assert not await h.events(parent, types=["task_yielded", "task_suspended"])
    assert (await h.task(parent))["status"] == "running"

    await h.transition(
        plan.id, parent, Transition.fail(execution, "registration failed")
    )
    assert (await h.task(parent))["status"] == "failed"


async def test_s28_a_yielded_child_whose_target_vanished_is_invalidated(
    h: Harness,
):
    """S28 — a yielded child already COMPLETED whose target is missing: the
    item carries ``observed_complete: false`` and is invalidated in the
    yield's transaction, so it is runnable under the parent's plan."""
    deployment = await h.new_deployment()
    child = item("C")
    _, other = await h.planned([child], [child], deployment_id=deployment)
    await h.run(other.id, child)

    parent = item("P")
    build, plan = await h.planned([parent], [parent], deployment_id=deployment)
    execution = await h.start(plan.id, parent)
    result = await h.yield_(plan.id, parent, execution, [observed(child, False)])
    assert result.members.invalidated == 1
    assert (await h.task(child))["status"] == "pending"
    assert task_ids((await h.frontier(build)).runnable) == {child.task_id}


async def test_s29_a_yielded_child_running_in_another_build(h: Harness):
    """S29 — a yielded child RUNNING in another build is admitted with its
    edges; it is not runnable while that claim is live (it shows as
    running, whoever holds it), and the parent waits on its global status."""
    deployment = await h.new_deployment()
    child = item("C")
    _, other = await h.planned([child], [child], deployment_id=deployment)
    await h.start(other.id, child)

    parent = item("P")
    build, plan = await h.planned([parent], [parent], deployment_id=deployment)
    execution = await h.start(plan.id, parent)
    await h.yield_(plan.id, parent, execution, [child])
    frontier = await h.frontier(build)
    assert frontier.runnable == []
    assert task_ids(frontier.running) == {child.task_id}


async def test_a_yield_larger_than_a_chunk_lands_in_the_sealed_plan(h: Harness):
    """STA-182 — a yield whose static closure exceeds one chunk arrives as
    several ``/yield`` batches of the same execution, as the SDK cuts it:
    post-order, at most ``MAX_CHUNK_ITEMS`` items each, every batch
    yielding the children it carries (a batch of closure only yields its
    last item, a task of a later child's closure), ``suspend`` on the last
    only. Every batch applies to the sealed plan, which stays sealed and
    still refuses new structure through ``/members``."""
    parent = item("P")
    build, plan = await h.planned([parent], [parent], seal=True)
    sealed_at = plan.sealed_at
    assert sealed_at is not None
    execution = await h.start(plan.id, parent)

    # One child whose closure alone spans more than a chunk, then many
    # children with a leaf each: about 2.4 chunks in all.
    wide_leaves = [item("WideLeaf", params={"i": i}) for i in range(1200)]
    wide = item("Wide", upstreams=wide_leaves)
    leaves = [item("Leaf", params={"i": i}) for i in range(600)]
    children = [
        item("Child", params={"i": i}, upstreams=[leaf])
        for i, leaf in enumerate(leaves)
    ]
    order = [*wide_leaves, wide]
    for leaf, child in zip(leaves, children):
        order += [leaf, child]
    child_hashes = {c.instance_hash for c in [wide, *children]}
    batches = [
        order[start : start + MAX_CHUNK_ITEMS]
        for start in range(0, len(order), MAX_CHUNK_ITEMS)
    ]
    assert len(batches) == 3

    results = []
    for n, batch in enumerate(batches):
        yielded = [i for i in batch if i.instance_hash in child_hashes] or [batch[-1]]
        results.append(
            await h.yield_(
                plan.id,
                parent,
                execution,
                batch,
                yielded=yielded,
                suspend=n == len(batches) - 1,
            )
        )
    assert [r.status for r in results] == ["running", "running", "suspended"]
    assert sum(r.members.members_admitted for r in results) == len(order)

    members = await h.members(plan.id)
    assert {i.task_id for i in order} <= set(members)
    assert members[wide.task_id]["admitted_by"] == "dynamic"
    assert members[children[0].task_id]["admitted_by"] == "dynamic"
    (row,) = await h._rows("SELECT sealed_at FROM plan WHERE id = :p", p=plan.id)
    assert row["sealed_at"] == sealed_at
    yields = await h.events(parent, types=["task_yielded"])
    assert len(yields) == 3 and all(e["report_applied"] for e in yields)

    # The parent waits on its children; the leaves are what runs first.
    frontier = await h.frontier(build)
    assert frontier.sealed and frontier.plan_id == plan.id
    assert task_ids(frontier.runnable) == {i.task_id for i in [*wide_leaves, *leaves]}

    with pytest.raises(Conflict) as exc:
        await h.register(plan.id, [item("Late")])
    assert exc.value.code == "plan_sealed"


async def test_a_closure_only_batch_lands_without_a_dynamic_edge(h: Harness):
    """STA-182 — a batch carrying closure only (``yielded`` empty, ahead of
    the batch with its children) is applied to the sealed plan: its items
    are admitted ``static``, the parent gets no dynamic edge to them, keeps
    its claim, and the plan stays sealed."""
    parent = item("P")
    build, plan = await h.planned([parent], [parent], seal=True)
    execution = await h.start(plan.id, parent)
    u1, u2 = item("U1"), item("U2")

    result = await h.yield_(plan.id, parent, execution, [u1, u2], [], suspend=False)
    assert result.members.members_admitted == 2
    assert result.dynamic_edges_created == 0
    assert (result.status, result.execution_id) == ("running", execution)

    members = await h.members(plan.id)
    assert members[u1.task_id]["admitted_by"] == "static"
    assert members[u2.task_id]["admitted_by"] == "static"
    deployment = members[u1.task_id]["deployment_id"]
    edges = await h.edges(deployment)
    assert not {e for e in edges if e[0] == parent.instance_hash}
    assert (await h.frontier(build)).sealed
    (yielded,) = await h.events(parent, types=["task_yielded"])
    assert yielded["report_applied"]


async def test_a_closure_only_batch_cannot_suspend(h: Harness):
    """A parent suspended on nothing would wait on nothing: a batch that
    yields nothing and asks to suspend is refused, and nothing lands."""
    parent = item("P")
    _, plan = await h.planned([parent], [parent], seal=True)
    execution = await h.start(plan.id, parent)
    u = item("U")
    with pytest.raises(BadRequest) as exc:
        await h.yield_(plan.id, parent, execution, [u], [], suspend=True)
    assert exc.value.code == "suspend_without_yielded"
    assert set(await h.members(plan.id)) == {parent.task_id}
    assert (await h.task(parent))["status"] == "running"


async def test_a_closure_only_batch_is_replayed(h: Harness):
    """A closure-only batch is recorded like any other: its retry (same
    ``execution_id`` and ``batch_id``) is replayed, writing nothing."""
    parent = item("P")
    _, plan = await h.planned([parent], [parent], seal=True)
    execution = await h.start(plan.id, parent)
    batch = uuid4()
    items = [item("U1"), item("U2")]
    first = await h.yield_(
        plan.id, parent, execution, items, [], suspend=False, batch_id=batch
    )
    events = len(await h.events())
    again = await h.yield_(
        plan.id, parent, execution, items, [], suspend=False, batch_id=batch
    )
    assert again.replayed and not first.replayed
    assert again.members == first.members
    assert again.dynamic_edges_created == first.dynamic_edges_created == 0
    assert len(await h.events()) == events


async def test_a_yield_whose_middle_batch_is_closure_only(h: Harness):
    """STA-182 — a yield of three batches in post-order: a child with its
    leaf, then the closure of a second child alone, then that child with
    ``suspend``. The parent gets a dynamic edge to each child and to
    nothing else, is SUSPENDED, and the leaves are what runs first."""
    parent = item("P")
    build, plan = await h.planned([parent], [parent], seal=True)
    execution = await h.start(plan.id, parent)
    leaf_a = item("LeafA")
    a = item("A", upstreams=[leaf_a])
    leaves_b = [item("LeafB", params={"i": i}) for i in range(3)]
    b = item("B", upstreams=leaves_b)

    first = await h.yield_(plan.id, parent, execution, [leaf_a, a], [a], suspend=False)
    middle = await h.yield_(plan.id, parent, execution, leaves_b, [], suspend=False)
    last = await h.yield_(plan.id, parent, execution, [b], [b], suspend=True)
    assert [r.dynamic_edges_created for r in (first, middle, last)] == [1, 0, 1]
    assert [r.status for r in (first, middle, last)] == [
        "running",
        "running",
        "suspended",
    ]

    deployment = (await h.members(plan.id))[a.task_id]["deployment_id"]
    from_parent = {
        up for down, up in await h.edges(deployment) if down == parent.instance_hash
    }
    assert from_parent == {a.instance_hash, b.instance_hash}
    frontier = await h.frontier(build)
    assert frontier.sealed
    assert task_ids(frontier.runnable) == {i.task_id for i in [leaf_a, *leaves_b]}
    assert len(await h.events(parent, types=["task_yielded"])) == 3


async def test_an_empty_batch_is_still_refused_over_http(
    client: AsyncClient, h: Harness
):
    """A batch with no items at all stays a validation error; one with
    items and nothing yielded is accepted when it does not suspend."""
    parent = item("P")
    _, plan = await h.planned([parent], [parent], seal=True)
    execution = await h.start(plan.id, parent)
    path = f"/api/v2/plans/{plan.id}/members/{parent.task_id}/yield"
    body: dict[str, Any] = {
        "execution_id": str(execution),
        "deployment_id": str(plan.deployment_id),
        "batch_id": str(uuid4()),
        "items": [],
        "yielded": [],
        "suspend": False,
    }
    empty = await client.post(path, json=body)
    assert empty.status_code == 422, empty.text
    closure_only = await client.post(
        path, json={**body, "items": [item("U").model_dump(mode="json")]}
    )
    assert closure_only.status_code == 200, closure_only.text
    assert closure_only.json()["dynamic_edges_created"] == 0
    suspending = await client.post(
        path,
        json={
            **body,
            "batch_id": str(uuid4()),
            "items": [item("V").model_dump(mode="json")],
            "suspend": True,
        },
    )
    assert suspending.status_code == 400, suspending.text
    assert suspending.json()["detail"]["code"] == "suspend_without_yielded"


async def test_yielded_names_items_of_the_batch(h: Harness):
    parent = item("P")
    _, plan = await h.planned([parent], [parent])
    execution = await h.start(plan.id, parent)
    with pytest.raises(BadRequest) as exc:
        await h.yield_(plan.id, parent, execution, [item("C")], yielded=[item("X")])
    assert exc.value.code == "unknown_yielded_instance"


async def test_yield_over_http(client: AsyncClient, h: Harness):
    parent = item("P")
    _, plan = await h.planned([parent], [parent])
    execution = await h.start(plan.id, parent)
    child = item("C")
    body: dict[str, Any] = {
        "execution_id": str(execution),
        "deployment_id": str(plan.deployment_id),
        "batch_id": str(uuid4()),
        "items": [child.model_dump(mode="json")],
        "yielded": [child.instance_hash],
        "suspend": True,
    }
    path = f"/api/v2/plans/{plan.id}/members/{parent.task_id}/yield"
    first = await client.post(path, json=body)
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "suspended" and not first.json()["replayed"]
    again = await client.post(path, json=body)
    assert again.status_code == 200 and again.json()["replayed"]
    mismatch = await client.post(path, json={**body, "deployment_id": str(uuid4())})
    assert mismatch.status_code == 409
    assert mismatch.json()["detail"]["code"] == "deployment_mismatch"


async def test_a_yield_and_a_preemption_come_through_the_claims_plan(h: Harness):
    """The authority rule's plan half, for ``/yield`` and ``/preempt``: the
    current execution of a task shared by two builds, claimed through plan
    A, is 409 ``not_claim_holder`` under plan B — lapsed or not — and leaves
    no trace; through plan A the same batch applies."""
    deployment = await h.new_deployment()
    parent = item("P")
    _, plan_a = await h.planned([parent], [parent], deployment_id=deployment)
    _, plan_b = await h.planned([parent], [parent], deployment_id=deployment)
    execution = await h.start(plan_a.id, parent)
    events_before = len(await h.events(parent))
    batch = uuid4()

    with pytest.raises(Conflict) as exc:
        await h.yield_(plan_b.id, parent, execution, [item("C")], batch_id=batch)
    assert exc.value.code == "not_claim_holder"
    await h.lapse_claim(parent)
    with pytest.raises(Conflict) as exc:
        await h.transition(plan_b.id, parent, Transition.preempt(execution))
    assert exc.value.code == "not_claim_holder"
    assert len(await h.events(parent)) == events_before
    assert item("C").task_id not in await h.members(plan_b.id)

    result = await h.yield_(plan_a.id, parent, execution, [item("C")], batch_id=batch)
    assert result.status == "suspended" and not result.replayed
