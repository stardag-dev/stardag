# `stardag` User Guide

For Python API level documentation, see source code. (-> TODO :))

See also `./examples` folder.

## Core Concepts

- Abstraction over the filesystem: The target location of any asset is deterministically determined by its input parameters, _before_ it has been executed.
- Each Asset has a self-contained representation of its entire upstream dependency tree -> great for reducing complexity and composability.
- `Makefile`/`luigi` style bottom up execution
- Typesafe/hints, leverage Python's ecosystem around types...

## The Three Levels of the Task-API

The following three ways of specifying a `root_task`, its _dependencies_, _persistent targets_ and _serialization_ are 100% equivalent:

### The Decorator (`@task`) API

```python
import stardag as sd

@sd.task(name="Range")
def get_range(limit: int) -> list[int]:
    return list(range(limit))

@sd.task(name="Sum")
def get_sum(integers: sd.Depends[list[int]]) -> int:
    return sum(integers)

root_task = get_sum(integers=get_range(limit=10))
```

### Extending `Task`

```python
import stardag as sd

class Range(sd.Task[list[int]]):
    limit: int

    def run(self):
        self._save(list(range(self.limit)))


class Sum(sd.Task[int]):
    integers: sd.TaskLoads[list[int]]

    def requires(self):
        return self.integers

    def run(self):
        self._save(sum(self.integers.load()))


root_task = Sum(integers=Range(limit=10))
```

### Extending the base `TargetBaseTask`

```python
import stardag as sd
from stardag.target import LoadableSaveableFileSystemTarget
from stardag.target.serialize import JSONSerializer, Serializable

def default_relpath(task: sd.TargetBaseTask) -> str:
    return "/".join(
        [
            task.get_name(),
            task.task_id[:2],
            task.task_id[2:4],
            f"{task.task_id}.json",
        ]
    )

class Range(sd.TargetBaseTask[LoadableSaveableFileSystemTarget[list[int]]]):
    limit: int

    def output(self) -> LoadableSaveableFileSystemTarget[list[int]]:
        return Serializable(
            wrapped=sd.get_target(default_relpath(self)),
            serializer=JSONSerializer(list[int]),
        )

    def run(self):
        self.output().save(list(range(self.limit)))

class Sum(sd.TargetBaseTask[LoadableSaveableFileSystemTarget[int]]):
    integers: sd.SubClass[sd.TargetBaseTask[LoadableSaveableFileSystemTarget[list[int]]]]

    def requires(self):
        return self.integers

    def output(self) -> LoadableSaveableFileSystemTarget[int]:
        return Serializable(
            wrapped=sd.get_target(default_relpath(self)),
            serializer=JSONSerializer(int),
        )

    def run(self):
        return self.output().save(sum(self.integers.output().load()))

root_task = Sum(integers=Range(limit=10))
```

In short:

- The decorator API can be used when defining a task for which all upstream dependencies are _injected_ as "task parameters". Sane defaults and type annotations are leverage to infer target location and serialization.
- `Task` should be used when upstream dependencies (output of `.requires()`) needs to be _computed_ based on task input parameters. Most things, like the target path, are still easily tweakable by overriding properties/methods of `Task`.
- The base `TargetBaseTask` should be used when we want full flexibility and/or use non-filesystem target (like a row in a DB for example).

## Filesystem Targets & Target Roots

In typical usage, most tasks will have their output saved to a filesystem; local disk or remote storage such as AWS S3 or Google Cloud Storage. This happens automatically when you use the [decorator API](#the-decorator-task-api) or extend [`Task`](#extending-task).

Each task only specifies its output location _relative to_ a (or multiple) globally configured _target root(s)_. To configure these, use the following environment variables:

```sh
export STARDAG_TARGET_ROOTS__DEFAULT=<abspath or URI>
export STARDAG_TARGET_ROOTS__OTHER=<abspath or URI>
```

or equivalent with JSON notation:

```sh
export STARDAG_TARGET_ROOTS='{"default": <abspath or URI>, "other": <abspath or URI>}'
```

Under the hood, target roots are managed by the global `stardag.target.TargetFactory` instance obtained by `stardag.target.target_factory_provider.get()`. For maximal flexibility you can instantiate a `TargetFactory` (or a custom subclass) explicitly and set it to `target_factory_provider.set(TargetFactory(target_roots={...}))`.

When you subclass `TargetBaseTask` directly (i.e. don't use [decorator API](#the-decorator-task-api) or extend [`Task`](#extending-task)) it is recommended to use `stardag.target.get_target(relpath=...)` to instantiate filesystem targets returned by `TargetBaseTask.output()`, this way the task specifies the _relative path_ to the configured target root:

```python fixture:default_in_memory_fs_target
import stardag as sd

class MyTask(sd.TargetBaseTask[sd.FileSystemTarget]):
    # ...
    def output(self):
        return sd.get_target(relpath="...")

    def run(self):
        with self.output().open("w") as handle:
            handle.write("result")
```

For special cases you can of course instantiate and return a `FileSystemTarget` such as `LocalTarget` or `RemoteFilesystemTarget` directly in which case the globally configured target roots have no effect.

### Switching Target Roots by "Environment"

A common use case for the globally configured target roots is to switch target filesystem depending on the environment. In local development you'd typically use a directory of choice on your local filesystem (or you could even set it by active git feature branch etc.). In testing you can setup pytest fixtures to use a temporary directory (separate for each test) or an in-memory filesystem (TODO document both), and in production you would typically select remote storage such as AWS S3:

```sh
export STARDAG_TARGET_ROOTS__DEFAULT="s3://my-bucket/stardag/root-default/"
```

### Serialization

...

## Parameter Hashing -> `task_id`

...

### Recursive Hashing of Tasks as Parameters

...

## Execution

...
