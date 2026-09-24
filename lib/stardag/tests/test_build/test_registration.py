"""The shared static-phase path (``stardag.build._registration``): the walk,
the item shape, chunking, and how a yield is split into requests."""

from __future__ import annotations

import typing
from datetime import datetime, timezone
from unittest import mock

import pytest

from stardag import auto_namespace
from stardag.build._registration import (
    RequiresError,
    register_plan_aio,
    walk_aio,
    yield_batches,
)
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

    async def test_a_large_yield_registers_its_closure_first_then_yields_the_children(
        self, default_in_memory_fs_target: Target
    ):
        leaves = [SyncOnlyTask(name=f"closure-{i}") for i in range(5)]
        children = [
            SyncOnlyTask(name=f"child-{i}", deps=tuple(leaves)) for i in range(3)
        ]
        walk = await walk_aio(children)
        batches = yield_batches(walk, children, suspend=True, chunk_size=2)
        closure = [b for b in batches if not b.yielded]
        yields = [b for b in batches if b.yielded]
        assert sum(len(b.items) for b in closure) == 5
        assert [len(b.items) for b in yields] == [2, 1]
        # Only the last carries the suspend; every child is yielded once.
        assert [b.suspend for b in yields] == [False, True]
        assert sorted(h for b in yields for h in b.yielded) == sorted(
            str(c.instance_hash) for c in children
        )
        assert batches.index(closure[-1]) < batches.index(yields[0])


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
