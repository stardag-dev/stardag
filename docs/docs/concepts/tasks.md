# Tasks

Tasks are the fundamental building blocks of Stardag. A task represents a unit of work that produces an output.

## What is a Task?

A task is:

- A **specification** of what to compute
- A **Pydantic model** with typed parameters
- **Serializable** to JSON for storage and transfer
- **Hashable** to produce a deterministic ID

## The Task Contract and Core Interface

Below is a minimal example of task:

```python
import stardag as sd

# Some external persistent state (typically *not* in memory as here)
world_state = {}

class MyTask(sd.BaseTask):
    # Declare any parameters
    parameter: str

    def run(self):
        # do some work
        result = len(self.parameter)
        # persist the result
        world_state[self.parameter] = result

    def complete(self):
        # let the outside world know if this task is complete
        return self.parameter in world_state
```

Even if contrived, it emphasizes the fundamental contract of a stardag task; At the very least, any task must implement the methods `complete` and `run`, and:

- `complete` should return `True` only if the task's desired world state is achieved
- `run` should only execute successfully once this state is achieved

To define how tasks depend on other tasks, each task must also implement the method:

```{.python notest}
    def requires(self) -> TaskStruct | None:
```

for which `BaseTask` default implementation simply returns `None` (no dependencies). When a task does return one or more tasks, it can - _and should_ - make the assumption that:

- all tasks returned from `self.requires()` are complete when `self.run()` is executed.

To some extent, _that's it_.

This allows us to implement build logic that traverses the Directed Acyclic Graph (DAG) of tasks and executes `run` in the correct order until the final desired tasks are complete.

```{.python continuation}
# instantiate an instance
my_task = MyTask(parameter="hello")

# build (or "materialize") the task and upstream
sd.build(my_task)

assert world_state == {"hello": 5}
```

## The Task Class Hierarchy

Stardag provides four base classes for defining tasks, each adding a layer of functionality. Understanding their roles helps you choose the right base class for your task.

```
            BaseTask               # complete(), run(), requires()
           /        \
LoadableTask[T]    TargetTask[TT]  # load() -> T  /  target() -> TT
           \        /
             Task[T]               # Combines both (TT = LoadableSaveableFileSystemTarget[T])
```

### `BaseTask` — Minimal Core Interface

`BaseTask` defines the minimal contract that the build system requires:

- `complete() -> bool` — Has the task's desired state been achieved?
- `run()` — Execute the task logic.
- `requires() -> TaskStruct | None` — What other tasks must be complete first?

Use `BaseTask` directly only when you need full control and none of the higher-level abstractions fit. For example, a task that interacts with an external system where "completeness" is defined by some custom check and the output isn't a file.

### `LoadableTask[T]` — Composable via `TaskLoads`

`LoadableTask[T]` extends `BaseTask` with a single abstract method:

- `load() -> T` — Load and return the task's output as a typed value.

This is the **minimal interface required for composability**. Any task that inherits `LoadableTask[T]` can be passed as a parameter annotated with `sd.TaskLoads[T]`:

Use `LoadableTask` when your task produces a typed output but doesn't use a standard filesystem target — for example, loading from a database or API.

!!! info "Task output should be deterministic given its parameters"

    If your task loads data from a database or API, it is important to make sure that it always produces the same output given the same input parameters. If you are querying something _mutable_, you should instead create an immutable snapshot of the data (referenced by e.g. a timestamp or date).

This can also be used for on-the-fly transformations that are not meaningful to persistently cache, or to generate data in unit testing.

```{.python notest}
import pandas as pd
import pandera.pandas as pa
from pandera.typing.pandas import DataFrame, Series


class MyDataset(pa.DataFrameModel):
    feature: Series[float]
    label: Series[int]


class MockDataset(sd.LoadableTask[DataFrame[MyDataset]]):
    """Generate a synthetic dataset for testing."""
    n_samples: int = 100

    def complete(self) -> bool:
        return True

    def run(self) -> None:
        pass

    def load(self) -> DataFrame[MyDataset]:
        return DataFrame[MyDataset](pd.DataFrame({
            "feature": range(self.n_samples),
            "label": [i % 2 for i in range(self.n_samples)],
        }))


class FilteredDataset(sd.LoadableTask[DataFrame[MyDataset]]):
    """Filter a dataset by a feature value range — too cheap to persist."""
    source: sd.TaskLoads[DataFrame[MyDataset]]
    feature_min: float
    feature_max: float

    def requires(self):
        return self.source

    def complete(self) -> bool:
        return self.source.complete()

    def run(self) -> None:
        pass

    def load(self) -> DataFrame[MyDataset]:
        df = self.source.load()
        return df[(df["feature"] >= self.feature_min) & (df["feature"] < self.feature_max)]
```

### `TargetTask[TargetType]` — Typed Target Output

`TargetTask[TargetType]` extends `BaseTask` with:

- `target() -> TargetType` — Returns a typed target (e.g., a file or remote storage).
- Auto-implements `complete()` as `self.target().exists()`.

This is useful when you need full control over the target type and path structure or using non-standard storage.

Note that `TargetTask` does **not** extend `LoadableTask`, so instances cannot be passed directly to `TaskLoads[T]` parameters. If you need both a custom target and composability via `TaskLoads`, inherit from both `TargetTask` and `LoadableTask` (diamond pattern), or use `Task` instead.

### `Task[T]` — The Recommended Default

`Task[T]` combines `TargetTask` and `LoadableTask` via diamond inheritance:

```{.python notest}
class Task(
    TargetTask[LoadableSaveableFileSystemTarget[T]],
    LoadableTask[T],
):
    ...
```

It provides:

- **Automatic filesystem target** — Output path derived from namespace, name, version, and ID.
- **Automatic serialization** — Serializer inferred from the type parameter `T`.
- **`load() -> T`** — Convenience method delegating to `self.target().load()`.
- **`_save(data: T)`** — Convenience method delegating to `self.target().save(data)`.

- **Composability** — Compatible with `TaskLoads[T]` since it extends `LoadableTask[T]`.

For most tasks, **`Task` is the right choice**. Use the other base classes only when you need to deviate from the default filesystem target behavior.

### Choosing the Right Base Class

| Base Class           | Use When                                                   |
| -------------------- | ---------------------------------------------------------- |
| `Task[T]`            | Default choice. Filesystem target with auto serialization. |
| `LoadableTask[T]`    | Custom `load()` without any target (DB, API, in-memory).   |
| `TargetTask[Target]` | Custom target type (non-filesystem, special path logic).   |
| `BaseTask`           | Full control. No target or load assumptions.               |

In the following section we will cover the fact that most tasks use `Target`s, and in particular `FileTarget`s and `DirectoryTarget`s, to persistently store their output and for downstream tasks to retrieve it as input.
