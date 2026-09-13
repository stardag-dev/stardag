"""Discovery and its bounded concurrency
(stardag.build._reactive._discovery)."""

from __future__ import annotations

import asyncio
import typing
from uuid import UUID, uuid4

import pytest

from stardag import (
    BaseTask,
    TaskStruct as TaskStructType,
    flatten_task_struct,
)
from stardag.build import (
    discover_and_register_aio,
)
from stardag.exceptions import DependencyDeclarationConflictError
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


class TestDeclarationConflict:
    """What a build does when the registry refuses its declaration.

    The registry refuses a chunk that re-points a task another *live* build
    is building differently. Both declarations are legitimate — a task's id
    promises its output, not how it was produced — so the question is not
    which is right but which build should stop.
    """

    def _conflict(self, other_build: UUID) -> DependencyDeclarationConflictError:
        return DependencyDeclarationConflictError(
            "R is declared differently by a running build",
            task_id="R",
            declared=["U2"],
            recorded=["U1"],
            build_ids=[str(other_build)],
        )

    async def test_the_conflict_surfaces_by_default(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """The new build fails, at the trigger, naming the builds in the
        way. That is the default because the alternative — cancelling
        somebody else's running build — is not something to do without
        being asked."""
        root = SyncOnlyTask(name="conflict-root")
        registry = FakeReactiveRegistry(root_task_ids=[str(root.id)])
        registry.declaration_conflict = self._conflict(uuid4())

        with pytest.raises(DependencyDeclarationConflictError) as caught:
            await discover_and_register_aio(registry, uuid4(), root)

        assert caught.value.task_id == "R"
        assert registry.cancelled_builds == [], "nothing was cancelled unasked"

    async def test_cancel_conflicting_clears_the_way_and_retries(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """The opt-in answer: take the tasks over.

        Cascading, because a cancel that left the other build's containers
        running would put two executions on the same tasks — the very thing
        the refusal exists to prevent.
        """
        root = SyncOnlyTask(name="conflict-cancel-root")
        other = uuid4()
        registry = FakeReactiveRegistry(root_task_ids=[str(root.id)])
        registry.declaration_conflict = self._conflict(other)

        result = await discover_and_register_aio(
            registry, uuid4(), root, cancel_conflicting=True
        )

        assert registry.cancelled_builds == [(other, True)]
        assert root.id in result.incomplete, "the retry did not go through"

    async def test_every_build_in_the_way_is_cleared_not_just_the_first(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """A refusal names the first conflicting task, so a chunk colliding
        with two builds over two tasks surfaces them one at a time.

        Retrying once would cancel the first build and then fail on the
        second — having taken somebody's work down and still not run, which
        is the worst of both answers.
        """
        root = SyncOnlyTask(name="conflict-two-builds-root")
        first, second = uuid4(), uuid4()
        registry = FakeReactiveRegistry(root_task_ids=[str(root.id)])
        registry.declaration_conflicts = [
            self._conflict(first),
            self._conflict(second),
        ]

        result = await discover_and_register_aio(
            registry, uuid4(), root, cancel_conflicting=True
        )

        assert registry.cancelled_builds == [(first, True), (second, True)]
        assert root.id in result.incomplete, "the retry did not go through"

    async def test_a_refusal_naming_only_cancelled_builds_gives_up(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """The loop's own termination condition. Cancelling a build it has
        already cancelled would clear nothing, so a refusal that names only
        those means something is starting builds faster than this can stop
        them — and looping on that is worse than failing."""
        root = SyncOnlyTask(name="conflict-same-build-root")
        other = uuid4()
        registry = FakeReactiveRegistry(root_task_ids=[str(root.id)])
        registry.declaration_conflicts = [
            self._conflict(other),
            self._conflict(other),
        ]

        with pytest.raises(DependencyDeclarationConflictError):
            await discover_and_register_aio(
                registry, uuid4(), root, cancel_conflicting=True
            )
        assert registry.cancelled_builds == [(other, True)], "cancelled once only"

    async def test_one_refusal_naming_a_crowd_cancels_none_of_them(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """The budget covers what a pass *would* cancel, not what it has.

        A refusal names every running holder of the task, so one response
        can exceed the limit on its own — and cancelling up to the limit
        and then failing would be the worst answer available: work taken
        down, way still not clear.
        """
        from stardag.build._reactive._discovery import _MAX_CONFLICTING_BUILDS

        root = SyncOnlyTask(name="conflict-crowd-root")
        crowd = [uuid4() for _ in range(_MAX_CONFLICTING_BUILDS + 1)]
        registry = FakeReactiveRegistry(root_task_ids=[str(root.id)])
        registry.declaration_conflict = DependencyDeclarationConflictError(
            "R is declared differently",
            task_id="R",
            build_ids=[str(b) for b in crowd],
        )

        with pytest.raises(DependencyDeclarationConflictError):
            await discover_and_register_aio(
                registry, uuid4(), root, cancel_conflicting=True
            )
        assert registry.cancelled_builds == [], (
            "cancelled part of the crowd and still failed"
        )

    async def test_a_conflict_naming_nobody_is_not_retried(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """Retrying would hit the same refusal forever: there is nothing to
        cancel, so nothing this build can do will clear it."""
        root = SyncOnlyTask(name="conflict-nobody-root")
        registry = FakeReactiveRegistry(root_task_ids=[str(root.id)])
        registry.declaration_conflict = DependencyDeclarationConflictError(
            "R is declared differently", task_id="R", build_ids=[]
        )

        with pytest.raises(DependencyDeclarationConflictError):
            await discover_and_register_aio(
                registry, uuid4(), root, cancel_conflicting=True
            )
        assert registry.cancelled_builds == []
