# SDK Core: Tasks, Dependencies, Parameters & Build

## Task Hierarchy

### BaseTask (Abstract Foundation)

All tasks inherit from `BaseTask`. It provides:

- `id` property: Deterministic UUID-5 based on parameter hash
- `complete()` / `complete_aio()`: Check if output exists
- `run()` / `run_aio()`: Execute the task logic
- `requires()`: Declare dependencies (return single task, list, or dict)
- `get_name()`, `get_namespace()`, `get_version()`: Task metadata

A concrete task class implements `run()` or `run_aio()`. The check runs when the class is
first **constructed** (or validated), not when it is defined, so an abstract intermediate
base without `run()` is fine; constructing it raises `TaskImplementationError`.

### LoadableTask[T] (No Filesystem Target)

For tasks that produce data without persisting to filesystem targets:

```python
class MockData(sd.LoadableTask[list[int]]):
    count: int

    def complete(self) -> bool:
        return True  # Always available

    def run(self) -> None:
        pass  # Nothing to persist

    def load(self) -> list[int]:
        return list(range(self.count))
```

Use when: data comes from an API, database, or is generated on-the-fly.

### Task[T] (Recommended Default)

Automatic filesystem targets with serialization inferred from `T`:

```python
class ProcessData(sd.Task[dict[str, float]]):
    """Process raw data into metrics."""
    input_data: sd.TaskLoads[pd.DataFrame]

    def requires(self):
        return self.input_data

    def run(self):
        df = self.input_data.load()
        metrics = {"mean": df["value"].mean(), "std": df["value"].std()}
        self._save(metrics)
```

Key behaviors:

- `self._save(data)` persists output to auto-configured target
- `self.load()` loads the persisted output
- `self.complete()` checks if target file exists
- Output path: `<target_root>/<namespace>/<name>/v<version>/<id[0:2]>/<id[2:4]>/<id>.json`

### TargetTask[TargetType] (Maximum Control)

Explicit target definition:

```python
from stardag.target import LoadableSaveableFileSystemTarget
from stardag.target.serialize import FileSerializable, JSONSerializer

class CustomTarget(sd.TargetTask[LoadableSaveableFileSystemTarget[dict]]):
    param: int

    def target(self) -> LoadableSaveableFileSystemTarget[dict]:
        return FileSerializable(
            wrapped=sd.get_file_target(f"custom/{self.id}.json"),
            serializer=JSONSerializer(dict),
        )

    def run(self):
        self.target().save({"result": self.param * 2})
```

Use when: custom output paths, non-standard serializers, or S3/remote targets.

### Task.ref (Reference Registered Tasks)

Reference a task the registry already holds, by id, instead of
re-composing it (`stardag tasks ref <task-id>` prints the line):

```python
model = TrainedModel.ref("6f1c...", instance_id="0193...")  # no registry call
evaluation = Evaluate(model=model)  # same id as with the real task
sd.build(evaluation)                # hydrates `model` from the registry first
model.hydrate()                     # -> TrainedModel; model.load() delegates
```

`TrainedModel.ref(...)` is a `TaskRef[TrainedModel, LoadedT]`, a
`Task[LoadedT]`: pyright accepts it as `sd.TaskLoads[LoadedT]`. Builds
replace every reference with its hydrated task before registering;
`sd.resolve_refs(tasks)` does it outside a build. `sd.AliasTask` is
deprecated in favour of it.

### The Committed Task Store (Pinned, Long-Lived Instances)

**Which handle to use:** for pinned, long-lived instances in code (a key
dataset, a trained model), the committed task store, loaded **eagerly**; for
ad hoc work (a notebook loading an asset already materialized in the current
environment), `MyTask.ref(...)` / `from_registry`, with nothing saved to the
store. For key instances, also register them in the relevant (production)
environment so they stay searchable there with tags and descriptions: the
registry is the record of what ran, the store a reviewed, immutable copy.

```toml
# <package>/pyproject.toml  (one per package; registry config stays in .stardag/)
[tool.stardag]
task_store = "src/my_package/stardag_task_store"   # inside the package: ships with it
task_modules = ["my_package.tasks.*", "!my_package.tasks.deploy"]  # "!" excludes
```

```sh
stardag tasks ref <task-id> --write   # run in the package dir; copies a registry instance, prints the line
```

```python
BASELINE = TrainedModel.from_store("6f1c...", instance_hash="7a2e...")
BASELINE.learning_rate                                # real task, fields readable
BASELINE.model_copy(update={"learning_rate": 1e-4})   # derivations work
sd.write_to_store(TrainedModel(data=...))             # from a task object (not built yet is fine)
```

- **Which store**: explicit `store=` (relative: against the package root;
  or `"pkg:<package>/<dir>"`), then `STARDAG_TASK_STORE`, then the nearest
  `pyproject.toml` with `[tool.stardag]` walking up from the calling
  module's file (stardag-internal frames skipped), then from the task
  class's module, then from the CWD. So a pin reads its own package's store
  wherever it is imported from (monorepos, scripts, notebooks). Installed
  packages: the store dir holding the `stardag_task_store.toml` marker. The
  CLI resolves from the CWD.
- Format: `<store>/<namespace>.<Name>/<task_id>/<instance_hash>.json`, one
  file per distinct body, nested tasks as pointers
  (`{"__stardag_task": {"task", "task_id", "instance_hash"}}`; shared
  upstreams once); lossless, content-addressed, deterministic. Only `body`
  is verified; `sources` (instance ids) is metadata, so pass
  `instance_hash=` too (the printed lines do). Never edit files by hand.
- Each file records the class's module as an import hint, so `from_store`
  works in a fresh process with no imports. Any rehydration miss imports the
  applicable `task_modules` once and retries. A namespace is never read as a
  module path. A store is code: only read stores you trust.
- A root id mismatch names the deepest nested task whose id moved.
- `ref.hydrate()` and build-start hydration look in the store of the code
  that created the ref first (pinned: body stored under its instance id;
  unpinned: only one body stored); `hydrate(registry=...)` skips it.
- Remote containers have a store only if the image has it: keep the store
  inside the package. A module-level `from_store(...)` in a module a
  deployed app's `task_modules` match is imported in every tick container.
- Tests: read the store with `STARDAG_NO_REGISTRY=1`, no profile, no fixtures.
  `InMemoryRegistry.add_instance(body=, task_id=, instance_id=)` remains the
  way to seed an in-memory registry.
- `STARDAG_TASK_STORE` / `STARDAG_TASK_MODULES` override the config.

## Decorator API (@sd.task)

```python
@sd.task
def simple_task(param: int) -> list[int]:
    """Return type determines serialization."""
    return list(range(param))

@sd.task(name="CustomName", version="2")
def named_task(param: int) -> int:
    """Override name and version."""
    return param * 2

@sd.task
def with_dependency(upstream: sd.Depends[list[int]]) -> int:
    """sd.Depends[T] marks a parameter as a task dependency."""
    return sum(upstream)

# Compose the DAG:
root = with_dependency(upstream=simple_task(param=10))
sd.build(root)
```

**Important**: `sd.Depends[T]` is the decorator API equivalent of `sd.TaskLoads[T]`.
Both accept any `LoadableTask[T]` subclass, enabling polymorphic composition. A
`Depends` parameter takes a **task**: passing a raw value (`with_dependency(upstream=[1, 2])`)
is a `ValidationError`. `@sd.task` resolves annotations, so a module using
`from __future__ import annotations` behaves exactly like one without it.

## Dependencies

### Static Dependencies

```python
class MyTask(sd.Task[int]):
    upstream: sd.TaskLoads[list[int]]

    def requires(self):
        return self.upstream

    def run(self):
        data = self.upstream.load()
        self._save(sum(data))
```

### Multiple Dependencies (dict)

```python
class CombineTask(sd.Task[dict]):
    source_a: sd.TaskLoads[pd.DataFrame]
    source_b: sd.TaskLoads[pd.DataFrame]

    def requires(self):
        return {
            "a": self.source_a,
            "b": self.source_b,
        }

    def run(self):
        a = self.source_a.load()
        b = self.source_b.load()
        self._save({"combined": len(a) + len(b)})
```

### Multiple Dependencies (list)

```python
class AggregateTask(sd.Task[list[dict]]):
    sources: list[str]

    def requires(self):
        return [FetchTask(source=s) for s in self.sources]
```

### Polymorphic Dependencies

`sd.TaskLoads[T]` accepts ANY task that loads type `T`:

```python
class Analyzer(sd.Task[dict]):
    # Accepts any task that loads pd.DataFrame
    data: sd.TaskLoads[pd.DataFrame]

# All of these work:
Analyzer(data=CSVLoader(path="data.csv"))
Analyzer(data=APIFetcher(endpoint="/data"))
Analyzer(data=DatabaseQuery(sql="SELECT * FROM t"))
```

## Build Execution

### Basic Build

```python
import stardag as sd

task = MyTask(param=42)
sd.build(task)           # Concurrent (recommended)
result = task.load()
```

### Build Multiple Tasks

```python
sd.build([task1, task2, task3])
```

### Async Build

```python
await sd.build_aio(task)
result = await task.load_aio()
```

### Sequential Build (Debugging)

```python
sd.build_sequential(task)          # Sync
await sd.build_sequential_aio(task)  # Async
```

### Build Options

All four build functions share these keyword arguments:

```python
from stardag.build import FailMode

summary = sd.build(
    task,
    settings={"MYAPP_THREADS": "8"},  # build-wide env vars (see "Settings" below)
    fail_mode=FailMode.CONTINUE,      # default FAIL_FAST
    raise_on_failure=False,           # default True: FAIL_FAST re-raises the task's error
    on_registry_failure="warn",       # default "raise"; "warn" rides out a registry outage
    register_all=False,               # True: register complete deps too (full graph in the UI)
    description="nightly",
)
summary.raise_on_failure()            # raises BuildFailed on a FAILURE summary
print(summary.build_id)
```

- **`resume_build_id=`**: resume an existing build. Its plan for the same scope is reused,
  completed targets are observed, failed members reset. With `settings` omitted, the build's
  stored settings are reused; `settings={}` explicitly means none.
- **`limit_key_selector=`**: maps a task to the registry concurrency-limit key(s) it competes
  on (limits that hold across builds; see below).
- **`claim_config=`** (`stardag.build.ClaimConfig`): how claims are waited on and renewed.
- **`registry=`**: default is the configured registry; with none (`NoOpRegistry`) the build is
  purely local — no plan, no claims.
- **`on_registry_failure="warn"`** carries on through a registry _outage_; a _refusal_ (an
  instance conflict, a reserved settings key) always raises.

**Registry transport.** Every registry call retries a lost answer (timeout, network error, a
body cut short, 502/503/504, Modal proxy 500) up to three times with jittered backoff, and
waits out the registry's rate limit (429 `rate_limited`, at least `Retry-After`, about 150 s
in total). An answer the server wrote (any other error) is never retried. Each retry is
logged as a warning and counted:

```python
from stardag.registry._api_http import transport_retry_counts

transport_retry_counts()  # e.g. {"HTTP 429": 3, "ReadTimeout": 1}
```

### Settings: build-wide values that are not parameters

`settings` is a flat `Mapping[str, str]` of environment variables applied for the build's
duration in every process of it — the resident driver here, and on Modal the bootstrap, every
tick and every worker. Use it for a thread count, a feature flag, an endpoint: anything you
would rather not make a task parameter.

```python
from pydantic_settings import BaseSettings


class RunSettings(BaseSettings):
    myapp_threads: int  # required: a missing setting fails loudly

sd.build(root, settings={"MYAPP_THREADS": "8"})
```

- **The contract: settings may change structure and execution, never output.** Completion is
  global, so a setting that changed output would let one build reuse another's different
  result. Anything that affects output is a significant parameter.
- **Read settings at run time** (in `run()` / `requires()`), not at import: a warm container
  imports before it knows its build.
- **Nothing validates the keys.** A misspelled key is an environment variable nothing reads;
  use a settings class with required fields so a missing one fails.
- Keys starting `STARDAG_` or `MODAL_` are refused (`SettingsError`) before a build exists.
  Settings are not for credentials.
- One settings owner per process: concurrent `sd.build()` calls with _different_ settings in
  one process raise `SettingsError`.
- Settings are the second half of the build's **scope** `(deployment, settings)`: a re-trigger
  under new settings plans a new scope.
- Same knob elsewhere: `app.build_trigger(root, settings={...})` on Modal, and
  `stardag build mypkg.dags:root --settings MYAPP_THREADS=8` on the CLI.

### Concurrency Limits

Two kinds, combinable.

**Build-local** — `ConcurrencyConfig` caps how much one build process submits at once:

```python
from stardag.build import ConcurrencyConfig

sd.build(
    task,
    concurrency_config=ConcurrencyConfig(
        max_concurrent_tasks=8,                       # overall cap (optional)
        limits={"request-to-service-x": 10},          # named limits
        # Map each task to its limit name(s); None = unlimited.
        key_selector=lambda t: (
            "request-to-service-x" if isinstance(t, ServiceXTask) else None
        ),
    ),
)
```

A slot is held only while a task is actively executing (released while it is suspended on
its own dynamic deps). `concurrency_limiter=...` overrides `concurrency_config`.

**Across builds** — named limits configured per environment in the registry
(`stardag concurrency-limits set <key> <max_concurrent>`) hold for every build, process and
scheduling mode. A task competes for a key when `limit_key_selector` (on `sd.build`, or on
`StardagApp` for reactive builds) returns it; the check is atomic with the claim.

### Build Behavior

1. Walks the DAG from the root task(s), stopping at complete tasks (target exists)
2. With a registry: registers a **plan** — roots first, then the walk in chunks, then sealed
3. Every execution **claims** its task first, so two builds never run one task at once; a
   loser waits on or re-attaches to the winner, and a lapsed claim is taken over
4. Runs tasks in dependency order (concurrent by default) and handles dynamic dependencies
5. A walk that fails (an `InstanceConflictError`, a `requires()` that raises, an
   `UnstableSerializationError`) fails before any build exists
6. In `FAIL_FAST` mode the failing task's exception propagates (unless `raise_on_failure=False`)

## Type System

### Generic Type Parameters

`Task[T]` where `T` determines:

- Serialization format (JSON for primitives/collections, pickle for custom classes, CSV for DataFrame)
- Load return type
- Type checking via pyright

### Supported Types for Automatic Serialization

- Primitives: `int`, `str`, `float`, `bool`, `None`
- Collections: `list[T]`, `dict[K,V]`, `tuple[T,...]`, `set[T]`
- Data: `pd.DataFrame` (CSV serialization)
- Pydantic models: Any `BaseModel` subclass (JSON serialization)
- Custom classes: Pickle serialization (fallback)

### Parameters: significant and non-significant

Every field is **significant** by default: part of `task.id`, so it names the output. Mark a
field `significant=False` when it changes only how the work is done, or which upstreams are
required or yielded — never the output:

```python
from typing import Annotated

import stardag as sd


class Aggregate(sd.Task[int]):
    period: str                                                        # significant
    partition_size: Annotated[int, sd.StardagField(significant=False)] = 100
    num_threads: Annotated[int, sd.StardagField(significant=False)] = 4

    def run(self):
        self._save(len(self.period))


a = Aggregate(period="2024-01", partition_size=500)  # both kinds passed at init
assert a.id == Aggregate(period="2024-01").id        # same output, same task id
```

- Both kinds are **ordinary parameters**: passed at init, stored on the registry's task
  instance, rehydrated from it.
- `StardagField(significance=...)` and `StardagField(hash_exclude=...)` were **removed** and
  raise `TypeError`. The rename to `significant=False` moves no task id.
- `compat_default` is for adding a **significant** field without re-keying existing tasks: a
  value equal to it is dropped from the hash (and a missing field on rehydration takes it).
  Invalid on a non-significant field.

```python
class Report(sd.Task[str]):
    period: str
    # Added later: tasks built before it existed keep their ids.
    currency: Annotated[str, sd.StardagField(compat_default="EUR")] = "EUR"

    def run(self):
        self._save(f"{self.period} in {self.currency}")
```

### The two hashes

| Hash                 | Covers                                           | Answers                                       |
| -------------------- | ------------------------------------------------ | --------------------------------------------- |
| `task.id`            | namespace, name, version, **significant** fields | "Is this output done?" — global, every build  |
| `task.instance_hash` | the same plus every **non-significant** field    | "How exactly was it constructed?" — per scope |

Completion, the execution claim and the target path key on `task.id`. The registry stores each
construction under a scope as a **task instance** (keyed on `instance_hash`).

Two task objects may share a task id and differ in non-significant fields — but **one build
plans one instance per task id**: constructing both in one DAG raises `InstanceConflictError`
at discovery, naming the differing fields.

**Serialization stability.** At registration each distinct instance is round-tripped once; a
field whose dump is not a fixed point (a naive vs aware datetime, a custom serializer that
drops precision, a non-deterministic float) raises `UnstableSerializationError`. Sets are
sorted for you; non-finite floats are refused. Run the same check in a test with
`sd.check_serialization_stability(task)`.

### Task ID Determinism

Task IDs are UUID-5 computed from:

- Namespace
- Task name
- Version
- Every **significant** parameter value (recursively; a nested task appears by its own task
  id, so it contributes only its significant identity)

Same significant parameters → same ID → same output path → skips re-execution. To change
what a task produces, change its task id (bump `__version__` or add/change a significant
parameter); downstream ids follow on their own. Nothing marks a completed task incomplete by
fiat: delete its target and build again.

To derive a variant, `task.model_copy(update={"a": 2})` is safe: the copy drops the source's
cached identity (`id`, `instance_hash`, and any `functools.cached_property`) and recomputes
it from its own fields, so `copy.id == MyTask(a=2).id`.

## Versioning

```python
class MyTask(sd.Task[int]):
    __version__ = "1"

    def run(self):
        self._save(42)
```

Bump `__version__` when task logic changes to force re-execution (changes the task ID and output path). The `version` instance field defaults automatically to `cls.__version__` — no boilerplate needed.
