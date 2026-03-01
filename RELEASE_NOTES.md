# Release Notes

## v0.3.0 — Breaking: Task Class Hierarchy Rename + LoadableTask + TaskLoads Update

The task class hierarchy has been renamed for clarity and a new `LoadableTask` abstraction has been introduced for better composability.

### What Changed

| Before                                               | After                       | Description                                                 |
| ---------------------------------------------------- | --------------------------- | ----------------------------------------------------------- |
| `AutoTask`                                           | `Task`                      | Auto filesystem targets + serialization (default)           |
| `Task`                                               | `TargetTask`                | Base class introducing typed `target()` targets             |
| `BaseTask`                                           | `BaseTask`                  | Unchanged - minimal core API                                |
| _(new)_                                              | `LoadableTask`              | Abstract base: `BaseTask` + `load() -> T`                   |
| `TaskLoads[T]` = `SubClass[Task[LoadableTarget[T]]]` | `SubClass[LoadableTask[T]]` | Now requires any `LoadableTask` subclass with matching type |
| `task.output()`                                      | `task.target()`             | Renamed for clarity: the target of a task                   |
| `_FunctionTask.result()`                             | _(removed)_                 | Use inherited `load()` instead                              |

### New: `LoadableTask[T]`

`LoadableTask[T]` is a new public abstraction that extends `BaseTask` with a single abstract method `load() -> T`. It is the minimal interface required for composability via `TaskLoads[T]`.

The class hierarchy is now:

```
          BaseTask                    # complete(), run(), requires()
         /        \
LoadableTask[T]    TargetTask[TT]    # load() -> T  /  target() -> TT
         \        /
          Task[T]                    # Combines both (TT = LSFST[T])
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

### `@task` Decorator: New `target_root_key` Parameter

The `@task` decorator gained a `target_root_key` parameter to control which target root from config is used for output:

```python
@sd.task(target_root_key="s3")
def my_task(data: sd.Depends[list[int]]) -> int:
    return sum(data)
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

5. **Replace `.result()` with `.load()`** on `@task`-created instances:

   ```python
   # Before                          # After
   my_task.result()                  my_task.load()
   ```

6. **`BaseTask` is unchanged**.

### Quick Find-and-Replace

For most codebases:

```
sd.Task[    →  sd.TargetTask[     (only for custom target() subclasses)
sd.AutoTask →  sd.Task
.output()   →  .target()
.result()   →  .load()
```

Run the first replacement before the second to avoid conflicts.
