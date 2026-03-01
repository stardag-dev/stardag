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
BaseTask                         # Minimal: complete(), run(), requires()
├── LoadableTask[T]              # Adds: abstract load() -> T
├── TargetBaseTask[TargetType]   # Adds: typed output() target, auto complete()
│
└── Task[T]                      # Diamond: both TargetBaseTask + LoadableTask
    (TargetBaseTask[LSFST[T]] + LoadableTask[T])
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

```{.python notest}
class MyCustomLoader(sd.LoadableTask[pd.DataFrame]):
    query: str

    def complete(self) -> bool:
        return True  # Always available

    def run(self) -> None:
        pass  # No-op: data is loaded on demand

    def load(self) -> pd.DataFrame:
        return pd.read_sql(self.query, engine)


class Analysis(sd.Task[dict]):
    data: sd.TaskLoads[pd.DataFrame]  # Accepts MyCustomLoader or Task[pd.DataFrame]

    def run(self):
        df = self.data.load()  # Works regardless of the source
        self._save({"rows": len(df)})
```

Use `LoadableTask` when your task produces a typed output but doesn't use a filesystem target — for example, loading from a database, an API, or an in-memory computation.

### `TargetBaseTask[TargetType]` — Typed Target Output

`TargetBaseTask[TargetType]` extends `BaseTask` with:

- `output() -> TargetType` — Returns a typed target (e.g., a file or remote storage).
- Auto-implements `complete()` as `self.output().exists()`.

This is useful when you need full control over the target type and path structure, such as writing to a database, a custom file format, or non-standard storage.

Note that `TargetBaseTask` does **not** extend `LoadableTask`, so instances cannot be passed directly to `TaskLoads[T]` parameters. If you need both a custom target and composability via `TaskLoads`, inherit from both `TargetBaseTask` and `LoadableTask` (diamond pattern), or use `Task` instead.

### `Task[T]` — The Recommended Default

`Task[T]` combines `TargetBaseTask` and `LoadableTask` via diamond inheritance:

```{.python notest}
class Task(
    TargetBaseTask[LoadableSaveableFileSystemTarget[T]],
    LoadableTask[T],
):
    ...
```

It provides:

- **Automatic filesystem target** — Output path derived from namespace, name, version, and ID.
- **Automatic serialization** — Serializer inferred from the type parameter `T`.
- **`load() -> T`** — Convenience method delegating to `self.output().load()`.
- **`_save(data: T)`** — Convenience method delegating to `self.output().save(data)`.
- **Composability** — Compatible with `TaskLoads[T]` since it extends `LoadableTask[T]`.

For most tasks, **`Task` is the right choice**. Use the other base classes only when you need to deviate from the default filesystem target behavior.

### Choosing the Right Base Class

| Base Class               | Use When                                                   |
| ------------------------ | ---------------------------------------------------------- |
| `Task[T]`                | Default choice. Filesystem target with auto serialization. |
| `LoadableTask[T]`        | Custom `load()` without any target (DB, API, in-memory).   |
| `TargetBaseTask[Target]` | Custom target type (non-filesystem, special path logic).   |
| `BaseTask`               | Full control. No target or load assumptions.               |

In the following section we will cover the fact that most tasks use `Target`s, and in particular `FileSystemTarget`s, to persistently store their output and for downstream tasks to retrieve it as input.
