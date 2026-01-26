# Targets

Targets represent where and how task outputs are stored.

## The Typical `Task` uses `Target`s

In most scenarios, downstream tasks needs to load the output from upstream dependencies, for this purpose the class `Task`, which inherits `BaseTask` introduces the concept of `Target`s.

Its extension of `BaseTask` can be summarized in seven lines of code:

```{.python notest}
class Task(BaseTask, Generic[TargetType]):
    def complete(self) -> bool:
        return self.output().exists()

    @abstractmethod
    def output(self) -> TargetType:
        ...
```

That is, it adds a default implemention of `complete` which checks if the return value of the new (abstract) methods `output` exists. Generally, the _only_ requirement on the `TargetType` returned from output is that it can report its existance. Strictly speaking, that is implements the protocol:

```{.python notest}
class Target(Protocol):
    def exists(self) -> bool:
        """Check if the target exists."""
        ...
```

Note that `Task` is implemented with `TargetType` as a _generic_ type variable. This means that when you subclass `Task`, you need to declare the type of target the `output` returns. This is critical for composability of tasks and allows typcheckers to verify that chained tasks are compatible in terms of their I/O.

## The Typical `Target` uses a File System

Moreover, the most commonly used `Target` persists, retrives and checks existance of one or many files/objects in a file system. To this end Stardag implements the `FileSystemTarget`.

You can, and it is in some cases motivated to, return a target for a certain type of file system with an absolute path/URI

```python
import stardag as sd

class MyTask(sd.Task[sd.LocalTarget]):

    def run(self):
        with self.output().open("w") as handle:
            handle.write("result")

    def output(self) -> sd.LocalTarget:
        return sd.LocalTarget("/absolute/path/to/file.txt")
```

However, you are strongly encouraged to instead use the methos `sd.get_target`:

```python
import stardag as sd

class MyTask(sd.Task[sd.FileSystemTarget]):

    def run(self):
        with self.output().open("w") as handle:
            handle.write("result")

    def output(self) -> sd.FileSystemTarget:
        return sd.get_target("path/to/file.txt")
```

The main benifit here is that you can configure the file system and root directory/URI-prefix _centrally and decoupled_ from your tasks. This means for example that you can trivially jump between experimenting fully locally and running pipelines in production (or staging etc.).

## Target Roots

Target roots define the base location for `FileSystemTargets` obtained by `sd.get_target()` or when using the `sd.AutoTask` or the `@sd.task` decorator API.

As per the last example above, when implementing `output`, you are advised to use `sd.get_target` which takes the arguments:

- `relpath: str` (required)
- `target_root_key: str` (default value `"default"`)

The core idea is that you define a set of named (the `target_root_key`) _base_ paths or URIs, to which the target of any task specifies the _relative_ path. E.g

### Configuration

Via environment variables:

```bash
# The "default" root
export STARDAG_TARGET_ROOT__DEFAULT="s3://some-bucket/prefix/"

# Additional root named "ingestion"
export STARDAG_TARGET_ROOT__INGESTION="s3://some-other-bucket/prefix/"
```

Or equivalently via a single JSON-encoded environment variable:

```bash
export STARDAG_TARGET_ROOTS=\
    '{"default": "s3://some-bucket/prefix/", "ingestion": "s3://some-other-bucket/prefix/"}'
```

You can also instantiate/implement your own `TargetFactory` and set it to `sd.target_factory_provider.set(my_target_factory)`.

!!! info "🚧 **Work in progress** 🚧"

    This documentation is still taking shape. It should soon cover:

    - How target roots are configured via the `STARDAG_PROFILE`/CLI/configuration
    - How target roots are connected to *Environments* and synced via the stardag Registry.
    - Common patterns and best practices.
    - Serialization

<!--
## What is a Target?

A target is:

- A **storage location** for task output
- An abstraction over **different backends** (local, S3, in-memory)
- A wrapper providing **serialization** (JSON, pickle, etc.)

## Target Types

### FileSystemTarget

The most common target type - files on a filesystem:

```python
from stardag.target import FileSystemTarget

target = sd.get_target("path/to/output.json", task=self)
```

### LoadableSaveableTarget

Targets with serialization support:

```python
from stardag.target import LoadableSaveableFileSystemTarget

# Automatically handles JSON serialization
target: LoadableSaveableFileSystemTarget[list[int]]
target.save([1, 2, 3])
data = target.load()  # [1, 2, 3]
```

### RemoteFilesystemTarget

For cloud storage like S3:

```python
# Configured via target roots
# s3://bucket/path/to/output.json
```

### InMemoryTarget

For testing:

```python
from stardag.target import InMemoryTarget

target = InMemoryTarget()
target.save(data)
```

## Target Roots

Target roots define the base location for task outputs.

### Configuration

Via environment variables:

```bash
# Single root
export STARDAG_TARGET_ROOT__DEFAULT=/path/to/outputs

# Multiple roots
export STARDAG_TARGET_ROOT__DEFAULT=/local/path
export STARDAG_TARGET_ROOT__S3=s3://bucket/prefix/
```

Or via JSON:

```bash
export STARDAG_TARGET_ROOTS='{"default": "/local/path", "s3": "s3://bucket/"}'
```

### Using Target Roots

Tasks specify relative paths; target roots provide the base:

```python
# Task outputs to: <target_root>/my_task/ab/cd/abcd1234.json
sd.get_target("my_task/ab/cd/abcd1234.json", task=self)
```

### Switching Environments

A common pattern is different roots per environment:

```bash
# Development (local)
export STARDAG_TARGET_ROOT__DEFAULT=~/.stardag/outputs

# Testing (temporary)
export STARDAG_TARGET_ROOT__DEFAULT=/tmp/stardag-test

# Production (S3)
export STARDAG_TARGET_ROOT__DEFAULT=s3://my-bucket/stardag/
```

## Serialization

Targets support different serialization strategies:

### JSON (default)

```python
@sd.task
def get_data() -> list[int]:  # JSON-serializable type
    return [1, 2, 3]
```

### Pickle

For complex Python objects:

```python
from stardag.target.serialize import PickleSerializer

# Used automatically for non-JSON types
@sd.task
def get_model() -> MyModel:
    return trained_model
```

### Custom Serializers -->

<!-- TODO: Document custom serializer implementation -->
<!--
## Target Operations

### Checking Existence

```python
if task.output().exists():
    print("Output already exists")
```

### Complete Check

```python
if task.complete():  # Calls output().exists()
    print("Task is complete")
```

### Loading and Saving

```python
# Save
task.output().save(result)

# Load
data = task.output().load()
```

### Path Access

```python
path = task.output().path
# /home/user/.stardag/outputs/my_task/ab/cd/abcd1234.json
```

## Advanced: TargetFactory

For programmatic control over target creation:

```python
from stardag.target import TargetFactory, target_factory_provider

# Create custom factory
factory = TargetFactory(
    target_roots={
        "default": "/path/to/default",
        "archive": "s3://archive-bucket/",
    }
)

# Set as global provider
target_factory_provider.set(factory)
``` -->

<!-- TODO: Document more TargetFactory options and patterns -->
