"""Discovery and its bounded concurrency
(stardag.build._reactive._discovery)."""

from __future__ import annotations

import asyncio
import typing
from uuid import UUID, uuid4


from stardag import (
    BaseTask,
    TaskStruct as TaskStructType,
    flatten_task_struct,
)
from stardag.build import (
    discover_and_register_aio,
)
from stardag.target import InMemoryFileTarget
from stardag.utils.testing.helper_tasks import SyncOnlyTask

from tests.test_build.reactive_fakes import (
    FakeReactiveRegistry,
)


class TestDiscoverAndRegister:
    async def test_post_order_registration_and_previously_completed(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        done_leaf = SyncOnlyTask(name="disc-done-leaf")
        done_leaf.run()  # complete
        fresh_leaf = SyncOnlyTask(name="disc-fresh-leaf")
        root = SyncOnlyTask(name="disc-root", deps=(done_leaf, fresh_leaf))

        registry = FakeReactiveRegistry(root_task_ids=[str(root.id)])
        result = await discover_and_register_aio(registry, uuid4(), root)

        assert set(result.incomplete) == {fresh_leaf.id, root.id}
        assert [t.id for t in result.previously_completed] == [done_leaf.id]
        register_order = [tid for (m, tid) in registry.calls if m == "register"]
        assert register_order.index(str(fresh_leaf.id)) < register_order.index(
            str(root.id)
        )
        # Previously-complete tasks are reflected as completed in the registry
        # (the frontier is the scheduler state).
        assert registry.statuses[str(done_leaf.id)] == "completed"


class TrackedTask(SyncOnlyTask):
    """SyncOnlyTask whose completion check is observable and suspends.

    The suspension is what makes concurrency measurable at all: the
    in-memory target answers synchronously, so without it a "concurrent"
    walk and a serial one are indistinguishable.
    """

    # Class-level because discovery constructs nothing — the tracker has to
    # outlive individual instances and be shared across the whole walk.
    tracker: typing.ClassVar[dict[str, int]] = {}

    async def complete_aio(self) -> bool:
        TrackedTask.tracker["in_flight"] = TrackedTask.tracker.get("in_flight", 0) + 1
        TrackedTask.tracker["max_in_flight"] = max(
            TrackedTask.tracker.get("max_in_flight", 0),
            TrackedTask.tracker["in_flight"],
        )
        TrackedTask.tracker["checks"] = TrackedTask.tracker.get("checks", 0) + 1
        try:
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            return await super().complete_aio()
        finally:
            TrackedTask.tracker["in_flight"] -= 1


async def _serial_discover(
    tasks: TaskStructType,
) -> tuple[list[UUID], list[UUID], list[UUID]]:
    """The pre-concurrency walk, verbatim, as the reference implementation.

    Returns ``(post_order, incomplete, previously_completed)`` as id lists.
    Kept in the test rather than in the module so the concurrent
    implementation has something independent to be identical to.
    """
    post_order: list[BaseTask] = []
    incomplete: dict[UUID, BaseTask] = {}
    previously_completed: list[BaseTask] = []
    seen: set[UUID] = set()

    async def walk(task: BaseTask) -> None:
        if task.id in seen:
            return
        seen.add(task.id)
        if await task.complete_aio():
            previously_completed.append(task)
            post_order.append(task)
            return
        for dep in flatten_task_struct(task.requires()):
            await walk(dep)
        incomplete[task.id] = task
        post_order.append(task)

    for task in flatten_task_struct(tasks):
        await walk(task)
    return (
        [t.id for t in post_order],
        list(incomplete),
        [t.id for t in previously_completed],
    )


def _diamond() -> tuple[BaseTask, list[BaseTask]]:
    """A diamond with a shared leaf, a completed branch, and two roots.

    Shape (arrows point at dependencies)::

        root ─┬─ left  ─┬─ shared ── deep
              └─ right ─┘
              └─ done            (already complete: not recursed into)
    """
    deep = TrackedTask(name="dia-deep")
    shared = TrackedTask(name="dia-shared", deps=(deep,))
    left = TrackedTask(name="dia-left", deps=(shared,))
    right = TrackedTask(name="dia-right", deps=(shared,))
    done_dep = TrackedTask(name="dia-done-dep")
    done = TrackedTask(name="dia-done", deps=(done_dep,))
    done.run()  # complete → its subtree must NOT be walked
    root = TrackedTask(name="dia-root", deps=(left, right, done))
    return root, [deep, shared, left, right, done, done_dep, root]


class TestConcurrentDiscovery:
    async def test_matches_the_serial_walk_exactly_for_a_diamond(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """Same DAG, same DiscoveryResult — element for element, in order.

        Concurrency here buys throughput and nothing else: a walk whose
        whole job is to get an ordering right may not have its output
        depend on which completion check answered first.
        """
        root, _ = _diamond()
        (
            expected_post_order,
            expected_incomplete,
            expected_completed,
        ) = await _serial_discover(root)

        registry = FakeReactiveRegistry(root_task_ids=[str(root.id)])
        result = await discover_and_register_aio(registry, uuid4(), root)

        assert list(result.incomplete) == expected_incomplete
        assert [t.id for t in result.previously_completed] == expected_completed
        assert result.retried == []
        # Registration order is the post-order the bulk endpoint relies on.
        registered = [
            UUID(tid) for (method, tid) in registry.calls if method == "register"
        ]
        assert registered == expected_post_order

    async def test_post_order_holds_under_concurrency(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """Every dependency is registered before the task that needs it —
        which is what keeps the bulk endpoint from creating phantom rows
        while resolving ``dependency_task_ids``."""
        root, all_tasks = _diamond()
        registry = FakeReactiveRegistry(root_task_ids=[str(root.id)])

        await discover_and_register_aio(registry, uuid4(), root)

        order = {
            tid: index
            for index, tid in enumerate(
                [tid for (method, tid) in registry.calls if method == "register"]
            )
            if tid is not None
        }
        by_id = {str(task.id): task for task in all_tasks}
        for tid, index in order.items():
            task = by_id[tid]
            if str(task.id) == str(
                next(t.id for t in all_tasks if getattr(t, "name") == "dia-done")
            ):
                continue  # complete → not recursed into, deps not registered
            for dep in flatten_task_struct(task.requires()):
                assert order[str(dep.id)] < index

    async def test_completion_checks_run_concurrently_within_the_bound(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """The bound is pinned, not just "it works": a wide layer's checks
        overlap, and never more than ``max_concurrent_discover`` at once.

        Without the semaphore the peak would be the whole layer; without
        the TaskGroup it would be 1 — the serial wall that made discovery
        50x slower than the resident engine's."""
        width, bound = 120, 6
        leaves = [TrackedTask(name=f"disc-wide-{i}") for i in range(width)]
        root = TrackedTask(name="disc-wide-root", deps=tuple(leaves))
        registry = FakeReactiveRegistry(root_task_ids=[str(root.id)])
        TrackedTask.tracker.clear()

        result = await discover_and_register_aio(
            registry, uuid4(), root, max_concurrent_discover=bound
        )

        assert len(result.incomplete) == width + 1
        assert TrackedTask.tracker["max_in_flight"] <= bound
        assert TrackedTask.tracker["max_in_flight"] == bound

    async def test_shared_dependency_is_checked_and_registered_once(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """The dedupe the serial walk got for free from being serial: two
        concurrent walkers reaching the same dep must not double-register
        it, and must not lose the branch either."""
        root, all_tasks = _diamond()
        registry = FakeReactiveRegistry(root_task_ids=[str(root.id)])
        TrackedTask.tracker.clear()

        result = await discover_and_register_aio(registry, uuid4(), root)

        registered = [tid for (method, tid) in registry.calls if method == "register"]
        assert len(registered) == len(set(registered))
        # deep/shared/left/right/root incomplete; done complete; done's own
        # dep never walked (complete subtrees are not recursed into).
        by_name = {typing.cast(typing.Any, t).name: t for t in all_tasks}
        assert set(result.incomplete) == {
            by_name[name].id
            for name in ("dia-deep", "dia-shared", "dia-left", "dia-right", "dia-root")
        }
        assert [t.id for t in result.previously_completed] == [by_name["dia-done"].id]
        assert str(by_name["dia-done-dep"].id) not in registered
        # One completion check per visited task, no more.
        assert TrackedTask.tracker["checks"] == len(registered)

    async def test_retry_failed_preserves_order_and_membership(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """Retries now run concurrently; ``retried`` still comes back in the
        registry's own reporting order, not in completion order."""
        leaves = [SyncOnlyTask(name=f"disc-retry-{i}") for i in range(20)]
        root = SyncOnlyTask(name="disc-retry-root", deps=tuple(leaves))
        registry = FakeReactiveRegistry(root_task_ids=[str(root.id)])
        for task in [*leaves, root]:
            registry.add_task(str(task.id), status="failed")

        result = await discover_and_register_aio(
            registry, uuid4(), root, retry_failed=True
        )

        registered = [
            UUID(tid) for (method, tid) in registry.calls if method == "register"
        ]
        assert [t.id for t in result.retried] == registered
        assert all(registry.statuses[str(t.id)] == "pending" for t in [*leaves, root])


# =============================================================================
# Bounded concurrent fan-out
# =============================================================================
