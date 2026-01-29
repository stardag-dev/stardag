#!/usr/bin/env python3
"""Benchmark runner for build configurations.

Runs each DAG scenario with different build configurations and measures execution time.

Key configurations compared:
- sync_run_default: "thread" vs "process" for CPU-bound sync tasks
- max_async_workers: concurrency level for async tasks
- Sequential vs concurrent build

Usage:
    cd lib/stardag-examples

    # With NoOp registry (default, fastest - no network overhead):
    uv run python -m stardag_examples.benchmarks.run_benchmark

    # With local registry (docker-compose at localhost:8000):
    uv run python -m stardag_examples.benchmarks.run_benchmark --registry local

    # With remote registry (uses configured API URL from env/config):
    uv run python -m stardag_examples.benchmarks.run_benchmark --registry remote
"""

from __future__ import annotations

import argparse
import asyncio
import gc
import json
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal

from stardag.build import (
    DefaultExecutionModeSelector,
    GlobalLockConfig,
    HybridConcurrentTaskExecutor,
    build_aio,
    build_sequential,
)
from stardag.registry import (
    APIRegistry,
    NoOpRegistry,
    RegistryABC,
    RegistryGlobalConcurrencyLockManager,
    init_registry,
)
from stardag.target import InMemoryFileSystemTarget
from stardag.target._factory import TargetFactory, target_factory_provider

# Registry mode type
RegistryMode = Literal["noop", "local", "remote"]


@dataclass
class BenchmarkResult:
    """Result of a single benchmark run."""

    scenario: str
    config: str
    duration: float
    success: bool
    error: str | None = None


def clear_targets(target_root: str) -> None:
    """Clear all target files to ensure fresh run."""
    root_path = Path(target_root)
    if root_path.exists():
        shutil.rmtree(root_path)
    root_path.mkdir(parents=True, exist_ok=True)


def create_registry(mode: RegistryMode, api_key: str | None = None) -> RegistryABC:
    """Create a registry based on the mode.

    Args:
        mode: Registry mode - "noop", "local", or "remote"
        api_key: Optional API key for authentication

    Returns:
        RegistryABC instance
    """
    if mode == "noop":
        return NoOpRegistry()
    elif mode == "local":
        # Local docker-compose registry at localhost:8000
        # Use APIRegistry with local URL, but allow it to pick up auth from config
        # (api_key from env, or access_token from browser login)
        return APIRegistry(api_url="http://localhost:8000", api_key=api_key)
    elif mode == "remote":
        # Remote registry - uses configured API URL from env/config
        # API key from env (STARDAG_API_KEY) or config is used automatically
        return init_registry()
    else:
        raise ValueError(f"Unknown registry mode: {mode}")


async def run_concurrent_build(
    dag_factory: Callable,
    run_id: str,
    registry: RegistryABC,
    use_global_lock: bool = False,
    sync_run_default: Literal["thread", "process", "blocking"] = "thread",
    max_async_workers: int = 10,
    max_thread_workers: int = 10,
    max_process_workers: int | None = None,
) -> float:
    """Run a concurrent build and return duration."""
    dag = dag_factory(prefix=f"{run_id}_")

    task_executor = HybridConcurrentTaskExecutor(
        execution_mode_selector=DefaultExecutionModeSelector(
            sync_run_default=sync_run_default
        ),
        max_async_workers=max_async_workers,
        max_thread_workers=max_thread_workers,
        max_process_workers=max_process_workers,
    )

    # Setup global lock if using real registry
    global_lock_manager = None
    global_lock_config = None
    if use_global_lock:
        global_lock_manager = RegistryGlobalConcurrencyLockManager()
        global_lock_config = GlobalLockConfig(enabled=True)

    gc.collect()
    start = time.perf_counter()
    await build_aio(
        [dag],
        task_executor=task_executor,
        registry=registry,
        global_lock_manager=global_lock_manager,
        global_lock_config=global_lock_config,
    )
    duration = time.perf_counter() - start

    return duration


def run_sequential_build(
    dag_factory: Callable,
    run_id: str,
    registry: RegistryABC,
) -> float:
    """Run a sequential build and return duration.

    Note: Sequential build doesn't use global locks since it's single-threaded.
    """
    dag = dag_factory(prefix=f"{run_id}_")

    gc.collect()
    start = time.perf_counter()
    build_sequential([dag], registry=registry)
    duration = time.perf_counter() - start

    return duration


# Build configurations to compare
CONFIGS: dict[str, dict[str, Any]] = {
    "sequential": {
        "type": "sequential",
    },
    "concurrent_thread": {
        "type": "concurrent",
        "sync_run_default": "thread",
        "max_async_workers": 10,
        "max_thread_workers": 10,
    },
    "concurrent_process": {
        "type": "concurrent",
        "sync_run_default": "process",
        "max_async_workers": 10,
        "max_thread_workers": 10,
        "max_process_workers": 4,
    },
    "concurrent_blocking": {
        "type": "concurrent",
        "sync_run_default": "blocking",
        "max_async_workers": 10,
    },
}


def run_benchmark(
    scenario_name: str,
    dag_factory: Callable,
    registry_mode: RegistryMode = "noop",
    use_global_lock: bool = False,
    api_key: str | None = None,
    configs: list[str] | None = None,
    timed_runs: int = 3,
    target_root: str = "/tmp/stardag_benchmark",
) -> list[BenchmarkResult]:
    """Run benchmark for a scenario across configurations.

    Note: No warmup runs - targets are cleared between each run to ensure
    fresh execution (cached results would invalidate timing).

    Args:
        scenario_name: Name of the benchmark scenario
        dag_factory: Factory function that creates a DAG
        registry_mode: Registry mode - "noop", "local", or "remote"
        use_global_lock: Whether to use global concurrency locks (for real registries)
        api_key: Optional API key for registry authentication
        configs: List of config names to run (default: all)
        timed_runs: Number of runs per configuration
        target_root: Target root directory
    """
    results = []
    configs = configs or list(CONFIGS.keys())

    for config_name in configs:
        config = CONFIGS[config_name]

        durations = []
        error = None
        success = True

        for i in range(timed_runs):
            try:
                # Clear targets before each run to ensure fresh execution
                clear_targets(target_root)

                # Create fresh registry for each run
                registry = create_registry(registry_mode, api_key=api_key)

                # Generate unique run_id to avoid registry conflicts
                # (registry may have completion records from previous runs)
                unique_id = uuid.uuid4().hex[:8]
                run_id = f"{config_name}_{i}_{unique_id}"

                if config["type"] == "sequential":
                    duration = run_sequential_build(dag_factory, run_id, registry)
                else:
                    duration = asyncio.run(
                        run_concurrent_build(
                            dag_factory,
                            run_id,
                            registry,
                            use_global_lock=use_global_lock,
                            sync_run_default=config.get("sync_run_default", "thread"),
                            max_async_workers=config.get("max_async_workers", 10),
                            max_thread_workers=config.get("max_thread_workers", 10),
                            max_process_workers=config.get("max_process_workers"),
                        )
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
                config=config_name,
                duration=avg_duration,
                success=success,
                error=error,
            )
        )

        status = f"{avg_duration:.3f}s" if success else f"FAILED: {error}"
        print(f"  {config_name}: {status}")

    return results


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    import os

    parser = argparse.ArgumentParser(
        description="Benchmark runner for build configurations",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Run with NoOp registry (fastest, no network overhead):
  uv run python -m stardag_examples.benchmarks.run_benchmark

  # Run with local registry (docker-compose at localhost:8000):
  STARDAG_API_KEY=<key> uv run python -m stardag_examples.benchmarks.run_benchmark --registry local

  # Run with local registry and global locks enabled:
  STARDAG_API_KEY=<key> uv run python -m stardag_examples.benchmarks.run_benchmark --registry local --lock

  # Run with remote registry (uses STARDAG_REGISTRY_URL env var):
  uv run python -m stardag_examples.benchmarks.run_benchmark --registry remote

Authentication:
  For local/remote registries, set STARDAG_API_KEY environment variable.
  For local dev, you can generate an API key via the stardag-ui or API.
""",
    )
    parser.add_argument(
        "--registry",
        type=str,
        choices=["noop", "local", "remote"],
        default="noop",
        help="Registry mode: noop (default, no network), local (docker-compose), remote (configured API)",
    )
    parser.add_argument(
        "--lock",
        action="store_true",
        help="Enable global concurrency locks (requires real registry: local or remote)",
    )
    parser.add_argument(
        "--quick",
        action="store_true",
        help="Run quick benchmark (fewer scenarios, fewer runs)",
    )

    args = parser.parse_args()
    # Get API key from environment
    args.api_key = os.environ.get("STARDAG_API_KEY")
    return args


def main():
    """Run all benchmarks."""
    args = parse_args()
    registry_mode: RegistryMode = args.registry
    use_global_lock: bool = args.lock
    api_key: str | None = args.api_key

    # Validate lock option requires real registry
    if use_global_lock and registry_mode == "noop":
        print("ERROR: --lock requires --registry local or --registry remote")
        return

    # Warn if using real registry without API key
    if registry_mode in ("local", "remote") and not api_key:
        print("WARNING: Using real registry without STARDAG_API_KEY.")
        print("         Set STARDAG_API_KEY environment variable for authentication.")
        print()

    from stardag_examples.benchmarks.dags import (
        cpu_bound_tree,
        heavy_cpu_flat,
        io_bound_flat,
        io_bound_tree,
        light_tree,
    )

    # Use in-memory targets for consistent benchmarks
    target_root = "/tmp/stardag_benchmark"

    with target_factory_provider.override(
        TargetFactory(
            target_roots={"default": target_root},
            prefix_to_target_prototype={"/": InMemoryFileSystemTarget},
        )
    ):
        all_results: list[BenchmarkResult] = []

        # Configurations to run for each section
        io_configs = ["sequential", "concurrent_thread", "concurrent_blocking"]
        cpu_configs = ["sequential", "concurrent_thread", "concurrent_process"]

        # Adjust runs for quick mode
        io_runs = 1 if args.quick else 3
        cpu_runs = 1 if args.quick else 3
        heavy_cpu_runs = 1 if args.quick else 2

        print("=" * 70)
        print("BUILD CONFIGURATION BENCHMARK")
        print("=" * 70)
        print(f"\nRegistry mode: {registry_mode}")
        print(f"Global locks: {'enabled' if use_global_lock else 'disabled'}")
        print(f"Timed runs: {io_runs} (targets cleared between each run)")
        print()

        # Section 1: IO-bound scenarios
        print("=" * 70)
        print("SECTION 1: IO-bound scenarios")
        print("=" * 70)
        print("These scenarios benefit from concurrent async execution.")
        print("Thread pool also works well since GIL is released during sleep.")
        print()

        print("\nio_bound_tree (15 tasks, 0.1s sleep each):")
        print("-" * 50)
        results = run_benchmark(
            "io_bound_tree",
            io_bound_tree,
            registry_mode=registry_mode,
            use_global_lock=use_global_lock,
            api_key=api_key,
            configs=io_configs,
            timed_runs=io_runs,
            target_root=target_root,
        )
        all_results.extend(results)

        if not args.quick:
            print("\nio_bound_flat_64 (65 tasks, 0.1s sleep each):")
            print("-" * 50)
            results = run_benchmark(
                "io_bound_flat_64",
                lambda prefix: io_bound_flat(prefix, leaf_count=64),
                registry_mode=registry_mode,
                use_global_lock=use_global_lock,
                api_key=api_key,
                configs=io_configs,
                timed_runs=io_runs,
                target_root=target_root,
            )
            all_results.extend(results)

        # Section 2: CPU-bound scenarios
        print()
        print("=" * 70)
        print("SECTION 2: CPU-bound scenarios")
        print("=" * 70)
        print("Thread pool is limited by GIL - no true parallelism.")
        print("Process pool can achieve true parallelism but has spawn overhead.")
        print()

        print("\ncpu_bound_tree (15 tasks, 100k hash iterations each):")
        print("-" * 50)
        results = run_benchmark(
            "cpu_bound_tree",
            cpu_bound_tree,
            registry_mode=registry_mode,
            use_global_lock=use_global_lock,
            api_key=api_key,
            configs=cpu_configs,
            timed_runs=cpu_runs,
            target_root=target_root,
        )
        all_results.extend(results)

        if not args.quick:
            print("\nheavy_cpu_flat (9 tasks, ~1s CPU work each):")
            print("-" * 50)
            results = run_benchmark(
                "heavy_cpu_flat",
                lambda prefix: heavy_cpu_flat(prefix, leaf_count=8),
                registry_mode=registry_mode,
                use_global_lock=use_global_lock,
                api_key=api_key,
                configs=cpu_configs,
                timed_runs=heavy_cpu_runs,
                target_root=target_root,
            )
            all_results.extend(results)

        # Section 3: Light/overhead scenarios
        print()
        print("=" * 70)
        print("SECTION 3: Light workload (overhead measurement)")
        print("=" * 70)
        print("Minimal task work - exposes scheduling/coordination overhead.")
        print()

        print("\nlight_tree (15 tasks, trivial work):")
        print("-" * 50)
        results = run_benchmark(
            "light_tree",
            light_tree,
            registry_mode=registry_mode,
            use_global_lock=use_global_lock,
            api_key=api_key,
            configs=["sequential", "concurrent_thread", "concurrent_blocking"],
            timed_runs=io_runs,
            target_root=target_root,
        )
        all_results.extend(results)

        # Print summary table
        print("\n" + "=" * 70)
        print("SUMMARY (average time in seconds, lower is better)")
        print("=" * 70)

        # Group by scenario
        scenarios_seen = []
        for r in all_results:
            if r.scenario not in scenarios_seen:
                scenarios_seen.append(r.scenario)

        # Get all unique configs
        all_configs = []
        for r in all_results:
            if r.config not in all_configs:
                all_configs.append(r.config)

        # Header
        header = f"{'Scenario':<20}" + "".join(f"{name:>18}" for name in all_configs)
        print(header)
        print("-" * len(header))

        for scenario in scenarios_seen:
            row = f"{scenario:<20}"
            for config in all_configs:
                result = next(
                    (
                        r
                        for r in all_results
                        if r.scenario == scenario and r.config == config
                    ),
                    None,
                )
                if result and result.success:
                    row += f"{result.duration:>18.3f}"
                elif result:
                    row += f"{'FAILED':>18}"
                else:
                    row += f"{'-':>18}"
            print(row)

        # Key insights
        print()
        print("=" * 70)
        print("KEY INSIGHTS")
        print("=" * 70)
        print("""
1. IO-bound tasks: Concurrent execution (thread or async) provides
   significant speedup. Both perform similarly with synthetic sleep
   since GIL is released during sleep.

2. CPU-bound tasks: Thread pool provides no speedup due to GIL.
   Process pool achieves true parallelism but has spawn overhead.
   For heavy CPU work (~1s+ per task), process pool wins.

3. Light tasks: Exposes scheduling overhead. Sequential is often
   fastest for trivial work where coordination cost > work cost.

4. sync_run_default="blocking" runs sync tasks on the main loop
   (via to_thread). Good for tasks that do brief sync work.
""")

        # Save raw results
        results_file = Path(__file__).parent / "results.json"
        with open(results_file, "w") as f:
            json.dump(
                [
                    {
                        "scenario": r.scenario,
                        "config": r.config,
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
