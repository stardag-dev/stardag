"""Multiprocessing-based concurrent build implementation.

This implementation uses Python's multiprocessing for CPU-bound tasks.
It provides true parallelism by running tasks in separate processes.

Limitations:
- Tasks must be picklable (Pydantic models generally are)
- Dynamic dependencies are NOT supported in this implementation
  because generators cannot be pickled/sent between processes
- Higher overhead than threading for I/O-bound tasks

This implementation is best for:
- CPU-bound task.run() methods
- Tasks that don't use dynamic dependencies
- When you need to bypass the GIL

For dynamic dependencies, use threadpool or asyncio builders.
"""

from __future__ import annotations

import logging
import multiprocessing as mp
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from stardag._task import BaseTask, flatten_task_struct
from stardag.build.registry import RegistryABC, registry_provider

logger = logging.getLogger(__name__)


# Worker function that runs in separate process
def _run_task_in_process(
    task_json: str, task_class_path: str
) -> tuple[UUID, bool, str | None]:
    """Run a task in a separate process.

    Args:
        task_json: JSON-serialized task
        task_class_path: Full import path of task class

    Returns:
        (task_id, success, error_message)
    """
    import importlib
    import json

    try:
        # Reconstruct the task from JSON
        module_path, class_name = task_class_path.rsplit(".", 1)
        module = importlib.import_module(module_path)
        task_class = getattr(module, class_name)

        task_data = json.loads(task_json)
        task = task_class.model_validate(task_data)

        # Check if already complete
        if task.complete():
            return task.id, True, None

        # Check for dynamic deps (not supported)
        if task.has_dynamic_deps():
            return (
                task.id,
                False,
                "Dynamic dependencies not supported in multiprocessing mode",
            )

        # Run the task
        result = task.run()
        if result is not None:
            return (
                task.id,
                False,
                "Task returned generator (dynamic deps not supported)",
            )

        return task.id, True, None

    except Exception as e:
        # We can't easily get task.id here if reconstruction failed
        return UUID(int=0), False, str(e)


@dataclass
class TaskNode:
    """Node in the task DAG for scheduling."""

    task: BaseTask
    deps: list[UUID]
    completed: bool = False
    submitted: bool = False


def build(
    task: BaseTask,
    *,
    max_workers: int | None = None,
    completion_cache: set[UUID] | None = None,
    registry: RegistryABC | None = None,
) -> None:
    """Build a task DAG using ProcessPoolExecutor for parallelism.

    WARNING: Dynamic dependencies are NOT supported in this mode.
    Use threadpool or asyncio builders for tasks with dynamic deps.

    Args:
        task: Root task to build
        max_workers: Maximum number of worker processes
        completion_cache: Set of already-completed task IDs
        registry: Registry for tracking builds
    """
    registry = registry or registry_provider.get()
    completion_cache = completion_cache or set()

    registry.start_build(root_tasks=[task])

    try:
        build_with_multiprocessing(
            task,
            max_workers=max_workers,
            completion_cache=completion_cache,
        )
        registry.complete_build()
    except Exception as e:
        registry.fail_build(str(e))
        raise


def build_with_multiprocessing(
    root_task: BaseTask,
    *,
    max_workers: int | None = None,
    completion_cache: set[UUID],
) -> None:
    """Core multiprocessing build logic.

    Algorithm:
    1. Build DAG of all tasks with dependencies
    2. Topologically schedule tasks (submit when deps complete)
    3. Run tasks in process pool
    4. Collect results and update completion state
    """
    # Build task graph
    task_nodes: dict[UUID, TaskNode] = {}

    def discover_task(task: BaseTask) -> TaskNode:
        """Recursively discover tasks and build graph."""
        if task.id in task_nodes:
            return task_nodes[task.id]

        # Check if already complete
        if task.id in completion_cache or task.complete():
            completion_cache.add(task.id)
            node = TaskNode(task=task, deps=[], completed=True)
            task_nodes[task.id] = node
            return node

        # Check for dynamic deps (not supported)
        if task.has_dynamic_deps():
            raise NotImplementedError(
                f"Task {task} has dynamic dependencies, which are not supported "
                "in multiprocessing mode. Use threadpool or asyncio builders."
            )

        # Get dependencies
        deps = flatten_task_struct(task.requires())
        dep_ids = []

        for dep in deps:
            dep_node = discover_task(dep)
            if not dep_node.completed:
                dep_ids.append(dep.id)

        node = TaskNode(task=task, deps=dep_ids)
        task_nodes[task.id] = node
        return node

    # Discover all tasks
    discover_task(root_task)

    # Check if root is already complete
    if task_nodes[root_task.id].completed:
        return

    def get_ready_tasks() -> list[TaskNode]:
        """Get tasks that are ready to submit (all deps complete)."""
        ready = []
        for node in task_nodes.values():
            if node.completed or node.submitted:
                continue
            if all(task_nodes[dep_id].completed for dep_id in node.deps):
                ready.append(node)
        return ready

    # Execute with process pool
    max_workers = max_workers or mp.cpu_count()

    with ProcessPoolExecutor(max_workers=max_workers) as executor:
        futures_to_task: dict[Any, UUID] = {}

        while not task_nodes[root_task.id].completed:
            # Submit ready tasks
            ready = get_ready_tasks()

            for node in ready:
                task = node.task
                # Serialize task for sending to process
                task_json = task.model_dump_json()
                task_class_path = (
                    f"{task.__class__.__module__}.{task.__class__.__name__}"
                )

                future = executor.submit(
                    _run_task_in_process, task_json, task_class_path
                )
                futures_to_task[future] = task.id
                node.submitted = True

            if not futures_to_task:
                # No tasks submitted and root not complete - error
                incomplete = [n for n in task_nodes.values() if not n.completed]
                raise RuntimeError(
                    f"No tasks ready but {len(incomplete)} tasks incomplete"
                )

            # Wait for at least one completion
            for future in as_completed(list(futures_to_task.keys())):
                task_id = futures_to_task.pop(future)
                returned_id, success, error = future.result()

                if not success:
                    raise RuntimeError(f"Task {task_id} failed: {error}")

                # Mark complete
                task_nodes[task_id].completed = True
                completion_cache.add(task_id)

                # Check for more ready tasks
                break


# Alternative: Use shared memory for better performance with large data
def build_with_shared_memory(
    root_task: BaseTask,
    *,
    max_workers: int | None = None,
    completion_cache: set[UUID] | None = None,
    registry: RegistryABC | None = None,
) -> None:
    """Build using multiprocessing with shared memory for completion tracking.

    This version uses multiprocessing.Manager for shared state, which can be
    more efficient for large DAGs with many completion checks.

    NOTE: Still does not support dynamic dependencies.
    """
    registry = registry or registry_provider.get()
    completion_cache = completion_cache or set()

    registry.start_build(root_tasks=[root_task])

    try:
        with mp.Manager() as manager:
            # Shared completion set
            shared_complete = manager.dict()
            for task_id in completion_cache:
                shared_complete[str(task_id)] = True

            _build_with_shared_state(
                root_task,
                max_workers=max_workers,
                shared_complete=shared_complete,
            )

            # Update completion cache from shared state
            for task_id_str in shared_complete.keys():
                completion_cache.add(UUID(task_id_str))

        registry.complete_build()

    except Exception as e:
        registry.fail_build(str(e))
        raise


def _build_with_shared_state(
    root_task: BaseTask,
    *,
    max_workers: int | None,
    shared_complete: dict | mp.managers.DictProxy,  # type: ignore[type-arg]
) -> None:
    """Internal build with shared state."""
    # Similar to build_with_multiprocessing but using shared_complete
    # for cross-process completion checks

    task_nodes: dict[UUID, TaskNode] = {}

    def discover_task(task: BaseTask) -> TaskNode:
        if task.id in task_nodes:
            return task_nodes[task.id]

        if str(task.id) in shared_complete or task.complete():
            shared_complete[str(task.id)] = True
            node = TaskNode(task=task, deps=[], completed=True)
            task_nodes[task.id] = node
            return node

        if task.has_dynamic_deps():
            raise NotImplementedError(
                "Dynamic dependencies not supported in multiprocessing mode"
            )

        deps = flatten_task_struct(task.requires())
        dep_ids = []
        for dep in deps:
            dep_node = discover_task(dep)
            if not dep_node.completed:
                dep_ids.append(dep.id)

        node = TaskNode(task=task, deps=dep_ids)
        task_nodes[task.id] = node
        return node

    discover_task(root_task)

    if task_nodes[root_task.id].completed:
        return

    def get_ready_tasks() -> list[TaskNode]:
        ready = []
        for node in task_nodes.values():
            if node.completed or node.submitted:
                continue
            if all(task_nodes[dep_id].completed for dep_id in node.deps):
                ready.append(node)
        return ready

    max_workers = max_workers or mp.cpu_count()

    with ProcessPoolExecutor(max_workers=max_workers) as executor:
        futures_to_task: dict[Any, UUID] = {}

        while not task_nodes[root_task.id].completed:
            ready = get_ready_tasks()

            for node in ready:
                task = node.task
                task_json = task.model_dump_json()
                task_class_path = (
                    f"{task.__class__.__module__}.{task.__class__.__name__}"
                )

                future = executor.submit(
                    _run_task_in_process, task_json, task_class_path
                )
                futures_to_task[future] = task.id
                node.submitted = True

            if not futures_to_task:
                incomplete = [n for n in task_nodes.values() if not n.completed]
                raise RuntimeError(
                    f"No tasks ready but {len(incomplete)} tasks incomplete"
                )

            for future in as_completed(list(futures_to_task.keys())):
                task_id = futures_to_task.pop(future)
                returned_id, success, error = future.result()

                if not success:
                    raise RuntimeError(f"Task {task_id} failed: {error}")

                task_nodes[task_id].completed = True
                shared_complete[str(task_id)] = True
                break
