# `stardag` User Guide

For Python API level documentation, see source code. (-> TODO :))

See also `./examples` folder.

## Core Concepts

- Abstraction over the filesystem: The target location of any asset is deterministically determined by its input parameters, _before_ it has been executed.
- Each Asset has a self-contained representation of its entire upstream dependency tree -> great for reducing complexity and composability.
- Declarative: Concurrency and execution can be planned separately. has its limitations, but no framework gives it a ambitious go...
- `Makefile`/`luigi` style bottom up execution
- Typesafe/hints, leverage pythons ecosystem around types...

## The Three Levels of the Task-API

The following three ways of specifying a `root_task`, its _dependencies_, _persistent targets_ and _serialization_ are 100% equivalent:

### The Decorator (`@task`) API

```python
import stardag as sd

@sd.task(family="Range")
def get_range(limit: int) -> list[int]:
    return list(range(limit))

@sd.task(family="Sum")
def get_sum(integers: sd.Depends[list[int]]) -> int:
    return sum(integers)

root_task = get_sum(integers=get_range(limit=10))
```

### Extending the `AutoTask`

```python
import stardag as sd

class Range(sd.AutoTask[list[int]]):
    limit: int

    def run(self):
        self.output().save(list(range(self.limit)))


class Sum(sd.AutoTask[int]):
    integers: sd.TaskLoads[list[int]]

    def requires(self):
        return self.integers

    def run(self):
        self.output().save(sum(self.integers.output().load()))


root_task = Sum(integers=Range(limit=10))
```

### Extending the base `Task`

```python
import stardag as sd
from stardag.target import LoadableSaveableFileSystemTarget
from stardag.target.serialize import JSONSerializer, Serializable

def default_relpath(task: sd.Task) -> str:
    return "/".join(
        [
            task.get_family(),
            task.task_id[:2],
            task.task_id[2:4],
            f"{task.task_id}.json",
        ]
    )

class Range(sd.Task[LoadableSaveableFileSystemTarget[list[int]]]):
    limit: int

    def output(self) -> LoadableSaveableFileSystemTarget[list[int]]:
        return Serializable(
            wrapped=sd.get_target(default_relpath(self), task=self),
            serializer=JSONSerializer(list[int]),
        )

    def run(self):
        self.output().save(list(range(self.limit)))

class Sum(sd.Task[LoadableSaveableFileSystemTarget[int]]):
    integers: sd.TaskLoads[list[int]]

    def requires(self):
        return self.integers

    def output(self) -> LoadableSaveableFileSystemTarget[int]:
        return Serializable(
            wrapped=sd.get_target(default_relpath(self), task=self),
            serializer=JSONSerializer(int),
        )

    def run(self):
        return self.output().save(sum(self.integers.output().load()))

root_task = Sum(integers=Range(limit=10))
```

In short:

- The decorator API can be used when defining a task for which all upstream dependencies are _injected_ as "task parameters". Sane defaults and type annotations are leverage to infer target location and serialization.
- The `AutoTask` should be used when upstream dependencies (output of `.requires()`) needs to be _computed_ based on task input parameters. Most things, like the target path, are still easily tweakable by overriding properties/methods of the `AutoTask`.
- The base `Task` should be used when we want full flexibility and/or use non-filesystem target (like a row in a DB for example).

## Filesystem Targets & Target Roots

In typicall usage, most task will have their output saved to a filesystem; local disk or remote storage such as AWS S3 or Google Cloud Storage. This happens automatically when you use the [decorator API](#the-decorator-task-api) or extending the [`AutoTask`](#extending-the-AutoTask-auto-file-system-target-task).

Each task only specifies its output location _relative to_ a (or multiple) globaly configured _target root(s)_. To configure these, use the following environment variables:

```sh
export STARDAG_TARGET_ROOT__DEFAULT=<abspath or URI>
export STARDAG_TARGET_ROOT__OTHER=<abspath or URI>
```

or equivalent with JSON notation:

```sh
export STARDAG_TARGET_ROOT='{"default": <abspath or URI>, "other": <abspath or URI>}'
```

Under the hood, target roots are managed by the global `stardag.target.TargetFactory` instance obtained by `stardag.target.target_factory_provider.get()`. For maximal flexibility you can instantiate a `TargetFactory` (or a custom subclass) explicitly and set it to `target_factory_provider.set(TargetFactory(target_roots={...}))`.

Whe you subclass `Task` directly (i.e. don't use [decorator API](#the-decorator-task-api) or extends the [`AutoTask`](#extending-the-AutoTask-auto-file-system-target-task)) it is recommended to use `stardag.target.get_target(relpath=...)` to instantiate filesystem targets returned by `Task.output()`, this way the task specifes the _relative path_ to the configured target root:

```python
import stardag as sd
# ...

class MyTask(sd.Task[sd.FileSystemTarget]):
    # ...
    def output(self):
        return sd.get_target(relpath="...", task=self)

```

For special cases you can of course instantiate and return a `FileSystemTarget` such as `LocalTarget` or `RemoteFilesystemTarget` directly in which case the globaly configured target roots have no effect.

### Switching Target Roots by "Environment"

A common use case for the globaly configured target roots is to switch target filesystem depending on the environemt. In local development you'd typically use a directory of choice on your local filesystem (or you could even set it by active git feature branch etc.). In testing you can setup pytest fixtures to use a temporary directory (separate for each test) or an in-memory filesystem (TODO document both), and in production you would typically select remote storage such as AWS S3:

```sh
export STARDAG_TARGET_ROOT__DEFAULT="s3://my-bucket/stardag/root-default/"
```

### Serialization

...

## Parameter Hashing -> `task_id`

...

### Recursive Hashing of Tasks as Parameters

...

## Execution

...
