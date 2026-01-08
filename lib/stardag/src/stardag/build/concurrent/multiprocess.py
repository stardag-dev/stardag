"""Multiprocessing-based concurrent build implementation.

This implementation uses Python's multiprocessing for CPU-bound tasks.
It provides true parallelism by running tasks in separate processes.

Key insight for dynamic dependencies:
- Tasks with dynamic deps MUST be idempotent
- When run() yields deps, we stop execution and return the discovered deps
- The deps are picklable (Pydantic models), even if generators aren't
- Next time the task runs, completed deps will be skipped and execution advances
- This continues until StopIteration (task complete)

This implementation is best for:
- CPU-bound task.run() methods
- When you need to bypass the GIL
- Tasks that are idempotent (as all stardag tasks should be)
"""

from __future__ import annotations

import logging
import multiprocessing as mp
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from stardag._task import BaseTask, flatten_task_struct
from stardag.build.registry import RegistryABC, registry_provider

logger = logging.getLogger(__name__)


def _run_task_in_process(
    task_json: str, task_class_path: str
) -> tuple[str, bool, list[tuple[str, str]] | None, str | None]:
    """Run a task in a separate process.

    Args:
        task_json: JSON-serialized task
        task_class_path: Full import path of task class

    Returns:
        (task_id_str, is_complete, dynamic_deps_json_list, error_message)
        - is_complete: True if task finished, False if yielded deps
        - dynamic_deps_json_list: List of JSON-serialized deps if yielded
    """
    import importlib

    try:
        # Reconstruct the task from JSON
        module_path, class_name = task_class_path.rsplit(".", 1)
        module = importlib.import_module(module_path)
        task_class = getattr(module, class_name)

        task = task_class.model_validate_json(task_json)
        task_id_str = str(task.id)

        # Check if already complete
        if task.complete():
            return task_id_str, True, None, None

        # Run the task
        result = task.run()

        if result is None:
            # Normal task completed
            return task_id_str, True, None, None

        # Task has dynamic deps - result is a generator
        # Iterate through yielded deps until we find incomplete ones
        gen = result
        try:
            while True:
                yielded = next(gen)
                deps = flatten_task_struct(yielded)

                # Check if all yielded deps are complete
                incomplete_deps = [dep for dep in deps if not dep.complete()]

                if incomplete_deps:
                    # Return incomplete deps for scheduling
                    deps_json = [
                        (
                            dep.model_dump_json(),
                            f"{dep.__class__.__module__}.{dep.__class__.__name__}",
                        )
                        for dep in incomplete_deps
                    ]
                    return task_id_str, False, deps_json, None

                # All deps complete, continue to next yield
                continue

        except StopIteration:
            # Generator completed - task is done
            return task_id_str, True, None, None

    except Exception as e:
        import traceback

        return "", False, None, f"{e}\n{traceback.format_exc()}"


@dataclass
class TaskNode:
    """Node in the task DAG for scheduling."""

    task: BaseTask
    task_json: str
    task_class_path: str
    static_deps: list[UUID] = field(default_factory=list)
    dynamic_deps: list[UUID] = field(default_factory=list)
    completed: bool = False
    submitted: bool = False

    @property
    def all_deps(self) -> list[UUID]:
        return self.static_deps + self.dynamic_deps


def build(
    task: BaseTask,
    *,
    max_workers: int | None = None,
    completion_cache: set[UUID] | None = None,
    registry: RegistryABC | None = None,
) -> None:
    """Build a task DAG using ProcessPoolExecutor for parallelism.

    Supports dynamic dependencies through idempotent re-execution:
    when a task yields incomplete deps, they are scheduled, and the
    task is re-run from scratch (relying on idempotency).

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
    """Core multiprocessing build logic with dynamic deps support.

    Algorithm:
    1. Build task graph from static requires()
    2. Submit tasks when all known deps complete
    3. If task yields incomplete deps, add them to graph and re-schedule task
    4. Continue until root completes
    """
    # Build task graph
    task_nodes: dict[UUID, TaskNode] = {}

    def add_task(task: BaseTask) -> TaskNode:
        """Add a task to the graph if not already present."""
        if task.id in task_nodes:
            return task_nodes[task.id]

        task_json = task.model_dump_json()
        task_class_path = f"{task.__class__.__module__}.{task.__class__.__name__}"

        # Check if already complete
        if task.id in completion_cache or task.complete():
            completion_cache.add(task.id)
            node = TaskNode(
                task=task,
                task_json=task_json,
                task_class_path=task_class_path,
                completed=True,
            )
            task_nodes[task.id] = node
            return node

        # Get static dependencies
        static_deps = flatten_task_struct(task.requires())
        static_dep_ids = []

        for dep in static_deps:
            dep_node = add_task(dep)
            if not dep_node.completed:
                static_dep_ids.append(dep.id)

        node = TaskNode(
            task=task,
            task_json=task_json,
            task_class_path=task_class_path,
            static_deps=static_dep_ids,
        )
        task_nodes[task.id] = node
        return node

    # Discover initial tasks
    add_task(root_task)

    # Check if root is already complete
    if task_nodes[root_task.id].completed:
        return

    def get_ready_tasks() -> list[TaskNode]:
        """Get tasks that are ready to submit (all deps complete)."""
        ready = []
        for node in task_nodes.values():
            if node.completed or node.submitted:
                continue
            # Check if all deps (static + dynamic) are complete
            all_deps_complete = all(
                task_nodes[dep_id].completed for dep_id in node.all_deps
            )
            if all_deps_complete:
                ready.append(node)
        return ready

    # Execute with process pool
    max_workers = max_workers or mp.cpu_count()

    with ProcessPoolExecutor(max_workers=max_workers) as executor:
        futures_to_node: dict[Any, TaskNode] = {}

        while not task_nodes[root_task.id].completed:
            # Submit ready tasks
            ready = get_ready_tasks()

            for node in ready:
                future = executor.submit(
                    _run_task_in_process,
                    node.task_json,
                    node.task_class_path,
                )
                futures_to_node[future] = node
                node.submitted = True

            if not futures_to_node:
                # No tasks submitted and root not complete - check for issues
                incomplete = [n for n in task_nodes.values() if not n.completed]
                if incomplete:
                    # Check if there's a deadlock
                    raise RuntimeError(
                        f"No tasks ready but {len(incomplete)} tasks incomplete. "
                        f"Possible cycle or missing dependency."
                    )
                break

            # Wait for at least one completion
            for future in as_completed(list(futures_to_node.keys())):
                node = futures_to_node.pop(future)
                task_id_str, is_complete, deps_json, error = future.result()

                if error:
                    raise RuntimeError(f"Task {node.task.id} failed: {error}")

                if is_complete:
                    # Task completed successfully
                    node.completed = True
                    completion_cache.add(node.task.id)
                else:
                    # Task yielded incomplete deps - add them and reschedule
                    node.submitted = False  # Allow resubmission

                    if deps_json:
                        for dep_json, dep_class_path in deps_json:
                            # Reconstruct the dep task
                            import importlib

                            module_path, class_name = dep_class_path.rsplit(".", 1)
                            module = importlib.import_module(module_path)
                            dep_class = getattr(module, class_name)
                            dep_task = dep_class.model_validate_json(dep_json)

                            # Add to graph if not present
                            add_task(dep_task)

                            # Add as dynamic dep if not already tracked
                            if dep_task.id not in node.dynamic_deps:
                                node.dynamic_deps.append(dep_task.id)

                # Process one at a time to check for newly ready tasks
                break
