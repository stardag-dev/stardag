# Targets

Targets represent where and how task outputs are stored.

## The Typical `Task` uses a `Target`

In most scenarios, downstream tasks needs to load the output from upstream dependencies, for this purpose the class [`Task`](../reference/api.md#stardag.Task), which inherits [`BaseTask`](../reference/api.md#stardag.BaseTask) introduces the concept of `Target`s.

Its extension of [`BaseTask`](../reference/api.md#stardag.BaseTask) can be summarized in seven lines of code:

```{.python notest}
class Task(BaseTask, Generic[TargetType]):
    def complete(self) -> bool:
        return self.output().exists()

    @abstractmethod
    def output(self) -> TargetType:
        ...
```

That is, it adds a default implemention of `complete` which checks if the return value of the new (abstract) methods `output` exists. Generally, the _only_ requirement on the `TargetType` returned from output is that it can report its existance. Strictly speaking, that it implements the protocol:

```{.python notest}
class Target(Protocol):
    def exists(self) -> bool:
        """Check if the target exists."""
        ...
```

Note that [`Task`](../reference/api.md#stardag.Task) is implemented with `TargetType` as a _generic_ type variable. This means that when you subclass [`Task`](../reference/api.md#stardag.Task), you need to declare the type of target the `output` returns. This is critical for composability of tasks and allows typcheckers to verify that chained tasks are compatible in terms of their I/O.

## The Typical `Target` uses a File System

Moreover, the most commonly used `Target` persists, retrives and checks existance of one or many files/objects in a file system. To this end Stardag implements the [`FileSystemTarget`](../reference/api.md#stardag.FileSystemTarget).

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

However, you are strongly encouraged to instead use the function `sd.get_target`:

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

---

!!! info "🚧 **Work in progress** 🚧"

    This documentation is still taking shape. It should soon cover:

    - Serialization
    - How target roots are configured via the `STARDAG_PROFILE`/CLI/configuration
    - How target roots are connected to *Environments* and synced via the stardag Registry.
    - Common patterns and best practices.
