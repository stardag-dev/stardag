# Release Notes

## Breaking: Task Class Hierarchy Rename + TaskLoads Narrowing

The task class hierarchy has been renamed for clarity. The most commonly used class is now simply `Task`, matching the `@task` decorator and `TaskLoads` semantics.

### What Changed

| Before                                          | After               | Description                                        |
| ----------------------------------------------- | ------------------- | -------------------------------------------------- |
| `AutoTask`                                      | `Task`              | Auto filesystem targets + serialization (default)  |
| `Task`                                          | `TargetBaseTask`    | Base class introducing typed `output()` targets    |
| `BaseTask`                                      | `BaseTask`          | Unchanged - minimal core API                       |
| `TaskLoads[T]` = `SubClass[Task[LoadableT[T]]]` | `SubClass[Task[T]]` | Now requires `Task` subclass, not `TargetBaseTask` |

### Convenience Methods on `Task`

`Task` gained `load()` and `_save()`, and `TaskLoads[T]` now resolves to `SubClass[Task[T]]` so these methods are available on dependency fields too:

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

The `output().load()` / `output().save()` pattern still works but `load()` / `_save()` is preferred.

### Migration Guide

1. **Rename `AutoTask` to `Task`** everywhere:

   ```python
   # Before                          # After
   class MyTask(sd.AutoTask[int]):   class MyTask(sd.Task[int]):
       ...                               ...
   ```

2. **Rename `Task` to `TargetBaseTask`** if you subclass it directly (with custom `output()`):

   ```python
   # Before                                    # After
   class MyTask(sd.Task[MyTarget]):             class MyTask(sd.TargetBaseTask[MyTarget]):
       def output(self) -> MyTarget: ...            def output(self) -> MyTarget: ...
   ```

3. **`TaskLoads[T]` now requires `Task` subclasses** (not bare `TargetBaseTask`). If you pass a `TargetBaseTask` subclass as a dependency, use the explicit annotation instead:

   ```python
   # Before (worked with TargetBaseTask)
   dep: sd.TaskLoads[MyType]

   # After (if the dependency is a TargetBaseTask, not a Task)
   dep: sd.SubClass[sd.TargetBaseTask[LoadableTarget[MyType]]]
   ```

   Most users won't need this — if your dependencies are `Task` or `@task` based, `TaskLoads[T]` works unchanged.

4. **`@task` decorator is unchanged** — it generates `Task` instances.

5. **`BaseTask` is unchanged**.

### Quick Find-and-Replace

For most codebases:

```
sd.Task[    →  sd.TargetBaseTask[     (only for custom output() subclasses)
sd.AutoTask →  sd.Task
```

Run the second replacement after the first to avoid conflicts.
