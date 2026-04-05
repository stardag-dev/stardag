# Release Notes

Release notes for the **stardag SDK** (`pip install stardag`). These cover significant changes and migration guides for SDK versions published to PyPI.

For changes to the Registry API, UI, and other components, see [CHANGELOG.md](CHANGELOG.md).

---

## v0.5.2 — Configuration cleanup and auth token auto-refresh

This release restructures the configuration system and adds automatic JWT token
refresh during builds. **Breaking changes are limited to the configuration/auth
layer** — the core SDK for defining tasks, building DAGs, and working with
targets is completely unaffected.

### Breaking changes

If you access `StardagConfig` fields programmatically, the following paths changed:

| Before                          | After                                                       |
| ------------------------------- | ----------------------------------------------------------- |
| `config.api.url`                | `config.registry.url` (check `config.registry is not None`) |
| `config.api.timeout`            | `config.registry.timeout`                                   |
| `config.context.workspace_id`   | `config.registry.workspace_id`                              |
| `config.context.environment_id` | `config.registry.environment_id`                            |
| `config.context.user`           | `config.registry.auth.user_email`                           |
| `config.access_token`           | `config.registry.auth.access_token`                         |
| `config.api_key`                | `config.registry.auth.api_key`                              |
| `config.context.profile`        | `config.context.profile` (unchanged)                        |
| `config.context.registry_name`  | `config.context.registry_name` (unchanged)                  |

`config.registry` is `None` when no registry is configured (offline/local mode).

Removed symbols: `APIConfig`, `ContextConfig`, `DEFAULT_API_URL`.

`RegistryConfig` repurposed: was `RegistryConfig(url: str)` (TOML entry), now
`RegistryConfig(url, workspace_id, environment_id, auth, timeout)` (runtime config).
TOML registry entries are plain `dict[str, str]` in `TomlConfig`.

The `config/__init__.py` public API is now explicit. Internal symbols must be
imported from submodules: `from stardag.config.cache import _looks_like_uuid`.

### New: auto-refresh JWT tokens

`APIRegistry` now transparently refreshes expired JWT tokens before each API
request. This fixes a bug where long-running builds with browser-login auth
would fail when the short-lived token expired mid-execution.

### New: STARDAG_NO_REGISTRY

Set `STARDAG_NO_REGISTRY=1` to force offline/local mode. The registry provider
returns `NoOpRegistry` and `config.registry` is `None`.

---

## v0.5.1 — Automatic version field default

Small quality-of-life improvement: the `version` instance field on task classes now
automatically defaults to `cls.__version__`, eliminating the boilerplate
`version: str = __version__` that previously had to be repeated in every versioned
task subclass.

**Before:**

```python
class MyTask(sd.Task[int]):
    __version__ = "1"
    version: str = __version__  # ← required boilerplate
```

**After:**

```python
class MyTask(sd.Task[int]):
    __version__ = "1"  # version field defaults automatically
```

Existing code that already declares `version: str = __version__` continues to work
without any changes. Stored/serialized tasks are also unaffected — explicit `version`
values are always preserved.

---

## v0.5.0 — LoadValidator, Test Harness, and Build System Robustness

This release introduces load-time validation, a testing utility, and significant build system improvements. **No breaking changes** — all additions are backward-compatible.

### New: `LoadValidator[T]`

Validators that run automatically when data passes through `Task._save()` and `Task.load()`. Attach them via `typing.Annotated`, following the same pattern as serializers. Validators can reject (raise) or transform (return modified value), and multiple validators chain left-to-right.

```python
import typing
import stardag as sd

class NonEmpty(sd.LoadValidator[list]):
    def validate(self, value: list) -> list:
        if not value:
            raise ValueError("List must not be empty")
        return value

class MyTask(sd.Task[typing.Annotated[list[int], NonEmpty()]]):
    def run(self):
        self._save([1, 2, 3])  # validated before saving

# Also works with @task decorator
@sd.task
def my_task() -> typing.Annotated[list[int], NonEmpty()]:
    return [1, 2, 3]
```

**Attribute-based escape hatch**: For cases where subclassing `LoadValidator` causes MRO conflicts, any class with `stardag_load_validator = True` and a `validate()` method is also discovered.

### New: `test_harness`

A context manager in `stardag.testing` that sets up an isolated test environment with temporary target root directories and a `NoOpRegistry`:

```python
from stardag.testing import test_harness

def test_my_pipeline():
    with test_harness():
        task = MyTask(param="value")
        task.complete()
        result = task.load()
        assert result == expected
```

### New: `get_default_relpath()`

Standalone public utility for constructing default task output relpaths. Previously this logic was internal to `Task._relpath`:

```python
import stardag as sd

relpath = sd.get_default_relpath(task, extension=".json")
```

### New: `BuildSummary.raise_on_failure()`

Raises a new `BuildFailed` exception (with `.summary` attribute) when the build status is `FAILURE`:

```python
from stardag import build

summary = build([my_task])
summary.raise_on_failure()  # raises BuildFailed if any task failed
```

### Build System Improvements

- **`on_registry_failure` parameter** on all build functions (`build`, `build_aio`, `build_sequential`, `build_sequential_aio`) — `"warn"` (default) or `"raise"` to control registry error handling.
- **`register_all` flag** — opt-in full DAG registration, ensuring all tasks (including already-complete dependencies) are registered in the registry for complete graph visibility.
- **FAIL_FAST fix**: Task exceptions now properly propagate to the caller in both sequential and concurrent builds.
- **Deadlock detection** in sequential builds.
- **`TaskExecutionError`**: Wraps executor exceptions with pre-formatted tracebacks for better debugging across thread/process boundaries.
- **Commit hash traceability**: All task/build lifecycle events include the git commit hash in metadata.

### Other Improvements

- All serializers are now hashable (Pydantic generic cache compatibility with `Annotated` types).
- `TaskLoads[Annotated[T, ...]]` validation fixed — `Annotated` wrappers are now stripped in type compatibility checks.
- `Task.from_registry(id)` accepts `str | UUID`.
- `artifacts()` / `artifacts_aio()` return `Sequence` instead of `list`.
- `ResourceProvider.is_initialized()` added.

---

## v0.4.0 — Breaking: Target & Serializer Type Hierarchy Restructure

The target and serializer type hierarchies have been restructured to cleanly support both file and directory targets through a unified `Task` interface.

### Rationale

The previous hierarchy had `FileSystemTarget` serving double duty — it was both the minimal base protocol and the full file-oriented target. This made it impossible to properly type directory targets within the same hierarchy. The restructure introduces a clear separation: `FileSystemTarget` (minimal base with `uri` + `exists()`) → `FileTarget` (file I/O) / `DirectoryTarget` (directory of sub-targets).

### What Changed

#### Target Renames

| Before                         | After                          | Description                                                                                           |
| ------------------------------ | ------------------------------ | ----------------------------------------------------------------------------------------------------- |
| `FileSystemTarget` (old, full) | `FileTarget`                   | File-oriented target with `open()`, `proxy_path()`, etc.                                              |
| _(new)_                        | `FileSystemTarget`             | Minimal base protocol: `uri` + `exists()`. Both `FileTarget` and `DirectoryTarget` inherit from this. |
| `RemoteFileSystemTarget`       | `RemoteFileTarget`             | Remote file target (S3, Modal volumes, etc.)                                                          |
| `InMemoryFileSystemTarget`     | `InMemoryFileTarget`           | In-memory file target for testing                                                                     |
| `LocalTarget`                  | `LocalFileTarget`              | Local filesystem file target                                                                          |
| `ModalMountedVolumeTarget`     | `ModalMountedVolumeFileTarget` | Modal mounted volume file target                                                                      |
| `_FileSystemTargetGeneric`     | `_FileTargetGeneric`           | Internal generic base for file targets                                                                |
| `_FSTargetType`                | `_FileTargetType`              | Internal TypeVar for file target types                                                                |

#### Serializer Changes

| Before                | After                          | Description                                       |
| --------------------- | ------------------------------ | ------------------------------------------------- |
| `Serializer[LoadedT]` | `Serializer[LoadedT, TargetT]` | Now parameterized by target type                  |
| _(new)_               | `FileSerializer[LoadedT]`      | Alias for `Serializer[LoadedT, FileTarget]`       |
| _(new)_               | `DirectorySerializer[LoadedT]` | Alias for `Serializer[LoadedT, DirectoryTarget]`  |
| `Serializable`        | `FileSerializable`             | File target + serializer wrapper                  |
| _(new)_               | `DirectorySerializable`        | Directory target + serializer wrapper             |
| `SelfSerializing`     | `SelfFileSerializing`          | Protocol for file self-serializers                |
| `SelfSerializer`      | `SelfFileSerializer`           | Serializer for `SelfFileSerializing` objects      |
| _(new)_               | `SelfDirectorySerializing`     | Protocol for directory self-serializers           |
| _(new)_               | `SelfDirectorySerializer`      | Serializer for `SelfDirectorySerializing` objects |

#### Factory & Helper Renames

| Before                        | After                             | Description                             |
| ----------------------------- | --------------------------------- | --------------------------------------- |
| `get_target()` (module-level) | `get_file_target()`               | Get a file target for a relative path   |
| `TargetFactory.get_target()`  | `TargetFactory.get_file_target()` | Method on factory class                 |
| `TargetPrototype`             | `FileTargetPrototype`             | Type alias for file target constructors |

### New: Directory Serializer Support in `Task`

`Task[T]` now automatically detects directory serializers and creates the appropriate target type:

```python
from typing import Annotated
import stardag as sd
from stardag.target import DirectoryTarget

class MyDirectorySerializer:
    target_type = DirectoryTarget

    def dump(self, obj, target: DirectoryTarget) -> None:
        with (target / "data.json").open("w") as f:
            f.write(json.dumps(obj))
        target.mark_done()

    def load(self, target: DirectoryTarget):
        with (target / "data.json").open("r") as f:
            return json.loads(f.read())

# Task automatically creates a DirectoryTarget
class MyTask(sd.Task[Annotated[dict, MyDirectorySerializer()]]):
    def run(self):
        self._save({"key": "value"})
```

### Migration Guide

1. **`FileSystemTarget` → `FileTarget`** in type annotations for file-oriented targets:

   ```python
   # Before
   class MyTask(sd.TargetTask[sd.FileSystemTarget]):
       def target(self) -> sd.FileSystemTarget:
           return sd.get_target("path.txt")

   # After
   class MyTask(sd.TargetTask[sd.FileTarget]):
       def target(self) -> sd.FileTarget:
           return sd.get_file_target("path.txt")
   ```

2. **`get_target()` → `get_file_target()`**:

   ```python
   # Before
   sd.get_target("path/file.json")

   # After
   sd.get_file_target("path/file.json")
   ```

3. **`Serializable` → `FileSerializable`**:

   ```python
   # Before
   from stardag.target.serialize import Serializable
   Serializable(wrapped=target, serializer=s)

   # After
   from stardag.target.serialize import FileSerializable
   FileSerializable(wrapped=target, serializer=s)
   ```

4. **`SelfSerializing` → `SelfFileSerializing`**, **`SelfSerializer` → `SelfFileSerializer`**

5. **`RemoteFileSystemTarget` → `RemoteFileTarget`**, **`InMemoryFileSystemTarget` → `InMemoryFileTarget`**

6. **`LocalTarget` → `LocalFileTarget`**, **`ModalMountedVolumeTarget` → `ModalMountedVolumeFileTarget`**

### Quick Find-and-Replace

```
sd.FileSystemTarget  →  sd.FileTarget         (for file-oriented targets)
sd.get_target(       →  sd.get_file_target(
Serializable(        →  FileSerializable(
SelfSerializing      →  SelfFileSerializing
SelfSerializer       →  SelfFileSerializer
RemoteFileSystemTarget  →  RemoteFileTarget
InMemoryFileSystemTarget  →  InMemoryFileTarget
LocalTarget          →  LocalFileTarget
ModalMountedVolumeTarget  →  ModalMountedVolumeFileTarget
```

---

## v0.3.0 — Breaking: Task Class Hierarchy Rename + LoadableTask + TaskLoads Update

The task class hierarchy has been renamed for clarity and a new `LoadableTask` abstraction has been introduced for better composability.

### Rationale

- **`LoadableTask` / adding `load()` to the task itself**: For downstream tasks, we only care about _what type is loaded_, not what type the `Target` has beyond that. In some cases, it is convenient not to implement a Target at all.
- **`output()` renamed to `target()`**: `output()` was taken from Luigi, where it was paired with `input()` (which mapped each dependency's `output()` into a corresponding struct). In Luigi, the Target was the only first-class representation of a task's result, so the naming made sense. In Stardag, the _loaded type_ is the primary result of a task — it's what powers type-hinted composability and why we don't have a Luigi-style `input()`. Given that, `output()` is confusingly close to the concept of "the task's result", while `target()` maps 1:1 to the data type it returns.
- **`AutoTask` renamed to `Task`**: This should be the default choice for most users, and now `Task` maps naturally to the `@task` decorator.

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
