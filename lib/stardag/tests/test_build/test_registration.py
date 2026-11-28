"""The shared static-phase path (``stardag.build._registration``): the walk,
the item shape, chunking, and how a yield is split into requests."""

from __future__ import annotations

import asyncio
import typing
from functools import partial
from datetime import datetime, timezone
from unittest import mock

import pytest

from stardag import Task, auto_namespace
from stardag.build import BuildExitStatus, _resident, build_aio
from stardag.build._registration import (
    RequiresError,
    YieldBatch,
    new_id,
    register_plan_aio,
    send_yield,
    send_yield_aio,
    walk_aio,
    yield_batches,
)
from stardag.exceptions import APIError
from stardag.target import InMemoryFileTarget
from stardag.testing import InMemoryRegistry
from stardag.utils.testing.helper_tasks import SyncOnlyTask

auto_namespace(__name__)

Target = typing.Type[InMemoryFileTarget]


def _chain():
    leaf = SyncOnlyTask(name="reg-leaf")
    mid = SyncOnlyTask(name="reg-mid", deps=(leaf,))
    root = SyncOnlyTask(name="reg-root", deps=(mid,))
    return leaf, mid, root


class TestWalk:
    async def test_post_order_and_the_items_state_what_was_seen(
        self, default_in_memory_fs_target: Target
    ):
        leaf, mid, root = _chain()
        walk = await walk_aio([root])
        assert [t.id for t in walk.order] == [leaf.id, mid.id, root.id]
        by_id = {i.task_id: i for i in walk.items()}
        item = by_id[str(mid.id)]
        assert item.declared_upstreams == [str(leaf.instance_hash)]
        assert item.instance_hash == str(mid.instance_hash)
        assert item.body == mid.instance_body()
        assert item.task_name == "SyncOnlyTask"
        assert item.version == mid.version
        assert item.output_uri == mid.target().uri
        assert item.observed_complete is False
        assert item.observed_at <= datetime.now(timezone.utc)

    async def test_the_walk_stops_at_a_complete_task(
        self, default_in_memory_fs_target: Target
    ):
        leaf, mid, root = _chain()
        mid.target().save({"done": True})
        walk = await walk_aio([root])
        assert [t.id for t in walk.order] == [mid.id, root.id]
        item = walk.item(mid)
        assert item.observed_complete is True
        # requires() of the complete task was never evaluated.
        assert item.declared_upstreams is None
        assert mid.id not in walk.deps

    async def test_roots_are_admitted_unexpanded(
        self, default_in_memory_fs_target: Target
    ):
        _, _, root = _chain()
        walk = await walk_aio([root])
        (item,) = walk.root_items()
        assert item.declared_upstreams is None

    async def test_the_stability_check_runs_once_per_distinct_instance(
        self, default_in_memory_fs_target: Target
    ):
        leaf = SyncOnlyTask(name="shared-leaf")
        a = SyncOnlyTask(name="a", deps=(leaf,))
        b = SyncOnlyTask(name="b", deps=(SyncOnlyTask(name="shared-leaf"),))
        root = SyncOnlyTask(name="diamond", deps=(a, b))
        with mock.patch(
            "stardag.build._registration.check_serialization_stability"
        ) as check:
            await walk_aio([root])
        checked = [call.args[0].id for call in check.call_args_list]
        assert sorted(checked) == sorted({root.id, a.id, b.id, leaf.id})

    async def test_a_requires_that_raises_is_a_requires_error(
        self, default_in_memory_fs_target: Target
    ):
        class Broken(SyncOnlyTask):
            def requires(self):
                raise KeyError("missing")

        with pytest.raises(RequiresError) as excinfo:
            await walk_aio([Broken(name="broken")])
        assert isinstance(excinfo.value.__cause__, KeyError)


class TestYieldBatches:
    async def test_one_batch_carries_the_children_and_their_closure(
        self, default_in_memory_fs_target: Target
    ):
        leaf, mid, _ = _chain()
        walk = await walk_aio([mid])
        (batch,) = yield_batches(walk, [mid], suspend=True)
        assert [i.task_id for i in batch.items] == [str(leaf.id), str(mid.id)]
        assert batch.yielded == [str(mid.instance_hash)]
        assert batch.suspend is True

    async def test_known_closure_is_not_resent_but_a_known_child_is(
        self, default_in_memory_fs_target: Target
    ):
        leaf, mid, _ = _chain()
        walk = await walk_aio([mid])
        (batch,) = yield_batches(walk, [mid], suspend=False, known={leaf.id, mid.id})
        assert [i.task_id for i in batch.items] == [str(mid.id)]

    async def test_a_yield_larger_than_a_chunk_goes_through_yield_only(
        self, default_in_memory_fs_target: Target
    ):
        """STA-182: the plan is sealed while anything runs, so every batch
        of a yield is a ``/yield`` (yielding at least one item), cut in
        post-order: an item's upstreams are in its own batch or an earlier
        one, and only the last batch suspends."""
        chunk_size = 10
        children = [
            SyncOnlyTask(name=f"child-{i}", deps=(SyncOnlyTask(name=f"leaf-{i}"),))
            for i in range(13)
        ]
        walk = await walk_aio(children)
        batches = yield_batches(walk, children, suspend=True, chunk_size=chunk_size)

        items = [i for b in batches for i in b.items]
        assert len(items) == 26 > 2 * chunk_size
        assert sorted(i.instance_hash for i in items) == sorted(
            str(t.instance_hash) for t in walk.order
        )
        assert all(0 < len(b.items) <= chunk_size for b in batches)
        _assert_upstreams_first(batches)
        assert [b.suspend for b in batches] == [False] * (len(batches) - 1) + [True]
        # Every child is yielded once, and every batch yields what it holds.
        assert sorted(h for b in batches for h in b.yielded) == sorted(
            str(c.instance_hash) for c in children
        )
        for b in batches:
            assert b.yielded
            assert set(b.yielded) <= {i.instance_hash for i in b.items}

    async def test_a_batch_of_closure_only_yields_nothing_and_names_a_stand_in(
        self, default_in_memory_fs_target: Target
    ):
        """One child whose closure alone spans several chunks: a batch with
        no child yields nothing, and names its last item -- a task of that
        child's closure, whose edge from the parent is implied by its edge
        to the child -- as the stand-in an older registry is sent."""
        leaves = tuple(SyncOnlyTask(name=f"wide-leaf-{i}") for i in range(25))
        child = SyncOnlyTask(name="wide-child", deps=leaves)
        walk = await walk_aio([child])
        batches = yield_batches(walk, [child], suspend=True, chunk_size=10)

        assert [len(b.items) for b in batches] == [10, 10, 6]
        _assert_upstreams_first(batches)
        leaf_hashes = {str(leaf.instance_hash) for leaf in leaves}
        for b in batches[:-1]:
            assert b.yielded == [] and not b.suspend
            assert b.stand_in == b.items[-1].instance_hash
            assert b.stand_in in leaf_hashes
        last = batches[-1]
        assert last.yielded == [str(child.instance_hash)]
        assert (last.suspend, last.stand_in) == (True, None)

    async def test_known_closure_is_left_out_of_every_batch(
        self, default_in_memory_fs_target: Target
    ):
        leaves = [SyncOnlyTask(name=f"k-leaf-{i}") for i in range(8)]
        children = [
            SyncOnlyTask(name=f"k-child-{i}", deps=(leaf,))
            for i, leaf in enumerate(leaves)
        ]
        walk = await walk_aio(children)
        known = {leaves[0].id, leaves[1].id}
        # A child that is known is still yielded.
        known.add(children[2].id)
        batches = yield_batches(walk, children, suspend=True, known=known, chunk_size=4)
        sent = {i.task_id for b in batches for i in b.items}
        assert sent == {str(t.id) for t in walk.order if t.id not in known} | {
            str(children[2].id)
        }
        assert len(batches) == 4
        assert sorted(h for b in batches for h in b.yielded) == sorted(
            str(c.instance_hash) for c in children
        )

    async def test_a_yield_of_nothing_is_no_batch(
        self, default_in_memory_fs_target: Target
    ):
        """Nothing is sent for ``yield []``: the generator carries on."""
        walk = await walk_aio([])
        assert yield_batches(walk, [], suspend=True) == []


def _assert_upstreams_first(batches: typing.Sequence[YieldBatch]) -> None:
    """Every declared upstream is sent in an earlier batch or the same one."""
    batch_of = {i.instance_hash: n for n, b in enumerate(batches) for i in b.items}
    for n, b in enumerate(batches):
        for i in b.items:
            for up in i.declared_upstreams or ():
                assert batch_of[up] <= n, (i.task_id, up)


async def test_a_multi_chunk_yield_lands_in_the_sealed_plan(
    default_in_memory_fs_target: Target,
):
    """STA-182 end to end against the in-memory registry: a member of a
    sealed plan yields a closure larger than a chunk. Everything lands,
    the plan stays sealed, ``/members`` is never called after the seal
    (which refuses new structure), and the parent suspends once."""
    registry = InMemoryRegistry()
    deployment = registry.add_deployment(kind="local", code_id="c")
    parent = SyncOnlyTask(name="sealed-parent")
    build_id = registry.build_create(root_task_ids=[str(parent.id)]).id
    plan = await register_plan_aio(
        registry,
        build_id,
        await walk_aio([parent]),
        deployment_id=deployment,
        settings={},
    )
    assert plan.sealed_at is not None
    execution_id = new_id()
    await registry.member_start_aio(plan.id, str(parent.id), execution_id=execution_id)

    children = [
        SyncOnlyTask(name=f"s-child-{i}", deps=(SyncOnlyTask(name=f"s-leaf-{i}"),))
        for i in range(12)
    ]
    walk = await walk_aio(children)
    members_calls = len(registry.calls_to("plan_register_members"))
    await send_yield_aio(
        registry,
        yield_batches(walk, children, suspend=True, chunk_size=5),
        plan_id=plan.id,
        task_id=str(parent.id),
        execution_id=execution_id,
        deployment_id=deployment,
    )

    assert len(registry.calls_to("plan_register_members")) == members_calls
    yields = registry.calls_to("member_yield")
    assert len(yields) == 5
    assert [c["suspend"] for c in yields] == [False] * 4 + [True]
    members = registry.members[plan.id]
    assert {str(t.id) for t in walk.order} <= members.keys()
    assert registry.plans[plan.id].sealed_at == plan.sealed_at
    assert registry.tasks[str(parent.id)].status == "suspended"

    # And the fake refuses what the registry refuses: new structure through
    # ``/members`` on a sealed plan.
    with pytest.raises(APIError) as excinfo:
        await registry.plan_register_members_aio(
            plan.id, (await walk_aio([SyncOnlyTask(name="late")])).items()
        )
    assert excinfo.value.code == "plan_sealed"


async def test_register_plan_is_idempotent_under_the_same_scope(
    default_in_memory_fs_target: Target,
):
    registry = InMemoryRegistry()
    deployment = registry.add_deployment(kind="local", code_id="c")
    _, _, root = _chain()
    build_id = registry.build_create(root_task_ids=[str(root.id)]).id
    walk = await walk_aio([root])
    first = await register_plan_aio(
        registry, build_id, walk, deployment_id=deployment, settings={}
    )
    second = await register_plan_aio(
        registry, build_id, walk, deployment_id=deployment, settings={}
    )
    assert first.id == second.id
    assert first.sealed_at is not None
    assert len(registry.plans) == 1


class _WideYield(Task[int]):
    """Yields ``width`` children, each requiring a leaf of its own."""

    width: int

    def run(self):
        yield [
            SyncOnlyTask(
                name=f"wide-yield-child-{i}",
                deps=(SyncOnlyTask(name=f"wide-yield-leaf-{i}"),),
            )
            for i in range(self.width)
        ]
        self._save(self.width)


async def test_a_resident_build_runs_a_yield_over_the_chunk_size(
    default_in_memory_fs_target: Target, monkeypatch: pytest.MonkeyPatch
):
    """STA-182 through the resident engine: a yield of about 2.5 chunks
    (children plus their leaves) is registered into the sealed plan and run.
    The chunk size is lowered to keep the build small; the real one is the
    registry's cap."""
    chunk_size = 10
    monkeypatch.setattr(
        _resident, "yield_batches", partial(yield_batches, chunk_size=chunk_size)
    )
    registry = InMemoryRegistry()
    width = (chunk_size * 5) // 4
    root = _WideYield(width=width)
    summary = await build_aio([root], registry=registry)

    assert summary.status == BuildExitStatus.SUCCESS
    assert root.load() == width
    (plan_id,) = registry.plans
    assert registry.plans[plan_id].sealed_at is not None
    assert len(registry.members[plan_id]) == 2 * width + 1
    yields = registry.calls_to("member_yield")
    assert len(yields) == 3
    assert all(c["yielded"] for c in yields)


class _WideChildYield(Task[int]):
    """Yields one child whose own closure spans several chunks."""

    leaves: int

    def child(self) -> SyncOnlyTask:
        return SyncOnlyTask(
            name="wide-closure-child",
            deps=tuple(
                SyncOnlyTask(name=f"wide-closure-leaf-{i}") for i in range(self.leaves)
            ),
        )

    def run(self):
        yield self.child()
        self._save(self.leaves)


@pytest.mark.parametrize("accepts_empty_yielded", [True, False])
async def test_a_resident_build_yields_a_closure_wider_than_a_chunk(
    default_in_memory_fs_target: Target,
    monkeypatch: pytest.MonkeyPatch,
    accepts_empty_yielded: bool,
):
    """STA-182, both registries: a closure-only batch yields nothing to a
    registry that accepts it, so the parent's only dynamic edge is to its
    child; an older registry refuses that (422, nothing applied), and the
    batch and every later closure-only batch of the yield are sent with
    their stand-in instead. The build succeeds either way."""
    chunk_size = 10
    monkeypatch.setattr(
        _resident, "yield_batches", partial(yield_batches, chunk_size=chunk_size)
    )
    registry = InMemoryRegistry(accepts_empty_yielded=accepts_empty_yielded)
    root = _WideChildYield(leaves=25)
    summary = await build_aio([root], registry=registry)

    assert summary.status == BuildExitStatus.SUCCESS
    assert root.load() == 25
    (plan_id,) = registry.plans
    members = registry.members[plan_id]
    assert len(members) == 25 + 2
    parent = registry.instances[members[str(root.id)].instance_id]
    edges_to = {registry.instances[u].task_id for u in parent.upstreams}
    yielded = [c["yielded"] for c in registry.calls_to("member_yield")]
    if accepts_empty_yielded:
        assert [len(y) for y in yielded] == [0, 0, 1]
        # The parent's only dynamic edge is to its child.
        assert edges_to == {str(root.child().id)}
    else:
        # The first closure-only batch is refused once and resent; the
        # second goes straight to its stand-in.
        assert [len(y) for y in yielded] == [0, 1, 1, 1]
        assert len(edges_to) == 3 and str(root.child().id) in edges_to


def test_send_yield_falls_back_to_the_stand_in_for_an_older_registry(
    default_in_memory_fs_target: Target,
):
    """The sync sender (a Modal worker's) does the same as the async one."""
    registry = InMemoryRegistry(accepts_empty_yielded=False)
    deployment = registry.add_deployment(kind="local", code_id="c")
    parent = SyncOnlyTask(name="sync-parent")
    build_id = registry.build_create(root_task_ids=[str(parent.id)]).id
    plan = asyncio.run(
        register_plan_aio(
            registry,
            build_id,
            asyncio.run(walk_aio([parent])),
            deployment_id=deployment,
            settings={},
        )
    )
    execution_id = new_id()
    registry.member_start(plan.id, str(parent.id), execution_id=execution_id)
    leaves = tuple(SyncOnlyTask(name=f"sync-leaf-{i}") for i in range(12))
    child = SyncOnlyTask(name="sync-child", deps=leaves)
    walk = asyncio.run(walk_aio([child]))
    batches = yield_batches(walk, [child], suspend=True, chunk_size=10)
    send_yield(
        registry,
        batches,
        plan_id=plan.id,
        task_id=str(parent.id),
        execution_id=execution_id,
        deployment_id=deployment,
    )
    yielded = [c["yielded"] for c in registry.calls_to("member_yield")]
    assert yielded == [[], [batches[0].stand_in], [str(child.instance_hash)]]
    assert registry.tasks[str(parent.id)].status == "suspended"


class _YieldsNothing(Task[int]):
    def run(self):
        yield []
        self._save(1)


async def test_a_yield_of_nothing_is_a_no_op_with_a_registry(
    default_in_memory_fs_target: Target,
):
    """``yield []`` sends nothing and the generator carries on: the build
    succeeds, as it did when the empty yield went to ``/members``."""
    registry = InMemoryRegistry()
    task = _YieldsNothing()
    summary = await build_aio([task], registry=registry)
    assert summary.status == BuildExitStatus.SUCCESS
    assert task.load() == 1
    assert registry.calls_to("member_yield") == []
