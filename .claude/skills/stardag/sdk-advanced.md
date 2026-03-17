# SDK Advanced: Async, Dynamic Dependencies, Namespaces & Artifacts

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
from stardag.artifact import Artifact, JSONArtifact, MarkdownArtifact

class MetricsTask(sd.Task[dict[str, float]]):
    def run(self):
        metrics = {"accuracy": 0.95, "f1": 0.92}
        self._save(metrics)

    def artifacts(self) -> list[Artifact]:
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

```python
from stardag.integration.modal import ModalExecutor

# Execute tasks on Modal.com infrastructure
sd.build(task, task_executor=ModalExecutor(...))
```

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
)
```

## TaskRef (Immutable Reference)

```python
ref = sd.TaskRef.from_task(my_task)
print(ref.name)       # "MyTask"
print(ref.version)    # "1"
print(ref.id)         # UUID
print(ref.slug)       # "my-namespace-MyTask-abc123"
```

Useful for logging, artifact keys, and API references.
