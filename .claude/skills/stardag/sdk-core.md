# SDK Core: Tasks, Dependencies & Build

## Task Hierarchy

### BaseTask (Abstract Foundation)

All tasks inherit from `BaseTask`. It provides:

- `id` property: Deterministic UUID-5 based on parameter hash
- `complete()` / `complete_aio()`: Check if output exists
- `run()` / `run_aio()`: Execute the task logic
- `requires()`: Declare dependencies (return single task, list, or dict)
- `get_name()`, `get_namespace()`, `get_version()`: Task metadata

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

### AliasTask[T] (Reference Remote Outputs)

Reference a task output that was produced elsewhere:

```python
remote_task = sd.AliasTask[pd.DataFrame].from_registry(
    id="abc123...",  # accepts str or UUID
    registry=registry,
)
data = remote_task.load()
```

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
Both accept any `LoadableTask[T]` subclass, enabling polymorphic composition.

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

All build functions accept these optional parameters:

```python
sd.build(
    task,
    on_registry_failure="warn",  # "warn" (default) or "raise"
    register_all=False,          # True: register all tasks (even already-complete deps)
)

# raise_on_failure() for quick error propagation
summary = sd.build(task)
summary.raise_on_failure()  # raises BuildFailed on FAILURE status
```

- **`on_registry_failure`**: Controls whether registry errors propagate (`"raise"`) or are logged as warnings (`"warn"`, default).
- **`register_all`**: When `True`, discovery recurses into already-complete dependencies, ensuring all tasks in the DAG are registered in the registry for complete graph visibility.

### Global Concurrency Lock

For coordinating distributed builds across multiple processes/machines:

```python
from stardag.build import GlobalLockConfig

sd.build(task, global_lock_config=GlobalLockConfig(enabled=True))
```

Requires a configured Registry API connection.

### Build Behavior

1. Discovers all dependencies recursively from root task(s)
2. Checks `complete()` for each task — skips if already done
3. Executes tasks in dependency order (concurrent by default)
4. Handles dynamic dependencies (tasks that yield new deps during run)
5. Deadlock detection in sequential builds (raises `RuntimeError` if stuck)
6. In FAIL_FAST mode, task exceptions propagate to the caller immediately

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

### StardagField for Parameter Control

```python
from typing import Annotated

class MyTask(sd.Task[int]):
    # Included in hash (affects task ID) — default
    important_param: int

    # Excluded from hash (runtime-only, doesn't affect task ID)
    sleep_seconds: Annotated[float, sd.StardagField(hash_exclude=True)] = 1.0
    debug: Annotated[bool, sd.StardagField(hash_exclude=True)] = False
```

### Task ID Determinism

Task IDs are UUID-5 computed from:

- Namespace
- Task name
- Version
- All parameter values (recursively, including upstream task IDs)

Same parameters → same ID → same output path → skips re-execution.

## Versioning

```python
class MyTask(sd.Task[int]):
    __version__ = "1"
    version: str = __version__

    def run(self):
        self._save(42)
```

Bump `__version__` when task logic changes to force re-execution (changes the task ID and output path).
