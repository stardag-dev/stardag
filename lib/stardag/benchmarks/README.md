# Build Benchmarks

This directory contains benchmarks comparing build configurations for stardag task DAGs.

## Build Configurations

| Configuration    | Description                                                  |
| ---------------- | ------------------------------------------------------------ |
| **sequential**   | Single-threaded sequential execution (for debugging)         |
| **thread_pool**  | Uses `ThreadPoolExecutor` for concurrent sync task execution |
| **process_pool** | Uses `ProcessPoolExecutor` for true parallelism (GIL bypass) |

## Async Task Support

Tasks can implement an optional `run_aio()` method for true async execution:

```python
class IOBoundTask(AutoTask[dict]):
    sleep_duration: float = 0.1

    def run(self) -> None:
        time.sleep(self.sleep_duration)  # Blocks thread
        self.output().save({"task_id": self.task_id})

    async def run_aio(self) -> None:
        """True async I/O - no thread blocking."""
        await asyncio.sleep(self.sleep_duration)
        self.output().save({"task_id": self.task_id})
```

The build system automatically detects `run_aio()` and routes async tasks to the main event loop.

## Test Scenarios

### Workload Types

| Workload      | Description                  | Characteristics                                    |
| ------------- | ---------------------------- | -------------------------------------------------- |
| **IO-bound**  | `asyncio.sleep(0.1)`         | True async sleep. Releases GIL during sleep.       |
| **CPU-bound** | 100k SHA-256 hash iterations | Light computation (~20ms). GIL limits parallelism. |
| **Heavy CPU** | 5M SHA-256 hash iterations   | Heavy computation (~1s). Process pool wins here.   |
| **Light**     | `sum(range(100))`            | Near-zero work. Exposes scheduling overhead.       |

### DAG Structures

**Static DAGs (15 tasks):**

```
        root
       /    \
    inter0  inter1
    /  \    /  \
   m0  m1  m2  m3
  /\   /\  /\   /\
 L0 L1 ... ... L6 L7
```

- 8 leaf tasks (level 0)
- 4 middle tasks (level 1, each depends on 2 leaves)
- 2 intermediate tasks (level 2, each depends on 2 middle)
- 1 root task (level 3, depends on 2 intermediate)

**Flat High-Concurrency DAG (33 tasks):**

```
           root
  / / / | | | | \ \ \ \
 L0 L1 L2 ......... L30 L31
```

- 32 leaf tasks that can all run in parallel
- 1 root task that depends on all leaves
- Tests high-concurrency scenarios

**Heavy CPU DAG (5 tasks):**

```
      root (~1s)
    / | | \
   L0 L1 L2 L3
  (~1s each)
```

- 4 leaf tasks that can run in parallel
- 1 root task that depends on all leaves
- Uses fewer tasks to keep benchmark time reasonable

## Running the Benchmark

```bash
cd lib/stardag
uv run python -m benchmarks.run_benchmark
```

The benchmark takes approximately 30-60 seconds and saves results to `results.json`.

## Configuration Options

Build configurations are controlled via `HybridConcurrentTaskRunner`:

```python
from stardag.build import (
    build,
    HybridConcurrentTaskRunner,
    DefaultExecutionModeSelector,
)
from stardag.registry import NoOpRegistry

# Thread pool (default for sync tasks)
runner = HybridConcurrentTaskRunner(
    registry=NoOpRegistry(),
    execution_mode_selector=DefaultExecutionModeSelector(sync_run_default="thread"),
    max_thread_workers=4,
)
build([dag], task_runner=runner)

# Process pool (true parallelism for CPU-bound tasks)
runner = HybridConcurrentTaskRunner(
    registry=NoOpRegistry(),
    execution_mode_selector=DefaultExecutionModeSelector(sync_run_default="process"),
    max_process_workers=4,
)
build([dag], task_runner=runner)
```

## Recommendations

| Use Case                                 | Recommended Configuration                    |
| ---------------------------------------- | -------------------------------------------- |
| **IO-bound tasks** (API calls, file I/O) | thread_pool or async (run_aio)               |
| **CPU-bound tasks < 100ms**              | thread_pool                                  |
| **CPU-bound tasks > 100ms**              | process_pool (GIL bypass worth the overhead) |
| **Light/fast tasks**                     | sequential or thread_pool                    |
| **Debugging**                            | sequential (build_sequential)                |

### Key Takeaways

1. **Thread pool is the default** - good balance for most workloads.

2. **Process pool wins for heavy CPU work** - when tasks take >100ms each, process spawn overhead becomes negligible and true parallelism provides significant speedup.

3. **Implement `run_aio()` for async benefits** - allows the build system to run async code natively without thread overhead.

4. **Sequential for debugging** - use `build_sequential()` when you need deterministic execution order.

## Files

- `tasks.py` - Benchmark task definitions (IO/CPU/Heavy CPU/Light workloads)
- `dags.py` - DAG factory functions (static trees, flat high-concurrency, heavy CPU)
- `run_benchmark.py` - Benchmark runner script
- `results.json` - Raw benchmark results
