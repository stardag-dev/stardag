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
| **CPU-bound** | 100k SHA-256 hash iterations | Actual computation. GIL limits parallelism in threads. |
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

## Running the Benchmark

```bash
cd lib/stardag
uv run python -m benchmarks.run_benchmark
```

## Results

**Configuration:** 4 workers, 1 warmup run, 3 timed runs (averaged)

| Scenario          | threadpool | asyncio | asyncio_queue | multiprocess |
| ----------------- | ---------- | ------- | ------------- | ------------ |
| io_bound_static   | 0.534s     | 0.531s  | 0.534s        | 1.538s       |
| io_bound_dynamic  | 0.321s     | 0.954s  | 0.948s        | 1.887s       |
| cpu_bound_static  | 0.290s     | 0.296s  | 0.298s        | 1.110s       |
| cpu_bound_dynamic | 0.179s     | 0.179s  | 0.183s        | 1.036s       |
| light_static      | 0.004s     | 0.004s  | 0.003s        | 1.006s       |
| light_dynamic     | 0.003s     | 0.003s  | 0.003s        | 1.039s       |

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

### CPU-Bound Workloads

All in-process approaches show **similar performance** (~0.29s) because:

- Python's GIL prevents true parallel CPU execution in threads
- Tasks execute essentially sequentially despite thread pool
- The 100k iterations complete quickly (~20ms each)

**Multiprocessing** is slower (~1.1s) despite bypassing the GIL because:

- Process spawn overhead (~0.1s per worker) dominates
- Task JSON serialization/deserialization adds latency
- Would only win with much longer-running tasks (>1s each)

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

3. **Multiprocess only for heavy CPU work** - the overhead makes it unsuitable for anything but long-running CPU-bound tasks (several seconds each).

4. **Dynamic deps favor threadpool** - the generator suspension pattern is more efficient than asyncio's approach.

## Files

- `tasks.py` - Benchmark task definitions (IO/CPU/Light workloads)
- `dags.py` - DAG factory functions (static trees, dynamic flat)
- `run_benchmark.py` - Benchmark runner script
- `results.json` - Raw benchmark results
