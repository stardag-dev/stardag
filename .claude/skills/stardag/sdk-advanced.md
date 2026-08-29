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
    __version__ = "2"           # Bump when logic changes
    version: str = __version__  # Include in parameters for hash

    def run(self):
        # New logic in v2
        self._save(42)
```

When to bump version:

- Task logic changes (different output for same inputs)
- Serialization format changes
- Bug fixes that affect output values

Version change → new task ID → new output path → forces re-execution.

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

Used internally by tasks for parameter hashing. Supports `model_dump(mode="hash")` for deterministic serialization.

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
from stardag.integration.prefect import run_as_prefect_flow

# Wraps stardag DAG as a Prefect flow
run_as_prefect_flow(root_task)
```

### Modal

The packaged setup is `StardagApp` (deploys a `build` function, per-worker
`worker_<name>` functions, a reactive scheduler `tick` function, and an
optional watchdog cron):

```python
from stardag.integration.modal import StardagApp, FunctionSettings

app = StardagApp(
    "my-app",
    builder_settings=FunctionSettings(image=image),
    worker_settings={"default": FunctionSettings(image=image)},
    # watchdog_period_minutes=5,        # optional timer; sweep deployed anyway
    limit_key_selector=lambda t: [],    # named concurrency-limit keys per task
    container_setup=my_container_setup, # runs once in EVERY container
)

# After `stardag modal deploy`:
result = app.build_trigger(root_task)              # restart-safe (build id
                                                   # minted at the trigger,
                                                   # restarts resume)
result = app.build_trigger(root_task, reactive=True)  # experimental: no
                                                   # resident orchestrator —
                                                   # scheduler ticks drive it
app.build_spawn(root_task)                         # legacy fire-and-forget
```

**Container setup**: `container_setup` (a zero-arg callable, `ContainerSetup`)
runs once per container at the top of all five registered functions —
`build`, `worker_*`, `tick`, `bootstrap`, `tick_watchdog` — before stardag's
logging default. It is the only setup hook that reaches the reactive
functions (a `tick`/`bootstrap`/`tick_watchdog` container has no `Builder`
or `Runner` in it). It complements rather than replaces `Builder.setup(tasks)`
(per build, `build` container only) and `Runner.setup(task)` (per task).
Define it in a module importable inside the container — it is pickled by
reference, like `worker_selector`.

**Placement rule for all five callables** (`container_setup`,
`worker_selector`, `limit_key_selector`, `build_function`, `run_function`):
define them in an importable module of your own package and _import_ them
into the file you deploy. They are cloudpickled into the `serialized=True`
functions, and cloudpickle stores a module-level callable (or the class of
a callable instance) as a reference to its defining module. `stardag modal
deploy path/to/app.py` loads that file under the module name `app`, so a
`def` written there pickles as `app.<name>`, deploys cleanly, and then
fails in every container with `ModuleNotFoundError: No module named 'app'`.
`StardagApp(...)` raises `SerializedCallablePlacementError` for this.

Worker executions are **detached** Modal function calls by default: they
survive orchestrator restarts (resumed builds re-attach instead of
re-executing), are explicitly cancellable, and workers self-report their
lifecycle events to the registry. Low-level executor:
`ModalTaskExecutor(modal_app_name=..., worker_selector=..., detached=True)`
implements the `TaskExecutorABC` detached surface (`submit_detached` /
`reattach` / `detached_status` / `cancel_detached`).

Key `stardag.build` exports for the execution layer: `DetachedHandle`,
`DetachedExecutionStatus`, `get_current_build_id`, `run_tick_aio`,
`TickConfig`, `TickSummary`, `BuildTaskStore`, `discover_and_register_aio`,
`ClaimConfig`.

**Exactly-once by default (execution claims)**: task starts atomically
claim the task (registry-arbitrated); a losing racer re-attaches to the
winner instead of duplicating work. Control via `build(..., claim=...)`
(`None`=auto for probeable executions, `True`, `False`); reactive
scheduler ticks always claim. Custom arbitration backends implement
`RegistryABC.task_start_claim_aio`. `GlobalLockConfig` is deprecated
(kept for ref-less executions, now with background TTL renewal).

See `docs/docs/concepts/build-execution.md` and
`docs/docs/how-to/integrate-modal.md` for the full model.

### AWS S3

```python
# Set S3 target root
export STARDAG_TARGET_ROOTS='{"default": "s3://my-bucket/stardag/"}'

# Tasks automatically use S3 for persistence
```

## Error Handling

```python
from stardag.exceptions import (
    StardagError,          # Base exception
    APIError,              # Registry API communication errors
    AuthenticationError,   # Auth failures (missing/invalid credentials)
    AuthorizationError,    # Permission denied (403)
    TokenExpiredError,     # Auth token expiration
    # Raised BY a task, not caught: "I checkpointed, run me again".
    ResumableInterruption,
)

from stardag.build import (
    BuildFailed,           # Raised by BuildSummary.raise_on_failure()
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
- An interruption you do **not** catch is a failure, retried under
  `TickConfig.max_attempts`. That is the correct answer for a hung task or
  a too-small `timeout`, and it is why no configuration decides whether a
  timeout was "expected".
- Resumption is bounded by `TickConfig.max_interruptions` (default 20).
- Only reactive builds resume. `sd.build`/`build_aio` fail the task.
- The checkpoint goes inside the task's directory target; `mark_done()` is
  what marks the task complete.

## TaskRef (Immutable Reference)

```python
ref = sd.TaskRef.from_task(my_task)
print(ref.name)       # "MyTask"
print(ref.version)    # "1"
print(ref.id)         # UUID
print(ref.slug)       # "my-namespace-MyTask-abc123"
```

Useful for logging, artifact keys, and API references.
