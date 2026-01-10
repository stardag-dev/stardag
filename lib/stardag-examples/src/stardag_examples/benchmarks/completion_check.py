#!/usr/bin/env python3
"""Benchmark for completion check overhead.

Tests how long it takes to check completion of many pre-completed tasks.
Simulates S3 HEAD request latency (~50ms) to expose sequential vs parallel
completion checking.

Usage:
    cd lib/stardag-examples
    uv run python -m stardag_examples.benchmarks.completion_check
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import stardag as sd
from stardag._task import BaseTask
from stardag.build._v2 import (
    DefaultTaskRunner,
    build,
    build_sequential,
)
from stardag.build.registry import NoOpRegistry

sd.auto_namespace(__name__)

# Simulated S3 HEAD request latency
S3_HEAD_LATENCY_MS = 50

# Global set to track which tasks are "complete"
_completed_tasks: set[str] = set()


class SlowCompleteTask(BaseTask):
    """Task with slow completion check (simulates S3 HEAD latency)."""

    task_id: str
    deps: tuple["SlowCompleteTask", ...] = ()

    def requires(self) -> sd.TaskStruct:
        return self.deps

    def complete(self) -> bool:
        time.sleep(S3_HEAD_LATENCY_MS / 1000)
        return self.task_id in _completed_tasks

    async def complete_aio(self) -> bool:
        await asyncio.sleep(S3_HEAD_LATENCY_MS / 1000)
        return self.task_id in _completed_tasks

    def run(self) -> None:
        _completed_tasks.add(self.task_id)


def create_flat_dag(leaf_count: int = 100) -> SlowCompleteTask:
    """Create flat DAG: 1 root with N leaf dependencies."""
    leaves = [SlowCompleteTask(task_id=f"leaf_{i}") for i in range(leaf_count)]
    root = SlowCompleteTask(task_id="root", deps=tuple(leaves))
    return root


def pre_complete_tasks(root: SlowCompleteTask) -> None:
    """Mark all tasks as complete."""
    tasks_to_complete = [root]
    seen: set[str] = set()
    while tasks_to_complete:
        task = tasks_to_complete.pop()
        if task.task_id in seen:
            continue
        seen.add(task.task_id)
        _completed_tasks.add(task.task_id)
        tasks_to_complete.extend(task.deps)


@dataclass
class BenchmarkResult:
    config: str
    duration: float
    task_count: int
    latency_ms: int


def run_sequential_benchmark(leaf_count: int) -> BenchmarkResult:
    """Run sequential build and measure time."""
    _completed_tasks.clear()
    root = create_flat_dag(leaf_count)
    pre_complete_tasks(root)

    start = time.perf_counter()
    build_sequential([root], registry=NoOpRegistry())
    duration = time.perf_counter() - start

    return BenchmarkResult(
        config="sequential",
        duration=duration,
        task_count=leaf_count + 1,
        latency_ms=S3_HEAD_LATENCY_MS,
    )


async def run_concurrent_benchmark(leaf_count: int) -> BenchmarkResult:
    """Run concurrent build and measure time."""
    _completed_tasks.clear()
    root = create_flat_dag(leaf_count)
    pre_complete_tasks(root)

    task_runner = DefaultTaskRunner(registry=NoOpRegistry())

    start = time.perf_counter()
    await build([root], task_runner=task_runner)
    duration = time.perf_counter() - start

    return BenchmarkResult(
        config="concurrent",
        duration=duration,
        task_count=leaf_count + 1,
        latency_ms=S3_HEAD_LATENCY_MS,
    )


def main() -> None:
    """Run completion check benchmarks."""
    leaf_count = 100

    print("=" * 70)
    print("COMPLETION CHECK BENCHMARK")
    print("=" * 70)
    print(f"\nScenario: 1 root task + {leaf_count} leaf dependencies")
    print("All tasks pre-completed (simulating cached results)")
    print(f"Simulated S3 HEAD latency: {S3_HEAD_LATENCY_MS}ms per check")
    print()
    print(f"Expected sequential time: {(leaf_count + 1) * S3_HEAD_LATENCY_MS}ms")
    print(f"Expected parallel time: ~{S3_HEAD_LATENCY_MS}ms (if using asyncio.gather)")
    print()

    # Run sequential
    print("Running sequential build...")
    seq_result = run_sequential_benchmark(leaf_count)
    print(f"  Sequential: {seq_result.duration:.3f}s")

    # Run concurrent
    print("Running concurrent build...")
    conc_result = asyncio.run(run_concurrent_benchmark(leaf_count))
    print(f"  Concurrent: {conc_result.duration:.3f}s")

    # Analysis
    print()
    print("=" * 70)
    print("ANALYSIS")
    print("=" * 70)
    expected_sequential = (leaf_count + 1) * S3_HEAD_LATENCY_MS / 1000
    print(f"Expected sequential (if checking one-by-one): {expected_sequential:.3f}s")
    print(f"Actual sequential: {seq_result.duration:.3f}s")
    print(f"Actual concurrent: {conc_result.duration:.3f}s")
    print()

    if conc_result.duration > expected_sequential * 0.5:
        print("ISSUE DETECTED: Concurrent build is checking completion sequentially!")
        print("The concurrent build should use asyncio.gather() for parallel checks.")
        speedup_potential = seq_result.duration / (S3_HEAD_LATENCY_MS / 1000)
        print(f"Potential speedup with asyncio.gather: ~{speedup_potential:.0f}x")
    else:
        speedup = seq_result.duration / conc_result.duration
        print(f"Concurrent speedup: {speedup:.1f}x")


if __name__ == "__main__":
    main()
