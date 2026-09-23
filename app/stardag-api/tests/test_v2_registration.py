"""Registration invariants of the static path (``api-pg`` tier).

Written from ``docs/design/registry-v2/design.md`` ("Registration", "The
deterministic scope", the scenario table) before the service they pin.
Each test names its scenario id and the constraint that decides it. S17
(deleted build) is pinned by ``test_v2_schema_constraints.py``.
"""

from __future__ import annotations

import asyncio
import random
import re
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine

from stardag_api.services.errors import BadRequest, Conflict
from stardag_api.services.registration import MAX_CHUNK_ITEMS
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


# --------------------------------------------------------------------------
# S2 / S10: one instance per completion per plan (plan_member PK)
# --------------------------------------------------------------------------


async def test_s2_second_instance_of_a_completion_in_a_plan_is_refused(h: Harness):
    """S2 — one ``task_id``, two instances, one plan: 409 ``instance_conflict``.

    Decided by the ``plan_member`` primary key ``(plan_id, task_pk)``: the
    plan already holds the completion under another instance.
    """
    a1 = item("A", params={"x": 1}, extra={"threads": 1})
    a2 = item("A", params={"x": 1}, extra={"threads": 2})
    assert a1.task_id == a2.task_id and a1.instance_hash != a2.instance_hash
    root = item("R", upstreams=[a1])
    _, plan = await h.planned([root], [a1])

    with pytest.raises(Conflict) as exc:
        await h.register(plan.id, [a2])
    assert exc.value.code == "instance_conflict"
    assert exc.value.detail["task_id"] == a1.task_id
    members = await h.members(plan.id)
    assert members[a1.task_id]["instance_hash"] == a1.instance_hash


async def test_s2_conflict_inside_one_chunk_lands_nothing(h: Harness):
    """S2 — both instances in one chunk: refused, and the chunk is atomic
    (nothing of it lands, not even the first instance)."""
    b1 = item("B", params={"x": 1}, extra={"threads": 1})
    b2 = item("B", params={"x": 1}, extra={"threads": 2})
    _, plan = await h.planned([item("R")])

    with pytest.raises(Conflict) as exc:
        await h.register(plan.id, [b1, b2])
    assert exc.value.code == "instance_conflict"
    assert await h.task_count(b1) == 0
    assert b1.task_id not in await h.members(plan.id)


async def test_s2_upstream_naming_another_instance_of_a_member_is_refused(
    h: Harness,
):
    """S2 via an edge — a declared upstream that is another instance of a
    completion the plan already holds cannot be admitted (409
    ``instance_conflict``), even though the instance exists in the scope."""
    deployment = await h.new_deployment()
    u1 = item("U", params={"k": 1}, extra={"mode": "fast"})
    u2 = item("U", params={"k": 1}, extra={"mode": "slow"})
    # Another build in the same scope registered u2.
    await h.planned([item("Other")], [u2], deployment_id=deployment)

    d = item("D", upstreams=[u2])
    _, plan = await h.planned([item("R2")], [u1], deployment_id=deployment)
    with pytest.raises(Conflict) as exc:
        await h.register(plan.id, [d])
    assert exc.value.code == "instance_conflict"


async def test_s10_annotation_only_difference_names_the_field(h: Harness):
    """S10 — an annotation-only difference is S2: rejected, with the field
    that differs named (``label``), decided by the ``plan_member`` PK."""
    nightly = item("Report", params={"day": "2026-09-24"}, extra={"label": "nightly"})
    backfill = item("Report", params={"day": "2026-09-24"}, extra={"label": "backfill"})
    _, plan = await h.planned([item("R")], [nightly])

    with pytest.raises(Conflict) as exc:
        await h.register(plan.id, [backfill])
    assert exc.value.code == "instance_conflict"
    assert exc.value.detail["fields"] == ["label"]


# --------------------------------------------------------------------------
# S4 (static half) / S12: the flag and the chunk transaction
# --------------------------------------------------------------------------


async def test_s4_a_chunk_lands_with_its_edges_or_not_at_all(h: Harness):
    """S4, static half — an instance never exists expanded without its
    edges: a chunk naming an upstream missing from the scope is 400 and
    lands nothing; a complete chunk lands every instance with its edges and
    ``expanded_at`` in one transaction (``task_instance.expanded_at``)."""
    x = item("X")
    u = item("U")
    d = item("D", upstreams=[u, x])
    deployment = await h.new_deployment()
    _, plan = await h.planned([item("R")], deployment_id=deployment)

    with pytest.raises(BadRequest) as exc:
        await h.register(plan.id, [u, d])  # x is in no chunk
    assert exc.value.code == "unknown_upstream_instance"
    assert await h.task_count(u) == 0
    assert await h.instance(deployment, d) is None

    await h.register(plan.id, [x, u, d])
    for it in (x, u, d):
        row = await h.instance(deployment, it)
        assert row is not None and row["expanded_at"] is not None
    assert await h.edges(deployment) == {
        (d.instance_hash, u.instance_hash),
        (d.instance_hash, x.instance_hash),
    }
    assert {x.task_id, u.task_id, d.task_id} <= set(await h.members(plan.id))


async def test_s12_complete_at_discovery_is_completed_in_the_same_transaction(
    h: Harness,
):
    """S12 — ``declared_upstreams = null, observed_complete = true``: the
    instance lands unexpanded and the task is COMPLETED in the registering
    transaction (``TASK_OBSERVED_COMPLETE``), so nothing can run it in
    between: it is neither runnable nor a discovery job, and a claiming
    start is 409 ``task_already_completed``."""
    done = observed(item("Done", upstreams=None), True)
    deployment = await h.new_deployment()
    build, plan = await h.planned(
        [item("R", upstreams=[done])], deployment_id=deployment
    )
    await h.register(plan.id, [done])

    task = await h.task(done)
    assert task["status"] == "completed" and task["completed_at"] is not None
    instance = await h.instance(deployment, done)
    assert instance is not None and instance["expanded_at"] is None
    assert [e["type"] for e in await h.events(done)] == [
        "task_pending",
        "task_observed_complete",
    ]
    frontier = await h.frontier(build)
    assert done.task_id not in task_ids(frontier.runnable)
    assert done.task_id not in task_ids(frontier.discovery_jobs)
    with pytest.raises(Conflict) as exc:
        await h.start(plan.id, done)
    assert exc.value.code == "task_already_completed"


# --------------------------------------------------------------------------
# S15: crash mid-registration, finished by a re-send; idempotency
# --------------------------------------------------------------------------


async def test_s15_crash_mid_registration_is_finished_by_a_resend(h: Harness):
    """S15 — the plan exists unsealed with its root as a discovery job; a
    re-send is a no-op for what landed and finishes the rest; the seal
    verifies roots expanded (roots-first, idempotent inserts, ``/seal``)."""
    leaf = item("Leaf")
    root = item("Root", upstreams=[leaf])
    deployment = await h.new_deployment()
    build = await h.new_build([root])
    plan_id = uuid4()
    plan = await h.plan(build, deployment, [root], plan_id=plan_id)
    assert plan.created and plan.activated_at is not None
    await h.register(plan.id, [leaf])
    # ... the driver crashes here.

    with pytest.raises(Conflict) as exc:
        await h.seal(plan.id)
    assert exc.value.code == "plan_incomplete_registration"
    frontier = await h.frontier(build)
    assert task_ids(frontier.discovery_jobs) == {root.task_id}

    # The re-sent plan and chunk change nothing.
    events_before = len(await h.events(build_id=build))
    again = await h.plan(build, deployment, [root], plan_id=plan_id)
    assert again.id == plan.id and not again.created
    result = await h.register(plan.id, [leaf])
    assert result.tasks_created == result.instances_created == 0
    assert result.members_admitted == result.edges_created == 0
    assert len(await h.events(build_id=build)) == events_before

    await h.register(plan.id, [leaf, root])
    sealed = await h.seal(plan.id)
    assert sealed.sealed_at is not None
    # Idempotent by state: a re-delivered seal writes nothing.
    assert (await h.seal(plan.id)).sealed_at == sealed.sealed_at
    frontier = await h.frontier(build)
    assert frontier.sealed and not frontier.discovery_jobs


# --------------------------------------------------------------------------
# S16: concurrent registration of one brand-new task (STA-48)
# --------------------------------------------------------------------------


async def test_s16_registration_waits_on_an_uncommitted_insert_and_references(
    h: Harness, async_engine: AsyncEngine
):
    """S16 — a registration meeting another transaction's uncommitted
    insert of the same brand-new task waits on the unique index (no 500,
    no lock on a missing row) and, once that commits, references the row:
    ``INSERT … ON CONFLICT DO NOTHING RETURNING`` then a plain read."""
    new = item("Brand-new")
    _, plan = await h.planned([item("R", upstreams=[new])])

    async with async_engine.connect() as other:
        await other.execute(
            text(
                "INSERT INTO task (id, environment_id, task_id, task_namespace,"
                " task_name, version, output_uri, status, status_at)"
                " VALUES (:id, :env, :tid, '', :name, :v, :uri, 'pending', now())"
            ),
            {
                "id": uuid4(),
                "env": ENV,
                "tid": new.task_id,
                "name": new.task_name,
                "v": new.version,
                "uri": new.output_uri,
            },
        )
        registering = asyncio.create_task(h.register(plan.id, [new]))
        await asyncio.sleep(0.3)
        assert not registering.done(), "must wait on the uncommitted insert"
        await other.commit()
    result = await registering

    assert result.tasks_created == 0 and result.members_admitted == 1
    assert await h.task_count(new) == 1
    assert [e["type"] for e in await h.events(new, plan_id=plan.id)] == [
        "task_referenced"
    ]


async def test_s16_concurrent_builds_register_one_new_dag_without_error(
    h: Harness, async_engine: AsyncEngine
):
    """S16 — N builds register the same brand-new DAG concurrently, each in
    its own item order: every chunk succeeds, one ``task`` row per
    completion, exactly one ``TASK_PENDING`` per completion and a
    ``TASK_REFERENCED`` for every other plan. The server sorts chunk rows
    by ``(task_id, instance_hash)``, so input order cannot deadlock, and
    takes no ``FOR UPDATE``, and no row lock on ``task`` at all, to register
    (the root's first expansion locks its ``task_instance`` row only)."""
    leaves = [item("Leaf", params={"i": i}) for i in range(40)]
    top = item("Top", upstreams=leaves)
    deployment = await h.new_deployment()
    plans = []
    for _ in range(6):
        _, plan = await h.planned([top], deployment_id=deployment)
        plans.append(plan)

    statements: list[str] = []

    def capture(conn, cursor, statement, *args):  # noqa: ARG001
        statements.append(statement)

    event.listen(async_engine.sync_engine, "before_cursor_execute", capture)
    try:
        chunks = []
        for plan in plans:
            shuffled = list(leaves)
            random.shuffle(shuffled)
            chunks.append(h.register(plan.id, [*shuffled, top]))
        results = await asyncio.gather(*chunks)
    finally:
        event.remove(async_engine.sync_engine, "before_cursor_execute", capture)

    assert sum(r.tasks_created for r in results) == len(leaves)
    assert not [s for s in statements if "FOR UPDATE" in s.upper()]
    row_locks = [
        s for s in statements if re.search(r"\bFOR (NO KEY UPDATE|SHARE)\b", s)
    ]
    assert not [s for s in row_locks if re.search(r"\bFROM task\b(?!_)", s)]
    for leaf in leaves[:5]:
        assert await h.task_count(leaf) == 1
        types = [e["type"] for e in await h.events(leaf)]
        assert types.count("task_pending") == 1
        assert types.count("task_referenced") == len(plans) - 1


# --------------------------------------------------------------------------
# S11 (static half): concurrent chunks into one plan
# --------------------------------------------------------------------------


async def test_s11_concurrent_chunks_into_one_plan(h: Harness):
    """S11, static half — each chunk is one transaction; overlapping
    membership inserts are ``DO NOTHING`` (one member, one event); of two
    concurrent chunks carrying conflicting instances of one completion,
    exactly one lands and the other is 409 (``plan_member`` PK)."""
    shared = item("Shared")
    a, b = item("A", upstreams=[shared]), item("B", upstreams=[shared])
    build, plan = await h.planned([item("R", upstreams=[a, b])])

    await asyncio.gather(
        h.register(plan.id, [shared, a]), h.register(plan.id, [shared, b])
    )
    members = await h.members(plan.id)
    assert {shared.task_id, a.task_id, b.task_id} <= set(members)
    assert len(await h.events(shared, plan_id=plan.id)) == 1

    v1 = item("V", extra={"variant": 1})
    v2 = item("V", extra={"variant": 2})
    outcomes = await asyncio.gather(
        h.register(plan.id, [v1]), h.register(plan.id, [v2]), return_exceptions=True
    )
    refused = [o for o in outcomes if isinstance(o, Conflict)]
    assert len(refused) == 1 and refused[0].code == "instance_conflict"
    assert len([o for o in outcomes if not isinstance(o, BaseException)]) == 1
    frontier = await h.frontier(build)
    assert v1.task_id in task_ids(frontier.runnable)  # whichever instance won


# --------------------------------------------------------------------------
# S31: the observed_at guard
# --------------------------------------------------------------------------


async def test_s31_delayed_duplicate_observation_does_not_undo_a_completion(
    h: Harness,
):
    """S31 — an ``observed_complete: false`` older than ``task.completed_at``
    is not applied (``observed_at`` guard); a newer one invalidates
    (COMPLETED -> PENDING, ``TASK_INVALIDATED``)."""
    t = item("T")
    _, plan = await h.planned([item("R", upstreams=[t])], [t])
    looked_at = utcnow() - timedelta(seconds=2)
    await h.run(plan.id, t)

    await h.register(plan.id, [observed(t, False, looked_at)])
    assert (await h.task(t))["status"] == "completed"
    assert not await h.events(t, types=["task_invalidated"])

    await h.register(plan.id, [observed(t, False)])
    assert (await h.task(t))["status"] == "pending"
    invalidated = await h.events(t, types=["task_invalidated"])
    assert len(invalidated) == 1
    assert invalidated[0]["event_metadata"]["reason"] == "target_missing"


async def test_observation_from_the_future_is_clock_skew(h: Harness):
    """``observed_at`` ahead of server time by more than a few seconds is
    400 ``clock_skew``: forward skew is the only direction that could pass
    the guard wrongly."""
    t = item("T")
    _, plan = await h.planned([item("R")])
    with pytest.raises(BadRequest) as exc:
        await h.register(plan.id, [observed(t, False, utcnow() + timedelta(minutes=1))])
    assert exc.value.code == "clock_skew"


# --------------------------------------------------------------------------
# S38 and the plan lookup-or-create
# --------------------------------------------------------------------------


async def test_s38_retrigger_with_a_changed_non_significant_root_field(h: Harness):
    """S38 — a re-trigger of the same build under the same scope whose root
    instance differs (a non-significant field changed) is 409
    ``root_instance_conflict``: a build is one request (roots recorded on
    the plan). The same roots are a lookup, not a second plan."""
    deployment = await h.new_deployment()
    root = item("Root", params={"day": 1}, extra={"label": "a"})
    build = await h.new_build([root])
    first = await h.plan(build, deployment, [root])

    same = await h.plan(build, deployment, [root])
    assert same.id == first.id and not same.created

    relabelled = item("Root", params={"day": 1}, extra={"label": "b"})
    assert relabelled.task_id == root.task_id
    with pytest.raises(Conflict) as exc:
        await h.plan(build, deployment, [relabelled])
    assert exc.value.code == "root_instance_conflict"


async def test_plans_are_generations_of_a_build_and_only_the_first_activates(
    h: Harness,
):
    """A plan is identified by ``(build, deployment, settings_hash)``; the
    server assigns ``generation`` per build; the first plan is activated on
    create, a replacement is not (it activates at seal)."""
    deployment = await h.new_deployment()
    root = item("Root")
    build = await h.new_build([root])
    first = await h.plan(build, deployment, [root])
    second = await h.plan(build, deployment, [root], settings={"THREADS": "4"})
    assert (first.generation, second.generation) == (1, 2)
    assert first.activated_at is not None and second.activated_at is None
    assert first.settings_hash != second.settings_hash
    assert await h.count("settings") == 2


async def test_create_plan_refuses_a_deployment_that_is_not_activated(h: Harness):
    """A deployment with ``activated_at IS NULL`` cannot host a plan (400)."""
    deployment = await h.new_deployment(activated=False)
    build = await h.new_build([item("Root")])
    with pytest.raises(BadRequest) as exc:
        await h.plan(build, deployment, [item("Root")])
    assert exc.value.code == "deployment_not_activated"


# --------------------------------------------------------------------------
# Identity checks on insert-if-absent rows
# --------------------------------------------------------------------------


async def test_task_identity_conflict(h: Harness):
    """An existing ``task`` row must agree on namespace, name, version and
    ``output_uri`` (409 ``task_identity_conflict``)."""
    t = item("T")
    _, plan = await h.planned([item("R")], [t])
    moved = t.model_copy(update={"output_uri": "memory://elsewhere"})
    with pytest.raises(Conflict) as exc:
        await h.register(plan.id, [moved])
    assert exc.value.code == "task_identity_conflict"


async def test_instance_body_conflict(h: Harness):
    """Same ``(scope, instance_hash)``, different body bytes: 409
    ``instance_body_conflict`` — only a client bug can produce it."""
    t = item("T", extra={"a": 1})
    _, plan = await h.planned([item("R")], [t])
    tampered = t.model_copy(update={"body": {**t.body, "a": 2}})
    with pytest.raises(Conflict) as exc:
        await h.register(plan.id, [tampered])
    assert exc.value.code == "instance_body_conflict"


async def test_chunk_size_is_bounded(h: Harness):
    _, plan = await h.planned([item("R")])
    too_many = [item("L", params={"i": i}) for i in range(MAX_CHUNK_ITEMS + 1)]
    with pytest.raises(BadRequest) as exc:
        await h.register(plan.id, too_many)
    assert exc.value.code == "chunk_too_large"


async def test_structure_divergence_appends_edges_and_records_it(h: Harness):
    """An already-expanded instance re-declared with more upstreams in its
    scope gets the new edges appended and one ``TASK_STRUCTURE_DIVERGED``:
    edges only grow; a re-delivery appends nothing."""
    deployment = await h.new_deployment()
    u1, u2 = item("U1"), item("U2")
    d = item("D", upstreams=[u1])
    _, first = await h.planned([item("R1")], [u1, d], deployment_id=deployment)
    grown = d.model_copy(
        update={"declared_upstreams": [u1.instance_hash, u2.instance_hash]}
    )
    _, second = await h.planned([item("R2")], [u1, u2, grown], deployment_id=deployment)

    assert (d.instance_hash, u2.instance_hash) in await h.edges(deployment)
    assert len(await h.events(d, types=["task_structure_diverged"])) == 1
    await h.register(second.id, [u1, u2, grown])
    assert len(await h.events(d, types=["task_structure_diverged"])) == 1
    assert first.id != second.id


# --------------------------------------------------------------------------
# Seal
# --------------------------------------------------------------------------


async def test_seal_refuses_a_deployment_that_is_no_longer_current(h: Harness):
    """``/seal`` re-checks that the plan's deployment is the app's current
    one (highest activated generation): rollover only moves forward."""
    old = await h.new_deployment(app_name="svc")
    root = item("Root")
    _, plan = await h.planned([root], [root], deployment_id=old)
    await h.new_deployment(app_name="svc")  # a newer deploy, activated
    with pytest.raises(Conflict) as exc:
        await h.seal(plan.id)
    assert exc.value.code == "deployment_not_current"


async def test_seal_latest_request_wins_and_supersedes_the_active_plan(h: Harness):
    """A replacement plan activates at seal and supersedes the active one in
    the same transaction; a plan with a higher-generation sibling is 409
    ``plan_superseded``."""
    deployment = await h.new_deployment()
    root = item("Root")
    build = await h.new_build([root])
    p1 = await h.plan(build, deployment, [root])
    await h.register(p1.id, [root])
    await h.seal(p1.id)
    p2 = await h.plan(build, deployment, [root], settings={"S": "2"})
    p3 = await h.plan(build, deployment, [root], settings={"S": "3"})
    await h.register(p2.id, [root])
    await h.register(p3.id, [root])

    with pytest.raises(Conflict) as exc:
        await h.seal(p2.id)
    assert exc.value.code == "plan_superseded"
    sealed = await h.seal(p3.id)
    assert sealed.activated_at is not None
    frontier = await h.frontier(build)
    assert frontier.plan_id == p3.id


async def test_seal_runs_the_closure_step_before_it_verifies(h: Harness):
    """``/seal`` runs the closure step first, then verifies every edge from
    a member has its upstream as a member. Another plan's expansion of a
    shared instance can add an edge this plan does not hold; the closure
    admits it, so a correct seal does not fail on it."""
    deployment = await h.new_deployment()
    u = item("U")
    x = item("X", upstreams=[u])
    x_done = observed(unexpanded(x), True)
    _, a = await h.planned([x], [x_done], deployment_id=deployment)
    # B expands the same instance of X (same scope): edge X -> U.
    await h.planned([item("RB", upstreams=[x])], [u, x], deployment_id=deployment)

    assert (await h.seal(a.id)).sealed_at is not None
    assert (await h.members(a.id))[u.task_id]["admitted_by"] == "closure"


async def test_seal_refuses_when_its_closure_step_finds_a_conflict(h: Harness):
    """A conflict the seal's closure step finds fails the build (committed)
    and refuses the seal with ``instance_conflict``."""
    deployment = await h.new_deployment()
    u1 = item("U", extra={"mode": "fast"})
    u2 = item("U", extra={"mode": "slow"})
    x = item("X", upstreams=[u2])
    root = item("RA", upstreams=[x])
    build, plan = await h.planned(
        [root], [u1, observed(unexpanded(x), True), root], deployment_id=deployment
    )
    await h.planned([item("RB", upstreams=[x])], [u2, x], deployment_id=deployment)

    with pytest.raises(Conflict) as exc:
        await h.seal(plan.id)
    assert exc.value.code == "instance_conflict"
    assert (await h.build(build))["status"] == "failed"
