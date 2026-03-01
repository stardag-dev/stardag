# Release Notes

## Breaking: Task Class Hierarchy Rename + LoadableTask + TaskLoads Update

The task class hierarchy has been renamed for clarity and a new `LoadableTask` abstraction has been introduced for better composability.

### What Changed

| Before                                          | After                       | Description                                                 |
| ----------------------------------------------- | --------------------------- | ----------------------------------------------------------- |
| `AutoTask`                                      | `Task`                      | Auto filesystem targets + serialization (default)           |
| `Task`                                          | `TargetTask`                | Base class introducing typed `target()` targets             |
| `BaseTask`                                      | `BaseTask`                  | Unchanged - minimal core API                                |
| _(new)_                                         | `LoadableTask`              | Abstract base: `BaseTask` + `load() -> T`                   |
| `TaskLoads[T]` = `SubClass[Task[LoadableT[T]]]` | `SubClass[LoadableTask[T]]` | Now requires any `LoadableTask` subclass with matching type |
| `task.output()`                                 | `task.target()`             | Renamed for clarity: the target of a task                   |

### New: `LoadableTask[T]`

`LoadableTask[T]` is a new public abstraction that extends `BaseTask` with a single abstract method `load() -> T`. It is the minimal interface required for composability via `TaskLoads[T]`.

The class hierarchy is now:

```
BaseTask                         # Minimal: complete(), run(), requires()
├── LoadableTask[T]              # Adds abstract load() -> T
├── TargetTask[TargetType]       # Adds typed target() target
│
└── Task[T]                      # Diamond: extends both TargetTask and LoadableTask
    (TargetTask[LSFST[T]] + LoadableTask[T])
```

`Task[T]` uses diamond inheritance to extend both `TargetTask[LoadableSaveableFileSystemTarget[T]]` and `LoadableTask[T]`, so `Task` instances satisfy both interfaces.

### Convenience Methods on `Task`

`Task` gained `load()` and `_save()`, and `TaskLoads[T]` now resolves to `SubClass[LoadableTask[T]]` so `.load()` is available on dependency fields:

```python
# Before
class MyTask(sd.AutoTask[dict]):
    dep: sd.TaskLoads[list[int]]

    def run(self):
        data = self.dep.output().load()
        self.output().save({"sum": sum(data)})

# After
class MyTask(sd.Task[dict]):
    dep: sd.TaskLoads[list[int]]

    def run(self):
        data = self.dep.load()
        self._save({"sum": sum(data)})
```

### Bare `LoadableTask` for Custom Composability

You can now create tasks that are composable via `TaskLoads` without requiring a filesystem target:

```python
class MyCustomLoader(sd.LoadableTask[pd.DataFrame]):
    query: str

    def complete(self) -> bool:
        return True  # Always "complete" - loads from external source

    def run(self) -> None:
        pass  # No-op: data is loaded on demand

    def load(self) -> pd.DataFrame:
        return pd.read_sql(self.query, engine)

# Works with TaskLoads:
class Analysis(sd.Task[dict]):
    data: sd.TaskLoads[pd.DataFrame]  # Accepts MyCustomLoader or any Task[pd.DataFrame]

    def run(self):
        df = self.data.load()
        self._save({"rows": len(df)})
```

### Migration Guide

1. **Rename `AutoTask` to `Task`** everywhere:

   ```python
   # Before                          # After
   class MyTask(sd.AutoTask[int]):   class MyTask(sd.Task[int]):
       ...                               ...
   ```

2. **Rename `Task` to `TargetTask`** if you subclass it directly (with custom `target()`):

   ```python
   # Before                                    # After
   class MyTask(sd.Task[MyTarget]):             class MyTask(sd.TargetTask[MyTarget]):
       def output(self) -> MyTarget: ...            def target(self) -> MyTarget: ...
   ```

3. **`TaskLoads[T]` now requires `LoadableTask[T]`** (not `TargetTask`). Both `Task` and bare `LoadableTask` subclasses work. If you have a `TargetTask` subclass that needs to be passed as a dependency, use the explicit annotation:

   ```python
   # Use TaskLoads for most cases (Task and LoadableTask subclasses)
   dep: sd.TaskLoads[MyType]

   # For TargetTask subclasses (rare), use explicit annotation
   dep: sd.SubClass[sd.TargetTask[LoadableTarget[MyType]]]
   ```

4. **Rename `output()` to `target()`** on all task classes:

   ```python
   # Before                          # After
   task.output().load()              task.target().load()
   task.output().save(data)          task.target().save(data)
   def output(self) -> Target:       def target(self) -> Target:
   ```

5. **`@task` decorator is unchanged** — it generates `Task` instances.

6. **`BaseTask` is unchanged**.

### Quick Find-and-Replace

For most codebases:

```
sd.Task[    →  sd.TargetTask[     (only for custom target() subclasses)
sd.AutoTask →  sd.Task
.output()   →  .target()
```

Run the second replacement after the first to avoid conflicts.
