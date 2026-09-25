# SDK Advanced: Validation, Testing, Async, Dynamic Dependencies, Namespaces & Artifacts

## Load Validation

`LoadValidator[T]` provides automatic validation on `Task._save()` and `Task.load()`. Validators are attached via `typing.Annotated`, following the same pattern as serializers.

### Defining a Validator

```python
import typing
import stardag as sd

class NonEmpty(sd.LoadValidator[list]):
    def validate(self, value: list) -> list:
        if not value:
            raise ValueError("List must not be empty")
        return value

class Clamped(sd.LoadValidator[float]):
    def __init__(self, lo: float, hi: float):
        self.lo, self.hi = lo, hi

    def validate(self, value: float) -> float:
        return max(self.lo, min(self.hi, value))  # transform
```

### Using Validators

```python
# Class API — validators chain left-to-right in Annotated order
class MyTask(sd.Task[typing.Annotated[list[int], NonEmpty()]]):
    def run(self):
        self._save([1, 2, 3])  # validated before saving

# Decorator API
@sd.task
def my_task() -> typing.Annotated[list[int], NonEmpty()]:
    return [1, 2, 3]

# Multiple validators chain
class StrictTask(sd.Task[typing.Annotated[float, Clamped(0, 1), RoundTo(2)]]):
    ...
```

### Attribute-Based Discovery (MRO Escape Hatch)

For cases where subclassing `LoadValidator` causes MRO conflicts:

```python
class MyValidator(SomeOtherBase):
    stardag_load_validator = True  # marker attribute

    def validate(self, value: str) -> str:
        if not value.strip():
            raise ValueError("Empty string")
        return value
```

Validators run on both `_save()` and `load()`. They can both reject (raise) and transform (return modified value).

## Test Harness

`test_harness` is a context manager in `stardag.testing` that sets up an isolated test environment with temporary target root directories and a `NoOpRegistry`:

```python
from stardag.testing import test_harness

def test_my_pipeline():
    with test_harness():
        task = MyTask(param="value")
        task.complete()
        result = task.load()
        assert result == expected
```

This is the recommended way to test task logic. It avoids touching real target roots or the registry.

## Async Support

Tasks support both sync and async execution. The framework automatically bridges between them.

### Async Task Implementation

```python
class AsyncTask(sd.Task[dict]):
    url: str

    async def run_aio(self):
        import httpx
        async with httpx.AsyncClient() as client:
            resp = await client.get(self.url)
            self._save(resp.json())
```

### Async Build

```python
await sd.build_aio(task)
result = await task.load_aio()
```

### Sync/Async Bridging

- If only `run()` implemented → `run_aio()` delegates to it via asyncio
- If only `run_aio()` implemented → `run()` calls it via `asyncio.run()`
- If both → async execution prefers `run_aio()`, sync uses `run()`

### Execution Modes

Control how tasks execute within the concurrent build:

- `ASYNC_MAIN_LOOP` — Run in the main asyncio event loop (default for async tasks)
- `SYNC_THREAD` — Run sync task in thread pool
- `SYNC_PROCESS` — Run sync task in process pool
- `SYNC_BLOCKING` — Run blocking in current thread (debugging only)

## Dynamic Dependencies

Tasks can discover new dependencies during execution using generator `run()`:

```python
class DynamicTask(sd.Task[list[dict]]):
    source_urls: list[str]

    def run(self):
        # Phase 1: yield tasks to be built
        fetch_tasks = [FetchTask(url=url) for url in self.source_urls]
        yield fetch_tasks  # BUILD CONTRACT: all complete before resuming

        # Phase 2: use results
        results = [t.load() for t in fetch_tasks]
        self._save(results)
```

**BUILD CONTRACT**: All yielded tasks are guaranteed complete before the generator resumes.

Async variant:

```python
class AsyncDynamic(sd.Task[list[dict]]):
    async def run_aio(self):
        tasks = [FetchTask(url=url) for url in self.urls]
        yield tasks
        results = [await t.load_aio() for t in tasks]
        self._save(results)
```

## Namespaces

Namespaces organize tasks and affect output paths and task IDs.

### Module-Level Namespace

```python
import stardag as sd

# Explicit namespace
sd.namespace("my_app.data_pipeline", scope=__name__)

# Auto from module path
sd.auto_namespace(scope=__name__)
```

### Effect on Output Paths

With `sd.namespace("my_app.pipeline", scope=__name__)`:

- Task `MyTask` gets full name: `my_app.pipeline.MyTask`
- Output path: `<root>/my_app/pipeline/MyTask/<id_prefix>/<id>.json`

### Scope Parameter

`scope` is a required parameter. Pass `__name__` to scope the namespace to the current module
and its submodules. Tasks defined in other modules are unaffected.

## Artifacts

Artifacts are rich outputs displayed in the Registry UI. They don't affect task execution.

```python
from collections.abc import Sequence
from stardag.artifact import Artifact, JSONArtifact, MarkdownArtifact

class MetricsTask(sd.Task[dict[str, float]]):
    def run(self):
        metrics = {"accuracy": 0.95, "f1": 0.92}
        self._save(metrics)

    def artifacts(self) -> Sequence[Artifact]:
        """Called after task completion to generate display artifacts."""
        metrics = self.load()
        return [
            JSONArtifact(name="metrics", body=metrics),
            MarkdownArtifact(
                name="report",
                body=f"# Metrics\n\n- Accuracy: {metrics['accuracy']:.2%}\n- F1: {metrics['f1']:.2%}",
            ),
        ]
```

Artifact types:

- `JSONArtifact(name, body)` — structured JSON data
- `MarkdownArtifact(name, body)` — formatted markdown (tables, charts, reports)

## Versioning Strategy

```python
class MyTask(sd.Task[int]):
    __version__ = "2"           # Bump when logic changes (the version field follows it)

    def run(self):
        # New logic in v2
        self._save(42)
```

When to bump version:

- Task logic changes (different output for same inputs)
- Serialization format changes
- Bug fixes that affect output values

Version change → new task ID → new output path → forces re-execution. A change to
`requires()` or a fan-out that does not change the output needs **no** bump: dependency
structure belongs to the deployment, not the task id.

## HashableSet

For set-valued parameters that need deterministic hashing:

```python
class FilterTask(sd.Task[pd.DataFrame]):
    categories: sd.HashableSet[str]  # Hashable frozenset

    def run(self):
        # categories is a frozenset
        df = load_data()
        filtered = df[df["category"].isin(self.categories)]
        self._save(filtered)

# Usage — pass a regular set or frozenset (Pydantic coerces it)
task = FilterTask(categories={"a", "b", "c"})
```

`HashableSet` ensures deterministic ordering for consistent task IDs regardless of set insertion order.

## StardagBaseModel

Base Pydantic model with special modes for hash computation and compatibility:

```python
class MyConfig(sd.StardagBaseModel):
    param: int
    name: str
```

Use it for nested parameter models: `StardagField(significant=False)` and `compat_default`
work on its fields exactly as on a task's, so a nested model carries its own significance.

## Polymorphic Type System

### SubClass[T]

Validated subclass type for accepting any subclass of T:

```python
class Pipeline(sd.Task[dict]):
    # Accepts any subclass of TargetTask that produces DataFrame
    data_source: sd.SubClass[sd.TargetTask[LoadableSaveableFileSystemTarget[pd.DataFrame]]]
```

### TaskLoads[T] (Convenience Alias)

`sd.TaskLoads[T]` is equivalent to `sd.SubClass[sd.LoadableTask[T]]`:

```python
class Consumer(sd.Task[int]):
    # These are equivalent:
    data: sd.TaskLoads[list[int]]
    # data: sd.SubClass[sd.LoadableTask[list[int]]]
```

### Polymorphic Marker

`Polymorphic()` is an `Annotated` metadata marker that enables runtime polymorphic type
discrimination in Pydantic fields. It is NOT used as a generic (`Polymorphic[T]` is wrong).

```python
from typing import Annotated
from stardag.polymorphic import Polymorphic

class MyModel(sd.StardagBaseModel):
    # Correct: Annotated with Polymorphic() marker
    task: Annotated[sd.LoadableTask[int], Polymorphic()]

    # Equivalent shorthand using SubClass:
    task: sd.SubClass[sd.LoadableTask[int]]
```

`SubClass[T]` is syntactic sugar for `Annotated[T, Polymorphic()]`.

## Integration Points

### Prefect

```python
from stardag.integration.prefect import build as prefect_build

# Runs the DAG as a Prefect flow (sync wrapper; build_aio for async callers)
prefect_build(root_task)
```

### Modal

The packaged setup is `StardagApp`: a `build` function (resident builds), per-worker
`worker_<name>` functions, the reactive scheduler's `bootstrap` and `tick` functions, and an
optional watchdog cron.

```python
from stardag.integration.modal import StardagApp, FunctionSettings

app = StardagApp(
    "my-app",
    builder_settings=FunctionSettings(image=image),
    worker_settings={"default": FunctionSettings(image=image)},
    # watchdog_period_minutes=5,           # optional timed sweep (lapsed claims)
    # limit_key_selector=my_limit_keys,    # registry concurrency-limit keys per task
    # container_setup=my_container_setup,  # runs once in EVERY container
    # task_modules=["my_pkg.tasks"],       # inferred from the app's package by default
)
```

```bash
stardag modal deploy my_pkg/app.py   # records a deployment, deploys, activates it
```

```python
result = app.build_trigger(root_task)                 # resident: the build function drives it
result = app.build_trigger(root_task, reactive=True)  # reactive: short-lived ticks drive it
result = app.build_trigger(
    root_task,
    reactive=True,
    settings={"MYAPP_THREADS": "8"},                   # applied in every process of the build
    tick_kwargs={"max_attempts": 3},                   # TickConfig, stored with the build
)
app.build_trigger(root_task, build_id=result.build_id, reactive=True)  # resume / wake it
```

`build_trigger` mints the build at the trigger, so any restart resumes the same build. A
re-trigger names the same roots (a build is one request; other roots are refused — start a new
build). It needs registry credentials (the active stardag profile) and Modal credentials.

**Deployments.** `stardag modal deploy` mints a deployment id, bakes it into every function as
`STARDAG_DEPLOYMENT_ID`, records the deployment with the registry **before** the deploy and
activates it **after**; a failed record or activation exits non-zero (re-run the deploy).
`stardag deployments list` shows them and marks each app's current one. A running reactive
build **follows the live deployment**: the first tick on new code re-plans it (`rolled_over`
in its tick summary), and a tick on old code exits `superseded`. A redeploy of unchanged code
is a new deployment too. What cannot roll over (a root whose task id changed, a class the new
code cannot import) fails the build. A branch that should run beside production is a separate
app name.

A **local** `sd.build()` plans under a local deployment keyed on its code id:
`STARDAG_CODE_ID` if set, else the clean git commit, else a fresh one-off id (dirty tree) — so
local builds at one clean commit share structure. A hybrid build whose tasks run on a Modal app
plans under that app's current deployment.

**Reactive builds** rebuild every task from the registry's stored instance body, which needs
the task classes importable in the tick: declare `task_modules` if inference cannot find them.
Tick budgets (`stardag.build.TickConfig`, passed as `tick_kwargs`):

| `TickConfig` field  | Default | Bounds                                                                                      |
| ------------------- | ------- | ------------------------------------------------------------------------------------------- |
| `max_attempts`      | 2       | a spawn failing before any container starts, retried within one claim                       |
| `max_interruptions` | 20      | a task checkpointing and raising `ResumableInterruption`                                    |
| `max_executions`    | 20      | executions of a task in the build (a dead worker's lapsed claim is taken over as a new one) |
| `linger_seconds`    | 120     | how long an idle tick waits for a wake-up before exiting                                    |

An exception inside a task is `FAILED` and is never retried automatically: `stardag tasks
retry` or a re-trigger moves it back to `PENDING`. Deployed ticks, workers and the bootstrap
run one input per container (`stardag modal deploy` refuses `max_concurrent_inputs` above one
on them), because each applies its build's settings to the process environment.

**Placement rule** for everything you pass as a callable (`container_setup`,
`worker_selector`, `limit_key_selector`, `build_function`, `run_function`): define it in an
importable module of your own package and _import_ it into the file you deploy. They are
cloudpickled by reference; a `def` in the deploy script pickles as `app.<name>` and fails in
every container. `StardagApp(...)` raises `SerializedCallablePlacementError` for this.

Worker executions are **detached** Modal calls: they survive the driver, report their own
lifecycle to the registry, and a later build re-attaches instead of re-running.

See `docs/docs/concepts/modal-orchestration.md` and `docs/docs/how-to/integrate-modal.md` for
the full model.

### AWS S3

```bash
# Set an S3 target root; tasks then persist to S3 automatically
export STARDAG_TARGET_ROOTS='{"default": "s3://my-bucket/stardag/"}'
```

## Error Handling

```python
from stardag.exceptions import (
    StardagError,          # Base exception
    APIError,              # Registry API communication errors
    NotFoundError,         # 404 — also what an SDK/server version mismatch looks like
    AuthenticationError,   # Auth failures (missing/invalid credentials)
    AuthorizationError,    # Permission denied (403)
    TokenExpiredError,     # Auth token expiration
    InstanceConflictError,       # One task id constructed twice in one build
    UnstableSerializationError,  # A field whose dump is not a fixed point
    # Raised BY a task, not caught: "I checkpointed, run me again".
    ResumableInterruption,
    # Raised BY a task that was told to stop (see Cooperative cancellation).
    ExecutionCancelled,
)

from stardag.build import (
    BuildFailed,           # Raised by BuildSummary.raise_on_failure()
    SettingsError,         # Reserved settings key, or conflicting settings in one process
    TaskExecutionError,    # Wraps task executor exceptions with formatted tracebacks
)
```

`TaskExecutionError` preserves tracebacks across thread/process/remote executor boundaries. `BuildFailed` has a `.summary` attribute with the full `BuildSummary`.

### Surviving preemption and timeouts

A task that can be killed and resumed checkpoints and says so:

```python
import stardag as sd
from stardag.integration.modal import MODAL_INTERRUPTIONS


class TrainModel(sd.TargetTask[sd.DirectoryTarget]):
    def target(self) -> sd.DirectoryTarget:
        return sd.get_directory_target(sd.get_default_relpath(self))

    def run(self):
        directory = self.target()
        checkpoint = directory / "checkpoint.json"
        try:
            train(resume_from=checkpoint)
        except MODAL_INTERRUPTIONS:        # preemption OR the function timeout
            save_checkpoint(checkpoint)
            raise sd.ResumableInterruption("checkpointed") from None
        directory.mark_done()
```

**Rules:**

- Catch `MODAL_INTERRUPTIONS`, never `BaseException` — a blanket catch
  sweeps up ordinary bugs and would resume a `NameError` until the budget
  runs out. `except KeyboardInterrupt:` is also wrong: it misses timeouts.
- An interruption you do **not** catch leaves the execution to die with no
  report, like a crashed container: its claim lapses and a later tick takes
  it over as a fresh execution, bounded by `TickConfig.max_executions`
  (default 20) — not `max_attempts`, which covers only a spawn failing
  before any container starts. No configuration decides whether a timeout
  was "expected": the task answers by raising `ResumableInterruption` or not.
- Resumption is bounded by `TickConfig.max_interruptions` (default 20). A
  preemption is restarted by Modal on the same call and spends no budget.
- Only reactive builds resume. `sd.build`/`build_aio` fail the task.
- The checkpoint goes inside the task's directory target; `mark_done()` is
  what marks the task complete.

## Cooperative Cancellation

Cancelling a build releases its claims; nothing reaches into a running container. A worker
checks at the start of each attempt and at each dynamic-dependency yield whether its execution
is still wanted, and stops cleanly if not. For a long `run()`, ask where stopping is safe:

```python
class LongTraining(sd.TargetTask[sd.DirectoryTarget]):
    epochs: int = 10

    def target(self) -> sd.DirectoryTarget:
        return sd.get_directory_target(sd.get_default_relpath(self))

    def run(self):
        directory = self.target()
        for epoch in range(self.epochs):
            if sd.cancellation_requested():   # throttled (30s); False outside a worker
                raise sd.ExecutionCancelled() # raise, never return: no output, no completion
            train_one_epoch(directory, epoch)
        directory.mark_done()
```

A registry that cannot be reached answers `False`: a worker never stops on silence. To end
containers _now_, use `stardag builds stop` (see registry-and-platform.md).

## TaskRef (Immutable Reference)

```python
ref = sd.TaskRef.from_task(my_task)
print(ref.name)       # "MyTask"
print(ref.version)    # "1"
print(ref.id)         # UUID
print(ref.slug)       # "my-namespace-MyTask-abc123"
```

Useful for logging, artifact keys, and API references.
