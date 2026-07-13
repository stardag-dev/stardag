# Modal Integration Walkthrough

A ready-to-run tour of the full Stardag + Modal feature set: restart-safe
triggering with `build_trigger`, detached task execution, reactive
scheduling (no resident orchestrator), and registry-backed named
concurrency limits.

For the concepts behind everything shown here, see the how-to guide:
[Integrate with Modal](https://docs.stardag.com/how-to/integrate-modal/).
This example is the `stardag-examples` companion to that guide — the
minimal counterpart is [`modal/basic`](../basic/) (`build_spawn`, no
registry required).

## The DAG

`tasks.py` defines a small pipeline shaped to exercise the interesting
execution paths (all "work" is sleep-based, durations are parameters):

```
Report
├── Summarize            (dynamic deps: yields the shard fan-out)
│   ├── PlanShards       (static dep: decides the fan-out)
│   └── ProcessShard × N (medium tasks, named concurrency limit)
└── LongScan             (long-running, routed to the "long" worker)
```

`app.py` configures the `StardagApp`:

- **builder retries** — with `build_trigger`, a Modal-level restart of the
  build function resumes the same registry build.
- **two workers + `worker_selector`** — `LongScan` runs on a `long` worker
  with a higher timeout.
- **`watchdog_period_minutes=5`** — scheduled safety-net ticks for
  reactive builds (lost wake-ups, UI cancellations, stale limit slots).
- **`limit_key_selector`** — tags every `ProcessShard` with the
  `walkthrough-shards` named limit. The cap itself is configured in the
  registry (next section).

> The two selectors are defined in `selectors.py`, not `app.py`. They are
> captured by the serialized Modal functions (build, workers, and the
> reactive `tick` / `tick_watchdog`), which Modal deserializes in fresh
> containers — so any callable you pass to `StardagApp` must live in a
> module that's importable there (i.e. part of the source added via
> `add_local_python_source`). A selector defined in the deploy script
> itself would deserialize to `ModuleNotFoundError: No module named 'app'`
> on the first cold container — most visibly the scheduled watchdog tick.
> See `selectors.py` for the full explanation.

## Prerequisites

- A [Modal](https://modal.com/) account with credentials set up locally
  (`modal token new`).
- Stardag Registry credentials in the calling process — an active stardag
  profile (`stardag auth login`, or an API key). `build_trigger` mints the
  build id locally, unlike `build_spawn`.
- A registry server version matching this SDK. Reactive scheduling fails
  explicitly against an older server; concurrency-limit enforcement is
  **silently ignored** by an older server — upgrade before relying on it.
- An environment whose default target root the calling process can access
  (reactive mode persists task objects there), e.g. a Modal volume:

```sh
cd lib/stardag-examples
uv sync --extra modal

# Create an isolated environment backed by a Modal volume, and a profile:
uv run stardag environment create "Modal Walkthrough" \
    --target-root "default=modalvol://stardag-examples/target-roots/walkthrough"
uv run stardag config profile add walkthrough -e modal-walkthrough --default

# Give the deployed Modal functions access to the registry:
uv run stardag modal stardag-api-key create
```

## Deploy

```sh
uv run stardag modal deploy src/stardag_examples/modal/walkthrough/app.py
```

This creates the `build`, `worker_default`, `worker_long`, `tick` and
`tick_watchdog` functions (the latter two power reactive scheduling).

## Configure the named concurrency limit

The limit cap lives in the registry, per environment:

```sh
uv run python -m stardag_examples.modal.walkthrough.configure_limits --max-concurrent 3
```

(Equivalently: `PUT /api/v1/concurrency-limits/walkthrough-shards` with
`{"max_concurrent": 3}`, or the **Concurrency Limits** admin page in the
registry UI, where you can also inspect current slot holders.)

## Run

### Default mode: resident build function, detached tasks

```sh
uv run python -m stardag_examples.modal.walkthrough.main
```

Prints the `build_id` minted at the trigger point. The build function
orchestrates on Modal; every task runs as a _detached_ spawned function
call that survives orchestrator restarts.

Re-trigger the same build at any time — completed tasks are skipped,
still-running tasks are **re-attached** instead of re-executed (try it
while `LongScan` sleeps):

```sh
uv run python -m stardag_examples.modal.walkthrough.main --build-id <id>
```

### Reactive mode: no resident orchestrator (experimental)

```sh
uv run python -m stardag_examples.modal.walkthrough.main --reactive
```

Discovery happens at the trigger; the build is then driven purely by
short-lived scheduler _ticks_ (spawned at the trigger, by workers
finishing tasks, and by the watchdog). Between ticks nothing runs except
your tasks — no orchestrator container time, no orchestrator to crash.
Note in this mode the named limit is enforced registry-side **across
builds**: trigger two reactive builds for different sources and watch
their `ProcessShard` tasks share the 3 slots.

### Add a root to a running build (add-roots / retry path)

Re-triggering with the same build id and a _different_ source appends the
new DAG as an extra root of the same build (and resets any
failed/cancelled/skipped tasks to pending — the retry path):

```sh
uv run python -m stardag_examples.modal.walkthrough.main --reactive \
    --build-id <id> --source other-dataset
```

## What to look for in the registry UI

- **Builds page**: the build appears immediately at the trigger, before
  any Modal container has started.
- **Executor badges**: tasks executed on Modal carry an executor (⚡)
  badge with **deep links to the Modal dashboard** (app, function call) —
  from the recorded executor metadata (app name, workspace, environment,
  function name).
- **DAG view**: the `Summarize → ProcessShard` edges render as _dynamic_
  deps (discovered at runtime), unlike the static `requires()` edges; the
  task suspends while its yielded deps run.
- **Concurrency Limits admin page**: the `walkthrough-shards` cap and its
  live slot holders — watch `ProcessShard` tasks queue (stay pending) and
  drain as slots free.
- **Failure handling**: if a task fails, tasks transitively blocked by it
  are marked **skipped** — and a later re-trigger of the same build id
  retries them.

## Contrast: the same named limits from resident builds

Named limits are not reactive-only. A resident build — `sd.build(...)`
running anywhere, even locally — can enforce the _same_ registry-backed
limits (sharing slots with reactive builds) via
`RegistryConcurrencyLimiter`:

```python
import stardag as sd
from stardag.build import RegistryConcurrencyLimiter

from stardag_examples.modal.walkthrough.selectors import limit_key_selector
from stardag_examples.modal.walkthrough.tasks import report_dag

sd.build(
    report_dag("local-run"),
    concurrency_limiter=RegistryConcurrencyLimiter(key_selector=limit_key_selector),
)
```

Caveat when mixing modes: a crashed resident build has no automatic
healer — its RUNNING task holds the slot until explicitly
failed/cancelled via the API/UI. Prefer reactive scheduling for
unattended limited runs.
