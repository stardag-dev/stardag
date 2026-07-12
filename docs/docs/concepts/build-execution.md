# Build & Execution

Understanding how Stardag executes DAGs.

## Execution Model

Stardag uses "Makefile-style" bottom-up execution:

1. Start at the requested task
2. Check if it's complete (output exists)
3. If not, recursively ensure dependencies are complete
4. Execute incomplete tasks in dependency order
5. Persist outputs

Example:

```
Requested: Task C (depends on B, which depends on A)

Step 1: Is C complete? No
Step 2: Is B complete? No
Step 3: Is A complete? No
Step 4: Execute A, save output
Step 5: Execute B, save output
Step 6: Execute C, save output

```

Two properties follow from this model and hold across every execution mode
described below:

- **Completeness is target existence.** A task is done when its output
  exists in storage — not when some scheduler says so. Storage is the
  ground truth, which is what makes resumption, retries and de-duplication
  safe: re-running a build never re-executes work whose outputs exist.
- **Idempotent re-execution.** Tasks (including those with dynamic
  dependencies) are written so that re-running them from scratch is safe.
  The engine relies on this whenever execution crosses a process or
  machine boundary.

## The Build Functions

The primary way to execute tasks is `sd.build` or `await sd.build_aio`.

Mostly for testing and debugging, there is also `sd.build_sequential` and
`sd.build_sequential_aio`.

## Local Concurrency

`sd.build` runs an asyncio scheduling loop: it discovers the DAG, then
repeatedly submits every _ready_ task (all dependencies complete) to a
**task executor** and processes results as they arrive. The default
`HybridConcurrentTaskExecutor` runs each task in one of four modes,
selected per task (async-native tasks on the event loop; sync tasks in a
thread pool by default, optionally a process pool):

- `ASYNC_MAIN_LOOP` — `await task.run_aio()` on the main event loop
- `SYNC_THREAD` — `task.run()` in a thread pool (default for sync tasks)
- `SYNC_PROCESS` — `task.run()` in a process pool (CPU-bound work)
- `SYNC_BLOCKING` — inline (debugging)

Build-local concurrency limits — an overall cap and named limits mapped to
tasks via a `key_selector` — are configured with `ConcurrencyConfig` and
enforced by gating executor submission. These are scoped to the one build
process; see [Concurrency Limits](#concurrency-limits) below for limits
that hold across builds.

## Transfer of Execution (Remote Executors)

The scheduling loop is decoupled from _where_ tasks run through the
`TaskExecutorABC` seam. A remote executor — `ModalTaskExecutor` is the
first-class implementation — submits each task to remote infrastructure
instead of a local pool, and a `RoutedTaskExecutor` can mix executors
(e.g. GPU tasks to Modal, the rest locally). See
[Integrate with Modal](../how-to/integrate-modal.md) for the packaged
setup.

### Detached execution

Remote executions are **detached** by default: the worker invocation
survives the process that spawned it. The execution's backend reference
(e.g. a Modal function call id) is recorded in the registry together with
the task's started event, which buys three things:

- **Re-attach instead of re-execute.** A resumed build (or a concurrent
  build wanting the same task) finds the task RUNNING with a live ref and
  attaches to that execution rather than starting a duplicate. An
  orchestrator restart no longer restarts your long-running tasks.
- **Real cancellation.** Fail-fast and user cancellation cancel the
  tracked remote executions — workers of a failed or cancelled build
  don't keep running. (A _crashed_ orchestrator's workers deliberately
  keep running — that's the re-attach premise above.)
- **Liveness probing.** Schedulers can ask the backend whether a recorded
  execution is still running, finished, or gone — no lease/heartbeat
  machinery needed.

### Worker-side lifecycle reporting

Workers report their own lifecycle to the registry — started (with their
own execution ref), completed (plus artifacts), suspended (dynamic
dependencies), failed — whenever the build id is forwarded to them and the
container has registry credentials. Reporting is best-effort by design: a
registry hiccup never fails a task whose actual work succeeded, and a lost
completion event self-heals from target existence. The consequence: a
task's registry state stays accurate **independent of any orchestrator's
lifetime**.

## Reactive Scheduling (No Resident Orchestrator)

With detached executions and self-reporting workers, the build process
itself is optional. In _reactive scheduling_ the build is driven by
short-lived, idempotent scheduler **ticks** instead of a resident process:

```
tick(build_id):
  acquire the build's scheduler lease (single-flight; held → exit)
  loop:
    clear the build's wake-up flag
    frontier = registry state: tasks whose upstreams are all complete
    act: spawn pending/suspended tasks detached (recording refs)
         probe running refs — leave live ones; self-heal completions
         (target existence is ground truth); record failures
    handle terminal states (all roots complete / failure — with blocked
    tasks marked skipped / cancelled)
    linger briefly on the wake-up flag; exit when quiet
```

Ticks are triggered when the build starts, by workers finishing tasks
(flag-then-spawn, so wake-ups are never lost), and by an optional periodic
watchdog that also picks up externally-cancelled builds and silently-lost
workers. While a DAG churns, one lingering tick behaves like a tight
scheduling loop; when only long-running tasks remain in flight, **nothing
runs but your tasks**.

The registry is the scheduler state (the frontier is computed from
recorded task statuses and dependency edges); task _objects_ are
rehydrated from a per-build task store persisted at trigger time. The
registry never pushes or executes anything — only user-deployed code
(which has the DAG-defining code) spawns work.

Reactive scheduling is experimental and currently Modal-first — see
[Integrate with Modal](../how-to/integrate-modal.md#reactive-scheduling-no-resident-build-function-experimental)
for usage, requirements and limitations.

## Global Concurrency Lock

Within one build, the engine guarantees each task executes at most once.
Across builds (started from anywhere), the optional **global concurrency
lock** extends that guarantee: a lease-based lock per task id, scoped to
the environment, acquired before execution and released together with the
completion record in one transaction (which also absorbs eventually-
consistent storage). Enable it per build or per task via
`GlobalLockConfig`; it adds a few registry roundtrips per task, so prefer
enabling it selectively for expensive or non-idempotent tasks.

## Concurrency Limits

Two mechanisms, by scope:

- **Build-local** (`ConcurrencyConfig`): asyncio-semaphore limits inside
  one build process — an overall cap plus named limits via a
  `key_selector`. Uniform across local and remote executors.
- **Registry-backed named limits** (reactive scheduling): caps configured
  per environment in the registry
  (`PUT /api/v1/concurrency-limits/{key}`), enforced atomically when a
  task starts — **across all builds** in the environment. A task occupies
  a slot simply by being RUNNING with the key recorded; the slot frees on
  any terminal status (no leases — status liveness is maintained by worker
  reporting and tick self-healing). Denied tasks stay pending and proceed
  when a slot frees.

Infrastructure-level limits (e.g. Modal's per-function
`concurrency_limit`) apply independently underneath either mechanism.
