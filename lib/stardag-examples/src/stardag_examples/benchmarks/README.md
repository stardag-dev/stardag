# Build Configuration Benchmarks

Compares execution time across different build configurations for various workload types.

## Running

```bash
cd lib/stardag-examples
uv run python -m stardag_examples.benchmarks.run_benchmark
```

## Results Summary

| Scenario                    | Sequential | Thread Pool | Process Pool |
| --------------------------- | ---------- | ----------- | ------------ |
| io_bound_tree (15 tasks)    | 1.56s      | 0.41s       | -            |
| io_bound_flat_64 (65 tasks) | 5.95s      | 0.71s       | -            |
| cpu_bound_tree (15 tasks)   | 0.30s      | 0.29s       | 1.11s        |
| heavy_cpu_flat (9 tasks)    | 9.25s      | 8.96s       | 4.07s        |
| light_tree (15 tasks)       | 0.001s     | 0.002s      | -            |

## Key Takeaways

**IO-bound workloads**: Concurrent execution provides ~8x speedup. Thread pool and async perform similarly because GIL is released during sleep/IO.

**CPU-bound workloads**: Thread pool provides no speedup (GIL blocks true parallelism). Process pool achieves ~2x speedup on heavy tasks but has spawn overhead that makes it slower for light CPU work.

**Light workloads**: Sequential is fastest. Coordination overhead exceeds task work.

## Configuration Options

- `sync_run_default="thread"`: Route sync tasks to thread pool (default)
- `sync_run_default="process"`: Route sync tasks to process pool (true parallelism, spawn overhead)
- `sync_run_default="blocking"`: Run sync tasks via `asyncio.to_thread` on main loop

## When to Use Process Pool

Use process pool (`sync_run_default="process"`) when:

- Tasks do heavy CPU work (>100ms per task)
- True parallelism benefit outweighs spawn overhead
- Tasks are serializable (picklable)

## Completion Check Benchmark

Tests overhead of checking task completion with simulated S3 latency (50ms HEAD request).

```bash
uv run python -m stardag_examples.benchmarks.completion_check
```

| Scenario                           | Sequential | Concurrent |
| ---------------------------------- | ---------- | ---------- |
| 101 pre-completed tasks (50ms/chk) | 5.47s      | 0.06s      |

**93x speedup** with parallel completion checking via `asyncio.gather()`.
