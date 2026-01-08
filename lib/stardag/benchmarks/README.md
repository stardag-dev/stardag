# Concurrent Build Benchmarks

This directory contains benchmarks comparing the four concurrent build implementations for stardag task DAGs.

## Build Implementations

| Implementation    | Description                                                              |
| ----------------- | ------------------------------------------------------------------------ |
| **threadpool**    | Uses `ThreadPoolExecutor` with generator suspension for dynamic deps     |
| **asyncio**       | Uses `asyncio.create_task()` with recursive dependency resolution        |
| **asyncio_queue** | Uses `asyncio.Queue` with worker pool pattern                            |
| **multiprocess**  | Uses `ProcessPoolExecutor` with idempotent re-execution for dynamic deps |

## Test Scenarios

### Workload Types

| Workload      | Description                  | Characteristics                                        |
| ------------- | ---------------------------- | ------------------------------------------------------ |
| **IO-bound**  | `time.sleep(0.1)` per task   | Simulates network/disk I/O. Releases GIL during sleep. |
| **CPU-bound** | 100k SHA-256 hash iterations | Light computation (~20ms). GIL limits parallelism.     |
| **Heavy CPU** | 5M SHA-256 hash iterations   | Heavy computation (~1s). Multiprocess wins here.       |
| **Light**     | `sum(range(100))`            | Near-zero work. Exposes scheduling overhead.           |

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

**Dynamic DAGs (9 tasks):**

```
         root
    (yields at runtime)
  / / / | | | \ \ \
 L0 L1 L2 L3 L4 L5 L6 L7
```

- 1 root task that dynamically discovers 8 leaf tasks
- Tests runtime dependency resolution

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

## Results

**Configuration:** 4 workers, 1 warmup run, 3 timed runs (averaged)

| Scenario          | threadpool | asyncio | asyncio_queue | multiprocess |
| ----------------- | ---------- | ------- | ------------- | ------------ |
| io_bound_static   | 0.534s     | 0.535s  | 0.534s        | 1.542s       |
| io_bound_dynamic  | 0.320s     | 0.948s  | 0.948s        | 1.878s       |
| cpu_bound_static  | 0.288s     | 0.292s  | 0.291s        | 1.109s       |
| cpu_bound_dynamic | 0.178s     | 0.174s  | 0.175s        | 1.022s       |
| **heavy_cpu**     | 4.796s     | 4.861s  | 4.944s        | **3.098s**   |
| light_static      | 0.004s     | 0.004s  | 0.003s        | 1.004s       |
| light_dynamic     | 0.002s     | 0.003s  | 0.003s        | 1.047s       |

## Analysis

### IO-Bound Workloads

For **static dependencies**, all in-process approaches perform similarly (~0.53s) because:

- Threads release the GIL during `time.sleep()`
- True concurrent execution across 4 workers
- Theoretical minimum: 0.1s \* 15 tasks / 4 workers = 0.375s (actual ~0.53s due to DAG structure)

For **dynamic dependencies**, **threadpool is fastest** (0.32s vs ~0.95s for asyncio):

- Threadpool handles generator suspension efficiently with minimal overhead
- Asyncio variants have more overhead managing dynamic dep discovery
- The flat DAG structure (9 tasks) is inherently faster than the tree (15 tasks)

**Multiprocessing** is slowest (~1.5-1.9s) due to process spawning overhead overwhelming the parallelism benefits for short-lived tasks.

### CPU-Bound Workloads (Light)

All in-process approaches show **similar performance** (~0.29s) because:

- Python's GIL prevents true parallel CPU execution in threads
- Tasks execute essentially sequentially despite thread pool
- The 100k iterations complete quickly (~20ms each)

**Multiprocessing** is slower (~1.1s) despite bypassing the GIL because:

- Process spawn overhead (~0.25s per worker) dominates
- Task JSON serialization/deserialization adds latency
- Only wins with much longer-running tasks

### CPU-Bound Workloads (Heavy)

For heavy CPU tasks (~1s each), **multiprocessing wins** (3.1s vs 4.8s):

- **Threadpool/Asyncio:** ~4.8s - GIL forces sequential execution of 5 tasks
- **Multiprocess:** ~3.1s - true parallelism (4 leaves run simultaneously, then root)

This demonstrates the crossover point: when individual task runtime exceeds process spawn overhead (~1s), multiprocessing becomes worthwhile.

### Light Workloads

In-process approaches are **extremely fast** (~3-4ms for 15 tasks):

- Minimal scheduling overhead
- Thread pool reuse eliminates spawn costs
- asyncio event loop is efficient for light work

**Multiprocessing** shows its **worst case** (~1s):

- Process spawning takes ~250ms per worker
- 99.6% of time is overhead, 0.4% is actual work
- Never appropriate for light tasks

## Recommendations

| Use Case                                 | Recommended Implementation                   |
| ---------------------------------------- | -------------------------------------------- |
| **IO-bound tasks** (API calls, file I/O) | threadpool or asyncio                        |
| **CPU-bound tasks < 1s**                 | threadpool (simpler, lower overhead)         |
| **CPU-bound tasks > 1s**                 | multiprocess (GIL bypass worth the overhead) |
| **Dynamic dependencies**                 | threadpool (best dynamic dep handling)       |
| **Light/fast tasks**                     | threadpool or asyncio (never multiprocess)   |
| **Async codebase**                       | asyncio (natural integration)                |

### Key Takeaways

1. **Threadpool is the best general-purpose choice** - good performance across all scenarios, efficient dynamic dep handling, simple mental model.

2. **Asyncio is comparable for static deps** - choose if your codebase is already async-heavy.

3. **Multiprocess wins for heavy CPU work** - when tasks take >1s each, the process overhead becomes negligible and true parallelism provides ~1.5x speedup.

4. **Dynamic deps favor threadpool** - the generator suspension pattern is more efficient than asyncio's approach.

## Limitations

### AsyncIO Implementation

The asyncio implementations cannot fully leverage async/await because the `TaskBase` interface only provides synchronous `run()` and `complete()` methods. The current implementation works around this by using `asyncio.to_thread()` to run synchronous task code without blocking the event loop.

If `TaskBase` supported async versions of these methods (e.g., `async def run_async()`, `async def complete_async()`), the asyncio implementation could:

- Directly `await` native async operations (aiohttp, asyncpg, etc.)
- Avoid thread pool overhead for async-native tasks
- Potentially outperform threadpool for IO-bound async workloads

This is a known limitation that may be addressed in a future version of the task interface.

## Files

- `tasks.py` - Benchmark task definitions (IO/CPU/Heavy CPU/Light workloads)
- `dags.py` - DAG factory functions (static trees, dynamic flat, heavy CPU)
- `run_benchmark.py` - Benchmark runner script
- `results.json` - Raw benchmark results
