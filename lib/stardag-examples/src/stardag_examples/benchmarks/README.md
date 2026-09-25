# Build Configuration Benchmarks

Compares execution time across different build configurations for various workload types.

## Running

```bash
cd lib/stardag-examples

# Default: NoOp registry (fastest, no network overhead)
uv run python -m stardag_examples.benchmarks.run_benchmark

# Quick mode (fewer scenarios, 1 run each)
uv run python -m stardag_examples.benchmarks.run_benchmark --quick

# With local registry (docker-compose at localhost:8000)
STARDAG_API_KEY=<key> uv run python -m stardag_examples.benchmarks.run_benchmark --registry local

# With remote registry (uses STARDAG_API_URL from env/config)
uv run python -m stardag_examples.benchmarks.run_benchmark --registry remote
```

### Command-line Options

- `--registry {noop,local,remote}`: Registry mode (default: noop)
- `--quick`: Run quick benchmark (fewer scenarios, fewer runs)

## Results Summary

### Baseline (NoOp Registry)

| Scenario                  | Sequential | Thread Pool | Process Pool |
| ------------------------- | ---------- | ----------- | ------------ |
| io_bound_tree (15 tasks)  | 1.57s      | 0.41s       | -            |
| cpu_bound_tree (15 tasks) | 0.31s      | 0.29s       | 1.08s        |
| heavy_cpu_flat (9 tasks)  | 9.25s      | 8.96s       | 4.07s        |
| light_tree (15 tasks)     | 0.001s     | 0.002s      | -            |

### Local Registry Comparison

| Scenario       | Config     | Time  |
| -------------- | ---------- | ----- |
| io_bound_tree  | sequential | 2.16s |
| io_bound_tree  | concurrent | 0.72s |
| cpu_bound_tree | sequential | 0.60s |
| cpu_bound_tree | concurrent | 0.58s |
| light_tree     | sequential | 0.24s |
| light_tree     | concurrent | 0.28s |

**Registry overhead**: Local registry adds ~0.6s for io_bound scenarios (API calls for task registration/completion tracking). This is network latency to localhost:8000.

These figures were measured against a v1 registry. The v1 `--lock` option (the
lease-based global concurrency lock) is gone: from stardag 0.27 the claim is the
only cross-build coordination, and it needs no option.

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
