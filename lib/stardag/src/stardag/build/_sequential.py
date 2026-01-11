"""Sequential build functions for debugging.

These functions execute tasks one at a time in dependency order.
Intended for debugging and testing, not production use.
"""

from __future__ import annotations

import asyncio
from typing import Literal
from uuid import UUID

from stardag._task import (
    BaseTask,
    _has_custom_run,
    _has_custom_run_aio,
    flatten_task_struct,
)
from stardag.build._base import (
    BuildExitStatus,
    BuildSummary,
    FailMode,
    TaskCount,
)
from stardag.registry import NoOpRegistry, RegistryABC


def build_sequential(
    tasks: list[BaseTask],
    registry: RegistryABC | None = None,
    fail_mode: FailMode = FailMode.FAIL_FAST,
    dual_run_default: Literal["sync", "async"] = "sync",
) -> BuildSummary:
    """Sync API for building tasks sequentially.

    This is intended primarily for debugging and testing.

    Task execution policy:
    - Sync-only tasks: run via `run()`
    - Async-only tasks: run via `asyncio.run(run_aio())`. (Does not work if called
        from within an existing event loop.)
    - Dual tasks: run via `run()` if `dual_run_default=="sync"` (default), else
        (`dual_run_default=="async"`) via `asyncio.run(run_aio())`.

    Args:
        tasks: List of root tasks to build (and their dependencies)
        registry: Registry for tracking builds
        fail_mode: How to handle task failures
        dual_run_default: For dual tasks, prefer sync or async execution

    Returns:
        BuildSummary with status and task counts
    """
    registry = registry or NoOpRegistry()
    task_count = TaskCount()
    completion_cache: set[UUID] = set()
    failed_cache: set[UUID] = set()
    error: Exception | None = None

    # Discover all tasks
    all_tasks: dict[UUID, BaseTask] = {}

    def discover(task: BaseTask) -> None:
        if task.id in all_tasks:
            return
        all_tasks[task.id] = task
        for dep in flatten_task_struct(task.requires()):
            discover(dep)

    for root in tasks:
        discover(root)

    task_count.discovered = len(all_tasks)

    # Check initial completion
    for task in all_tasks.values():
        if task.complete():
            completion_cache.add(task.id)
            task_count.previously_completed += 1

    registry.start_build(root_tasks=tasks)

    def has_failed_dep(task: BaseTask) -> bool:
        """Check if any dependency has failed."""
        deps = flatten_task_struct(task.requires())
        return any(d.id in failed_cache for d in deps)

    try:
        # Build in topological order
        while True:
            # Find a task ready to run
            ready_task: BaseTask | None = None
            for task in all_tasks.values():
                if task.id in completion_cache or task.id in failed_cache:
                    continue
                # Skip tasks with failed dependencies
                if has_failed_dep(task):
                    continue
                deps = flatten_task_struct(task.requires())
                if all(d.id in completion_cache for d in deps):
                    ready_task = task
                    break

            if ready_task is None:
                # No more tasks can run - either all done or blocked by failures
                break

            # Execute the task
            try:
                _run_task_sequential(
                    ready_task,
                    completion_cache,
                    all_tasks,
                    registry,
                    dual_run_default,
                )
                task_count.succeeded += 1
            except Exception as e:
                task_count.failed += 1
                failed_cache.add(ready_task.id)
                error = e
                registry.fail_task(ready_task, str(e))
                if fail_mode == FailMode.FAIL_FAST:
                    raise

        registry.complete_build()
        return BuildSummary(
            status=BuildExitStatus.SUCCESS
            if error is None
            else BuildExitStatus.FAILURE,
            task_count=task_count,
            error=error,
        )

    except Exception as e:
        registry.fail_build(str(e))
        return BuildSummary(
            status=BuildExitStatus.FAILURE,
            task_count=task_count,
            error=e,
        )


def _run_task_sequential(
    task: BaseTask,
    completion_cache: set[UUID],
    all_tasks: dict[UUID, BaseTask],
    registry: RegistryABC,
    dual_run_default: Literal["sync", "async"],
) -> None:
    """Run a single task in sequential mode, handling dynamic deps."""
    registry.start_task(task)

    has_run = _has_custom_run(task)
    has_run_aio = _has_custom_run_aio(task)

    # Determine how to run
    use_async = False
    if has_run_aio and not has_run:
        # Async-only
        use_async = True
    elif has_run and has_run_aio:
        # Dual
        use_async = dual_run_default == "async"
    # else: sync-only, use_async = False

    # Execute
    if use_async:
        result = asyncio.run(task.run_aio())
    else:
        result = task.run()

    # Handle generator (dynamic deps)
    if result is not None and hasattr(result, "__next__"):
        gen = result
        while True:
            try:
                yielded = next(gen)
                dynamic_deps = flatten_task_struct(yielded)

                # Discover and build dynamic deps
                for dep in dynamic_deps:
                    if dep.id not in all_tasks:
                        all_tasks[dep.id] = dep
                        # Recursively discover
                        for sub_dep in flatten_task_struct(dep.requires()):
                            if sub_dep.id not in all_tasks:
                                all_tasks[sub_dep.id] = sub_dep

                    if dep.id not in completion_cache:
                        _run_task_sequential(
                            dep, completion_cache, all_tasks, registry, dual_run_default
                        )

            except StopIteration:
                break

    completion_cache.add(task.id)
    registry.complete_task(task)

    # Upload registry assets if any
    assets = task.registry_assets()
    if assets:
        registry.upload_task_assets(task, assets)


async def build_sequential_aio(
    tasks: list[BaseTask],
    registry: RegistryABC | None = None,
    fail_mode: FailMode = FailMode.FAIL_FAST,
    sync_run_default: Literal["thread", "blocking"] = "blocking",
) -> BuildSummary:
    """Async API for building tasks sequentially.

    This is intended primarily for debugging and testing.

    Task execution policy:
    - Sync-only tasks: runs *blocking* via `run()` in main event loop if
        `sync_run_default=="blocking"` (default), else (`sync_run_default=="thread"`)
        in thread pool.
    - Async-only tasks: run via `await run_aio()`.
    - Dual tasks: run via `await run_aio()`.

    Args:
        tasks: List of root tasks to build (and their dependencies)
        registry: Registry for tracking builds
        fail_mode: How to handle task failures
        sync_run_default: For sync-only tasks, block or use thread pool

    Returns:
        BuildSummary with status and task counts
    """
    registry = registry or NoOpRegistry()
    task_count = TaskCount()
    completion_cache: set[UUID] = set()
    failed_cache: set[UUID] = set()
    error: Exception | None = None

    # Discover all tasks
    all_tasks: dict[UUID, BaseTask] = {}

    def discover(task: BaseTask) -> None:
        if task.id in all_tasks:
            return
        all_tasks[task.id] = task
        for dep in flatten_task_struct(task.requires()):
            discover(dep)

    for root in tasks:
        discover(root)

    task_count.discovered = len(all_tasks)

    # Check initial completion
    for task in all_tasks.values():
        if await task.complete_aio():
            completion_cache.add(task.id)
            task_count.previously_completed += 1

    await registry.start_build_aio(root_tasks=tasks)

    def has_failed_dep(task: BaseTask) -> bool:
        """Check if any dependency has failed."""
        deps = flatten_task_struct(task.requires())
        return any(d.id in failed_cache for d in deps)

    try:
        # Build in topological order
        while True:
            # Find a task ready to run
            ready_task: BaseTask | None = None
            for task in all_tasks.values():
                if task.id in completion_cache or task.id in failed_cache:
                    continue
                # Skip tasks with failed dependencies
                if has_failed_dep(task):
                    continue
                deps = flatten_task_struct(task.requires())
                if all(d.id in completion_cache for d in deps):
                    ready_task = task
                    break

            if ready_task is None:
                # No more tasks can run - either all done or blocked by failures
                break

            # Execute the task
            try:
                await _run_task_sequential_aio(
                    ready_task,
                    completion_cache,
                    all_tasks,
                    registry,
                    sync_run_default,
                )
                task_count.succeeded += 1
            except Exception as e:
                task_count.failed += 1
                failed_cache.add(ready_task.id)
                error = e
                await registry.fail_task_aio(ready_task, str(e))
                if fail_mode == FailMode.FAIL_FAST:
                    raise

        await registry.complete_build_aio()
        return BuildSummary(
            status=BuildExitStatus.SUCCESS
            if error is None
            else BuildExitStatus.FAILURE,
            task_count=task_count,
            error=error,
        )

    except Exception as e:
        await registry.fail_build_aio(str(e))
        return BuildSummary(
            status=BuildExitStatus.FAILURE,
            task_count=task_count,
            error=e,
        )


async def _run_task_sequential_aio(
    task: BaseTask,
    completion_cache: set[UUID],
    all_tasks: dict[UUID, BaseTask],
    registry: RegistryABC,
    sync_run_default: Literal["thread", "blocking"],
) -> None:
    """Run a single task in async sequential mode, handling dynamic deps."""
    await registry.start_task_aio(task)

    has_run = _has_custom_run(task)
    has_run_aio = _has_custom_run_aio(task)

    # Determine how to run
    if has_run_aio:
        # Async-only or Dual - use async
        result = await task.run_aio()
    elif has_run:
        # Sync-only
        if sync_run_default == "thread":
            result = await asyncio.to_thread(task.run)
        else:
            # Blocking - not recommended but useful for debugging
            result = task.run()
    else:
        raise ValueError(f"Task {task} has no run method")

    # Handle generator (dynamic deps)
    if result is not None and hasattr(result, "__next__"):
        gen = result
        while True:
            try:
                yielded = next(gen)
                dynamic_deps = flatten_task_struct(yielded)

                # Discover and build dynamic deps
                for dep in dynamic_deps:
                    if dep.id not in all_tasks:
                        all_tasks[dep.id] = dep
                        # Recursively discover
                        for sub_dep in flatten_task_struct(dep.requires()):
                            if sub_dep.id not in all_tasks:
                                all_tasks[sub_dep.id] = sub_dep

                    if dep.id not in completion_cache:
                        await _run_task_sequential_aio(
                            dep, completion_cache, all_tasks, registry, sync_run_default
                        )

            except StopIteration:
                break

    completion_cache.add(task.id)
    await registry.complete_task_aio(task)

    # Upload registry assets if any
    assets = task.registry_assets_aio()
    if assets:
        await registry.upload_task_assets_aio(task, assets)


__all__ = [
    "build_sequential",
    "build_sequential_aio",
]
