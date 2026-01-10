# Concurrent Build Benchmarks

This directory contains benchmarks comparing the concurrent build implementations for stardag task DAGs.

## Build Implementations

| Implementation    | Description                                                              |
| ----------------- | ------------------------------------------------------------------------ |
| **threadpool**    | Uses `ThreadPoolExecutor` with generator suspension for dynamic deps     |
| **asyncio**       | Uses `asyncio.create_task()` with recursive dependency resolution        |
| **asyncio_queue** | Uses `asyncio.Queue` with worker pool pattern                            |
| **multiprocess**  | Uses `ProcessPoolExecutor` with idempotent re-execution for dynamic deps |

## Async Task Support

Tasks can implement an optional `run_aio()` method for true async execution:

```python
class IOBoundTask(BenchmarkTask):
    sleep_duration: float = 0.1

    def _do_work(self) -> None:
        time.sleep(self.sleep_duration)  # Blocks thread

    async def run_aio(self) -> None:
        """True async I/O - no thread blocking."""
        await asyncio.sleep(self.sleep_duration)
        self.output().save({"task_id": self.task_id})
```

The asyncio builders automatically detect `run_aio()` and call it directly, enabling true concurrent execution without thread overhead.

## Test Scenarios

### Workload Types

| Workload      | Description                  | Characteristics                                     |
| ------------- | ---------------------------- | --------------------------------------------------- |
| **IO-bound**  | `asyncio.sleep(0.1)`         | True async sleep. Releases GIL during sleep.        |
| **CPU-bound** | 100k SHA-256 hash iterations | Light computation (~20ms). GIL limits parallelism.  |
| **Heavy CPU** | 5M SHA-256 hash iterations   | Heavy computation (~1s). Multiprocess wins here.    |
| **Light**     | `sum(range(100))`            | Near-zero work. Exposes scheduling overhead.        |
| **File I/O**  | Real file writes/reads       | Uses `target.open_aio()` for async file operations. |

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
- Tests high-concurrency scenarios (16 workers)

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

## Results

**Configuration:** In-process implementations only (threadpool, asyncio, asyncio_queue). Warmup: 1 run, Timed: 2 runs averaged.

| Scenario         | threadpool | asyncio | asyncio_queue |
| ---------------- | ---------- | ------- | ------------- |
| io_bound_static  | 0.536s     | 0.520s  | 0.511s        |
| cpu_bound_static | 0.287s     | 0.290s  | 0.283s        |
| light_static     | 0.004s     | 0.006s  | 0.003s        |
| heavy_cpu_static | 4.779s     | 4.896s  | 4.969s        |
| io_flat_32_w16   | 0.327s     | 0.335s  | 0.326s        |

Note: Multiprocess is excluded from the quick benchmark due to ~1s process spawn overhead per task. It excels for heavy CPU workloads (>1s per task) where spawn overhead is negligible.

## Analysis

### IO-Bound Workloads

All implementations perform similarly for IO-bound tasks:

- **io_bound_static**: ~0.51-0.54s for 15 tasks with 0.1s sleep each
- **io_flat_32_w16**: ~0.33s for 33 tasks with 16 workers

Why similar performance?

1. Both `time.sleep()` (threadpool) and `asyncio.sleep()` (asyncio) release the GIL
2. True concurrent execution across workers
3. No CPU contention

The `run_aio()` method provides true async sleep, but the performance difference is minimal with synthetic sleep because threads already release the GIL during `time.sleep()`.

### When Async Excels

Async advantages become significant with:

- **Real network I/O** (aiohttp vs requests) - connection pooling, no thread overhead
- **Many short-lived I/O operations** - thread creation overhead avoided
- **Memory-constrained environments** - coroutines use less memory than threads
- **Real async file I/O** - using `target.open_aio()` with aiofiles

### CPU-Bound Workloads

For light CPU work (~20ms per task), all in-process approaches show similar performance:

- Python's GIL prevents true parallel CPU execution in threads
- Tasks execute essentially sequentially despite thread/task pool
- asyncio_queue slightly faster due to lower coordination overhead

For heavy CPU work (~1s per task), all in-process approaches are GIL-limited:

- ~4.8-5.0s for 5 tasks that should parallelize
- Multiprocess would win here (not in quick benchmark)

### Light Workloads

In-process approaches are extremely fast (~3-6ms for 15 tasks):

- asyncio_queue often fastest (0.003s) due to efficient queue-based scheduling
- Minimal scheduling overhead
- Thread/task pool reuse eliminates spawn costs

## Recommendations

| Use Case                                 | Recommended Implementation                   |
| ---------------------------------------- | -------------------------------------------- |
| **IO-bound tasks** (API calls, file I/O) | asyncio or threadpool                        |
| **Async codebase with run_aio()**        | asyncio (native async execution)             |
| **CPU-bound tasks < 1s**                 | threadpool or asyncio_queue                  |
| **CPU-bound tasks > 1s**                 | multiprocess (GIL bypass worth the overhead) |
| **Light/fast tasks**                     | asyncio_queue (lowest overhead)              |
| **Dynamic dependencies**                 | threadpool (best dynamic dep handling)       |

### Key Takeaways

1. **All in-process implementations are comparable** for most workloads - choose based on your codebase style.

2. **asyncio_queue often edges out others** due to efficient queue-based scheduling with minimal coordination overhead.

3. **Implement `run_aio()` for true async benefits** - allows asyncio builders to call native async code directly without thread overhead.

4. **Multiprocess wins for heavy CPU work** - when tasks take >1s each, process overhead becomes negligible and true parallelism provides significant speedup.

5. **Dynamic deps favor threadpool** - the generator suspension pattern is more efficient than asyncio's approach.

## Files

- `tasks.py` - Benchmark task definitions (IO/CPU/Heavy CPU/Light/File I/O workloads)
- `dags.py` - DAG factory functions (static trees, flat high-concurrency, heavy CPU, file I/O)
- `run_benchmark.py` - Benchmark runner script
- `results.json` - Raw benchmark results
