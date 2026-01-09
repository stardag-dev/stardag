"""DAG factory functions for benchmarking.

Creates parameterized DAG structures for testing different scenarios.
"""

from __future__ import annotations

from typing import Type

from benchmarks.tasks import (
    BenchmarkTask,
    CPUBoundDynamicTask,
    CPUBoundTask,
    HeavyCPUBoundTask,
    IOBoundDynamicTask,
    IOBoundTask,
    LightDynamicTask,
    LightTask,
)


def create_tree_dag(
    task_class: Type[BenchmarkTask],
    prefix: str = "",
    leaf_count: int = 8,
    **task_kwargs,
) -> BenchmarkTask:
    """Create a 3-level tree DAG.

    Structure (leaf_count=8):
        - 8 leaf tasks (level 0, no deps)
        - 4 middle tasks (level 1, each depends on 2 leaves)
        - 2 intermediate tasks (level 2, each depends on 2 middle)
        - 1 root task (level 3, depends on 2 intermediate)

    Total: 15 tasks
    """
    # Level 0: Leaf tasks
    leaves = [
        task_class(task_id=f"{prefix}leaf_{i}", **task_kwargs)
        for i in range(leaf_count)
    ]

    # Level 1: Middle tasks (each depends on 2 leaves)
    middle_count = leaf_count // 2
    middle = [
        task_class(
            task_id=f"{prefix}middle_{i}",
            deps=(leaves[i * 2], leaves[i * 2 + 1]),
            **task_kwargs,
        )
        for i in range(middle_count)
    ]

    # Level 2: Intermediate tasks (each depends on 2 middle)
    inter_count = middle_count // 2
    intermediate = [
        task_class(
            task_id=f"{prefix}inter_{i}",
            deps=(middle[i * 2], middle[i * 2 + 1]),
            **task_kwargs,
        )
        for i in range(inter_count)
    ]

    # Level 3: Root task
    root = task_class(
        task_id=f"{prefix}root",
        deps=tuple(intermediate),
        **task_kwargs,
    )

    return root


def create_dynamic_flat_dag(
    workload: str = "io",
    prefix: str = "",
    leaf_count: int = 8,
    **task_kwargs,
) -> IOBoundDynamicTask | CPUBoundDynamicTask | LightDynamicTask:
    """Create a flat DAG where root dynamically discovers all leaf tasks.

    This is a simpler structure to test dynamic deps:
    - 1 root task that yields N leaf task IDs at runtime
    - N leaf tasks (discovered dynamically, no deps of their own)

    Total: N+1 tasks (all leaves can run in parallel once discovered)
    """
    # Select dynamic task class based on workload
    if workload == "io":
        dynamic_class = IOBoundDynamicTask
    elif workload == "cpu":
        dynamic_class = CPUBoundDynamicTask
    else:  # light
        dynamic_class = LightDynamicTask

    # Leaf task IDs (will be discovered dynamically)
    leaf_ids = tuple(f"{prefix}leaf_{i}" for i in range(leaf_count))

    # Root task with dynamic deps to all leaves
    root = dynamic_class(
        task_id=f"{prefix}root",
        dynamic_dep_ids=leaf_ids,
        **task_kwargs,
    )

    return root


# Pre-configured DAG factories for each scenario
def io_bound_tree(prefix: str = "") -> BenchmarkTask:
    """IO-bound tree DAG (8 leaves, 100ms sleep each)."""
    return create_tree_dag(IOBoundTask, prefix=prefix, sleep_duration=0.1)


def cpu_bound_tree(prefix: str = "") -> BenchmarkTask:
    """CPU-bound tree DAG (8 leaves, 100k hash iterations each)."""
    return create_tree_dag(CPUBoundTask, prefix=prefix, iterations=100_000)


def light_tree(prefix: str = "") -> BenchmarkTask:
    """Light tree DAG (8 leaves, minimal work)."""
    return create_tree_dag(LightTask, prefix=prefix)


def io_bound_dynamic(prefix: str = ""):
    """IO-bound flat DAG with dynamic deps (8 leaves discovered at runtime)."""
    return create_dynamic_flat_dag("io", prefix=prefix, sleep_duration=0.1)


def cpu_bound_dynamic(prefix: str = ""):
    """CPU-bound flat DAG with dynamic deps (8 leaves discovered at runtime)."""
    return create_dynamic_flat_dag("cpu", prefix=prefix, iterations=100_000)


def light_dynamic(prefix: str = ""):
    """Light flat DAG with dynamic deps (8 leaves discovered at runtime)."""
    return create_dynamic_flat_dag("light", prefix=prefix)


def heavy_cpu_flat(prefix: str = "") -> BenchmarkTask:
    """Heavy CPU-bound flat DAG (4 leaves, ~1s each).

    Uses fewer tasks to keep benchmark time reasonable.
    This scenario demonstrates multiprocessing's advantage for CPU-heavy work.
    """
    # Create flat structure: root depends on 4 heavy leaves
    leaves = [HeavyCPUBoundTask(task_id=f"{prefix}leaf_{i}") for i in range(4)]
    root = HeavyCPUBoundTask(
        task_id=f"{prefix}root",
        deps=tuple(leaves),
    )
    return root


def io_bound_flat(prefix: str = "", leaf_count: int = 32) -> BenchmarkTask:
    """Flat IO-bound DAG optimized for async execution.

    This scenario demonstrates asyncio's advantage:
    - Many independent IO tasks that can run truly concurrently
    - No thread pool overhead with async sleep
    - Single root depends on all leaves

    With max_concurrent=4:
    - Threadpool: 32 tasks / 4 threads = 8 batches * 0.1s = 0.8s minimum
    - Asyncio: Can overlap all sleeps within semaphore limit, ~0.8s minimum

    The advantage shows when we increase max_concurrent or have many quick I/O ops.
    """
    leaves = [
        IOBoundTask(task_id=f"{prefix}leaf_{i}", sleep_duration=0.1)
        for i in range(leaf_count)
    ]
    root = IOBoundTask(
        task_id=f"{prefix}root",
        deps=tuple(leaves),
        sleep_duration=0.1,
    )
    return root


def io_bound_flat_many(prefix: str = "") -> BenchmarkTask:
    """Flat IO-bound DAG with many quick tasks.

    This scenario shows asyncio's advantage with many short I/O operations:
    - 100 leaf tasks with 10ms sleep each
    - Single root depends on all leaves

    With max_concurrent=None (unlimited):
    - Asyncio: Can run all 100 tasks concurrently, ~0.01s + overhead
    - Threadpool: Limited by thread creation/management overhead
    """
    leaves = [
        IOBoundTask(task_id=f"{prefix}leaf_{i}", sleep_duration=0.01)
        for i in range(100)
    ]
    root = IOBoundTask(
        task_id=f"{prefix}root",
        deps=tuple(leaves),
        sleep_duration=0.01,
    )
    return root
