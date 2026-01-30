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

# With local registry and global concurrency locks enabled
STARDAG_API_KEY=<key> uv run python -m stardag_examples.benchmarks.run_benchmark --registry local --lock

# With remote registry (uses STARDAG_REGISTRY_URL from env/config)
uv run python -m stardag_examples.benchmarks.run_benchmark --registry remote
```

### Command-line Options

- `--registry {noop,local,remote}`: Registry mode (default: noop)
- `--lock`: Enable global concurrency locks (requires real registry)
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

| Scenario       | Config     | No Locks | With Locks | Overhead |
| -------------- | ---------- | -------- | ---------- | -------- |
| io_bound_tree  | sequential | 2.16s    | 2.18s      | +1%      |
| io_bound_tree  | concurrent | 0.72s    | 0.82s      | +14%     |
| cpu_bound_tree | sequential | 0.60s    | 0.55s      | -8%      |
| cpu_bound_tree | concurrent | 0.58s    | 0.66s      | +14%     |
| light_tree     | sequential | 0.24s    | 0.24s      | 0%       |
| light_tree     | concurrent | 0.28s    | 0.42s      | +50%     |

**Registry overhead**: Local registry adds ~0.6s for io_bound scenarios (API calls for task registration/completion tracking). This is network latency to localhost:8000.

**Lock overhead**: Global concurrency locks add ~10-15% overhead for concurrent execution due to lock acquisition/release API calls. Light tasks show higher percentage overhead because task work is minimal.

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
