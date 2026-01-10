#!/usr/bin/env python3
"""Benchmark runner for concurrent build implementations.

Runs each DAG scenario with each build implementation and measures execution time.

Usage:
    cd lib/stardag
    uv run python -m benchmarks.run_benchmark
"""

from __future__ import annotations

import asyncio
import gc
import json
import os
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from stardag.build.concurrent.asyncio_builder import build as asyncio_build
from stardag.build.concurrent.asyncio_builder import (
    build_queue_based as asyncio_queue_build,
)
from stardag.build.concurrent.multiprocess import build as multiprocess_build
from stardag.build.concurrent.threadpool import build_simple as threadpool_build
from stardag.build.registry import NoOpRegistry
from stardag.target import LocalTarget
from stardag.target._factory import TargetFactory, target_factory_provider


@dataclass
class BenchmarkResult:
    """Result of a single benchmark run."""

    scenario: str
    implementation: str
    workers: int
    duration: float
    success: bool
    error: str | None = None


def run_sync_build(
    build_func: Callable, dag_factory: Callable, workers: int, run_id: str
) -> float:
    """Run a sync build and return duration."""
    dag = dag_factory(prefix=f"{run_id}_")
    registry = NoOpRegistry()

    gc.collect()
    start = time.perf_counter()
    build_func(dag, max_workers=workers, registry=registry)
    duration = time.perf_counter() - start

    return duration


def run_async_build(
    async_build_func: Callable,
    dag_factory: Callable,
    workers: int,
    run_id: str,
    param_name: str = "max_concurrent",
) -> float:
    """Run an async build and return duration."""
    dag = dag_factory(prefix=f"{run_id}_")
    registry = NoOpRegistry()

    gc.collect()
    start = time.perf_counter()
    asyncio.run(async_build_func(dag, **{param_name: workers}, registry=registry))
    duration = time.perf_counter() - start

    return duration


# Build implementations
IMPLEMENTATIONS: dict[str, dict[str, Any]] = {
    "threadpool": {
        "func": threadpool_build,
        "async": False,
    },
    "asyncio": {
        "func": asyncio_build,
        "async": True,
        "param": "max_concurrent",
    },
    "asyncio_queue": {
        "func": asyncio_queue_build,
        "async": True,
        "param": "max_concurrent",
    },
    "multiprocess": {
        "func": multiprocess_build,
        "async": False,
    },
}


def run_benchmark(
    scenario_name: str,
    dag_factory: Callable,
    implementations: list[str] | None = None,
    workers: int = 4,
    warmup_runs: int = 1,
    timed_runs: int = 3,
) -> list[BenchmarkResult]:
    """Run benchmark for a scenario across implementations."""
    results = []
    implementations = implementations or list(IMPLEMENTATIONS.keys())

    for impl_name in implementations:
        impl = IMPLEMENTATIONS[impl_name]

        # Warmup runs
        for i in range(warmup_runs):
            try:
                run_id = f"warmup_{impl_name}_{i}"
                if impl["async"]:
                    run_async_build(
                        impl["func"],
                        dag_factory,
                        workers,
                        run_id,
                        impl.get("param", "max_concurrent"),
                    )
                else:
                    run_sync_build(impl["func"], dag_factory, workers, run_id)
            except Exception as e:
                print(f"  Warmup failed for {impl_name}: {e}")

        # Timed runs
        durations = []
        error = None
        success = True

        for i in range(timed_runs):
            try:
                run_id = f"timed_{impl_name}_{i}"
                if impl["async"]:
                    duration = run_async_build(
                        impl["func"],
                        dag_factory,
                        workers,
                        run_id,
                        impl.get("param", "max_concurrent"),
                    )
                else:
                    duration = run_sync_build(
                        impl["func"], dag_factory, workers, run_id
                    )
                durations.append(duration)
            except Exception as e:
                success = False
                error = str(e)
                break

        avg_duration = sum(durations) / len(durations) if durations else 0

        results.append(
            BenchmarkResult(
                scenario=scenario_name,
                implementation=impl_name,
                workers=workers,
                duration=avg_duration,
                success=success,
                error=error,
            )
        )

        status = f"{avg_duration:.3f}s" if success else f"FAILED: {error}"
        print(f"  {impl_name}: {status}")

    return results


def main():
    """Run all benchmarks."""
    # Import here to avoid circular imports
    from benchmarks.dags import (
        cpu_bound_tree,
        heavy_cpu_flat,
        io_bound_flat,
        io_bound_tree,
        light_tree,
    )

    # Setup temp directory for targets
    with tempfile.TemporaryDirectory() as tmp_dir:
        target_roots = {"default": tmp_dir}

        # Set env var for multiprocessing
        os.environ["STARDAG_TARGET_ROOTS"] = json.dumps(target_roots)

        with target_factory_provider.override(
            TargetFactory(
                target_roots=target_roots,
                prefixt_to_target_prototype={"/": LocalTarget},
            )
        ):
            all_results: list[BenchmarkResult] = []

            # In-process implementations (skip multiprocess for speed)
            in_process_only = ["threadpool", "asyncio", "asyncio_queue"]

            # Core scenarios (reduced set for faster benchmarks)
            scenarios_4workers = [
                ("io_bound_static", io_bound_tree),
                ("cpu_bound_static", cpu_bound_tree),
                ("light_static", light_tree),
            ]

            print("=" * 60)
            print("CONCURRENT BUILD BENCHMARK")
            print("=" * 60)
            print("\nWarmup: 1, Timed runs: 2")
            print()

            print("=" * 60)
            print("SECTION 1: Core scenarios (4 workers, in-process only)")
            print("=" * 60)
            print("Static DAGs: 3-level tree (15 tasks)")
            print(
                "Multiprocess skipped for speed (see heavy_cpu for multiprocess demo)"
            )
            print()

            for scenario_name, dag_factory in scenarios_4workers:
                print(f"\n{scenario_name}:")
                print("-" * 40)
                results = run_benchmark(
                    scenario_name,
                    dag_factory,
                    implementations=in_process_only,
                    workers=4,
                    warmup_runs=1,
                    timed_runs=2,
                )
                all_results.extend(results)

            # Heavy CPU - also in-process only for speed
            # (multiprocess would win here due to true parallelism, but adds ~20s overhead)
            print("\nheavy_cpu_static:")
            print("-" * 40)
            results = run_benchmark(
                "heavy_cpu_static",
                heavy_cpu_flat,
                implementations=in_process_only,
                workers=4,
                warmup_runs=1,
                timed_runs=2,
            )
            all_results.extend(results)

            print()
            print("=" * 60)
            print("SECTION 2: High-concurrency IO scenarios")
            print("=" * 60)
            print("io_flat_32_w16: 33 tasks (32 leaves + root), 16 workers")
            print("io_flat_many_w32: 101 tasks (100 leaves + root), 32 workers")
            print()
            print(
                "Note: With synthetic sleep, asyncio and threadpool perform similarly"
            )
            print("because GIL is released during sleep. Async advantages are more")
            print(
                "visible with real network I/O (connection pooling, no thread overhead)."
            )
            print()

            # High concurrency scenario: 32 leaves, 16 workers
            print("\nio_flat_32_w16:")
            print("-" * 40)
            results = run_benchmark(
                "io_flat_32_w16",
                io_bound_flat,
                implementations=in_process_only,
                workers=16,
                warmup_runs=1,
                timed_runs=2,
            )
            all_results.extend(results)

            # Note: File I/O scenarios (file_io_flat, file_io_heavy) available but
            # skipped for speed. They use target.open_aio() for true async file I/O.

            # Print summary table
            print("\n" + "=" * 60)
            print("SUMMARY (average time in seconds, lower is better)")
            print("=" * 60)

            # Group by scenario
            scenarios_seen = []
            for r in all_results:
                if r.scenario not in scenarios_seen:
                    scenarios_seen.append(r.scenario)

            # Header - only show in-process implementations
            impl_names = in_process_only
            header = f"{'Scenario':<25}" + "".join(f"{name:>15}" for name in impl_names)
            print(header)
            print("-" * len(header))

            for scenario in scenarios_seen:
                row = f"{scenario:<25}"
                for impl in impl_names:
                    result = next(
                        (
                            r
                            for r in all_results
                            if r.scenario == scenario and r.implementation == impl
                        ),
                        None,
                    )
                    if result and result.success:
                        row += f"{result.duration:>15.3f}"
                    else:
                        row += f"{'FAILED':>15}"
                print(row)

            # Save raw results
            results_file = Path(__file__).parent / "results.json"
            with open(results_file, "w") as f:
                json.dump(
                    [
                        {
                            "scenario": r.scenario,
                            "implementation": r.implementation,
                            "workers": r.workers,
                            "duration": r.duration,
                            "success": r.success,
                            "error": r.error,
                        }
                        for r in all_results
                    ],
                    f,
                    indent=2,
                )
            print(f"\nResults saved to: {results_file}")


if __name__ == "__main__":
    main()
