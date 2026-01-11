#!/usr/bin/env python3
"""Benchmark runner for concurrent build implementations.

Runs each DAG scenario with different build configurations and measures execution time.

Usage:
    cd lib/stardag
    uv run python -m benchmarks.run_benchmark
"""

from __future__ import annotations

import gc
import json
import os
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from stardag.build import (
    DefaultExecutionModeSelector,
    HybridConcurrentTaskRunner,
    build,
    build_sequential,
)
from stardag.registry import NoOpRegistry
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


def run_build(
    dag_factory: Callable,
    run_id: str,
    mode: Literal["sequential", "thread", "process"] = "thread",
    workers: int = 4,
) -> float:
    """Run a build and return duration."""
    dag = dag_factory(prefix=f"{run_id}_")
    registry = NoOpRegistry()

    gc.collect()
    start = time.perf_counter()

    if mode == "sequential":
        build_sequential([dag], registry=registry)
    else:
        # Use HybridConcurrentTaskRunner with configured execution mode
        sync_run_default: Literal["thread", "process"] = mode  # type: ignore
        runner = HybridConcurrentTaskRunner(
            registry=registry,
            execution_mode_selector=DefaultExecutionModeSelector(
                sync_run_default=sync_run_default
            ),
            max_thread_workers=workers,
            max_process_workers=workers,
        )
        build([dag], task_runner=runner)

    duration = time.perf_counter() - start
    return duration


# Build configurations
IMPLEMENTATIONS: dict[str, dict[str, Any]] = {
    "sequential": {
        "mode": "sequential",
        "workers": 1,
    },
    "thread_pool": {
        "mode": "thread",
        "workers": 4,
    },
    "process_pool": {
        "mode": "process",
        "workers": 4,
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
        impl_workers = impl.get("workers", workers)

        # Warmup runs
        for i in range(warmup_runs):
            try:
                run_id = f"warmup_{impl_name}_{i}"
                run_build(
                    dag_factory,
                    run_id,
                    mode=impl["mode"],
                    workers=impl_workers,
                )
            except Exception as e:
                print(f"  Warmup failed for {impl_name}: {e}")

        # Timed runs
        durations = []
        error = None
        success = True

        for i in range(timed_runs):
            try:
                run_id = f"timed_{impl_name}_{i}"
                duration = run_build(
                    dag_factory,
                    run_id,
                    mode=impl["mode"],
                    workers=impl_workers,
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
                workers=impl_workers,
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

            # In-process implementations (skip process_pool for speed)
            in_process_only = ["sequential", "thread_pool"]

            # Core scenarios
            scenarios = [
                ("io_bound_tree", io_bound_tree),
                ("cpu_bound_tree", cpu_bound_tree),
                ("light_tree", light_tree),
            ]

            print("=" * 60)
            print("STARDAG BUILD BENCHMARK")
            print("=" * 60)
            print("\nWarmup: 1, Timed runs: 2")
            print()

            print("=" * 60)
            print("SECTION 1: Core scenarios (in-process only)")
            print("=" * 60)
            print("Static DAGs: 3-level tree (15 tasks)")
            print(
                "Process pool skipped for speed (see heavy_cpu for process pool demo)"
            )
            print()

            for scenario_name, dag_factory in scenarios:
                print(f"\n{scenario_name}:")
                print("-" * 40)
                results = run_benchmark(
                    scenario_name,
                    dag_factory,
                    implementations=in_process_only,
                    warmup_runs=1,
                    timed_runs=2,
                )
                all_results.extend(results)

            # Heavy CPU - include process pool
            print("\nheavy_cpu_flat:")
            print("-" * 40)
            results = run_benchmark(
                "heavy_cpu_flat",
                heavy_cpu_flat,
                implementations=["sequential", "thread_pool", "process_pool"],
                warmup_runs=1,
                timed_runs=2,
            )
            all_results.extend(results)

            print()
            print("=" * 60)
            print("SECTION 2: High-concurrency IO scenarios")
            print("=" * 60)
            print("io_flat: 33 tasks (32 leaves + root)")
            print()

            # High concurrency scenario
            print("\nio_flat:")
            print("-" * 40)
            results = run_benchmark(
                "io_flat",
                io_bound_flat,
                implementations=in_process_only,
                warmup_runs=1,
                timed_runs=2,
            )
            all_results.extend(results)

            # Print summary table
            print("\n" + "=" * 60)
            print("SUMMARY (average time in seconds, lower is better)")
            print("=" * 60)

            # Group by scenario
            scenarios_seen = []
            for r in all_results:
                if r.scenario not in scenarios_seen:
                    scenarios_seen.append(r.scenario)

            # Header
            impl_names = ["sequential", "thread_pool", "process_pool"]
            header = f"{'Scenario':<20}" + "".join(f"{name:>15}" for name in impl_names)
            print(header)
            print("-" * len(header))

            for scenario in scenarios_seen:
                row = f"{scenario:<20}"
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
                    elif result:
                        row += f"{'FAILED':>15}"
                    else:
                        row += f"{'-':>15}"
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
