# SDK Targets: Persistence, Serialization & Storage

## Target Hierarchy

Targets are the persistence layer. They define where and how task outputs are stored.

```
LoadableTarget[T]          — Can load data of type T
SaveableTarget[T]          — Can save data of type T
FileSystemTarget           — Has a path property (local or remote)
FileTarget                 — Single file target
DirectoryTarget            — Directory-based target
LocalFileTarget            — Local filesystem
RemoteFileTarget           — Remote (S3, etc.)
```

## Using Task[T] (Automatic Targets)

When using `sd.Task[T]`, targets and serialization are handled automatically:

```python
class MyTask(sd.Task[dict[str, float]]):
    param: int

    def run(self):
        self._save({"result": self.param * 2.0})
```

- `self._save(data)` → serializes and writes to auto-configured target
- `self.load()` → reads and deserializes from target
- `self.complete()` → checks if target file exists
- Path: `<target_root>/<namespace>/<name>/v<version>/<id[0:2]>/<id[2:4]>/<id>.<ext>`

### Custom Relative Path

Override the default output path:

```python
class MyTask(sd.Task[dict]):
    date: str

    @property
    def _relpath_extra(self) -> str:
        """Inserted between name and id in the path."""
        return self.date

# Output: <target_root>/<namespace>/MyTask/v<version>/2024-01-15/<id[0:2]>/<id[2:4]>/<id>.json
```

### get_default_relpath (Standalone Utility)

Construct the default task output relpath without a Task subclass:

```python
relpath = sd.get_default_relpath(task, extension=".json")
# Equivalent to what Task._relpath computes internally
```

With the decorator API:

```python
@sd.task(relpath=lambda task: f"custom/{task.id}.json")
def my_task(param: int) -> dict:
    return {"result": param}
```

## Explicit Targets (TargetTask API)

For full control over target configuration:

```python
from stardag.target import LoadableSaveableFileSystemTarget
from stardag.target.serialize import FileSerializable, JSONSerializer

class CustomTask(sd.TargetTask[LoadableSaveableFileSystemTarget[list[int]]]):
    param: int

    def target(self) -> LoadableSaveableFileSystemTarget[list[int]]:
        return FileSerializable(
            wrapped=sd.get_file_target(f"custom-output/{self.id}.json"),
            serializer=JSONSerializer(list[int]),
        )

    def run(self):
        self.target().save(list(range(self.param)))
```

## Target Factories

`sd.get_file_target()` and `sd.get_directory_target()` create targets resolved against configured target roots:

```python
# File target (single file)
target = sd.get_file_target("my-namespace/output.json")

# Directory target
target = sd.get_directory_target("my-namespace/model-dir/")

# With specific target root key
target = sd.get_file_target("output.json", target_root_key="ingestion")
```

## Serializers

Built-in serializers in `stardag.target.serialize`:

| Serializer                     | Types                                              | Extension |
| ------------------------------ | -------------------------------------------------- | --------- |
| `JSONSerializer(T)`            | int, str, float, bool, list, dict, Pydantic models | `.json`   |
| `PickleSerializer(T)`          | Any Python object                                  | `.pkl`    |
| `PandasDataFrameCSVSerializer` | `pd.DataFrame`                                     | `.csv`    |

All built-in serializers are hashable, so they can be used in `Annotated` type parameters with Pydantic generics (e.g., `Task[Annotated[dict, JSONSerializer(dict)]]`).

### Custom Serializer

```python
from stardag.target.serialize import FileSerializable

target = FileSerializable(
    wrapped=sd.get_file_target("output.json"),
    serializer=JSONSerializer(MyPydanticModel),
)
```

### Automatic Type → Serializer Mapping

When using `sd.Task[T]`, the serializer is auto-selected:

- `Task[int]`, `Task[str]`, `Task[list[...]]`, `Task[dict[...]]` → JSON
- `Task[pd.DataFrame]` → Pandas CSV
- `Task[MyBaseModel]` → JSON (Pydantic serialization)
- `Task[CustomClass]` → Pickle

## Target Roots Configuration

Target roots define the base directories for task output storage.

### Environment Variables

```bash
# Single root (default)
export STARDAG_TARGET_ROOTS='{"default": "/path/to/outputs/"}'

# Multiple roots
export STARDAG_TARGET_ROOTS='{"default": "~/.stardag/outputs/", "ingestion": "s3://bucket/prefix/"}'

# Individual roots
export STARDAG_TARGET_ROOTS__DEFAULT="/path/to/outputs/"
export STARDAG_TARGET_ROOTS__INGESTION="s3://bucket/prefix/"
```

### Config File (~/.stardag/config.toml)

```toml
[profiles.default]
registry = "production"

[profiles.default.target_roots]
default = "~/.stardag/local-target-roots/default/default"
```

### Using Named Target Roots

```python
# Uses "default" target root
sd.get_file_target("output.json")

# Uses "ingestion" target root
sd.get_file_target("output.json", target_root_key="ingestion")
```

### S3 Target Factory

For remote storage via AWS S3:

```python
from stardag.integration.aws.s3 import S3TargetFactory

# Configured automatically when target root URI starts with s3://
# e.g., STARDAG_TARGET_ROOTS='{"default": "s3://my-bucket/stardag/"}'
```

## Custom Target Factory Provider

For advanced use cases, override the target factory:

```python
from stardag.target import target_factory_provider

# The provider resolves target_root_key → actual target factory
factory = target_factory_provider.get()
target = factory.get_file_target("relpath", target_root_key="default")
```

## Testing

### test_harness (Recommended)

The recommended approach for testing tasks. Sets up isolated temp target roots and `NoOpRegistry`:

```python
from stardag.testing import test_harness

def test_my_pipeline():
    with test_harness():
        task = MyTask(param="value")
        task.complete()
        result = task.load()
        assert result == expected
```

### InMemoryTarget (Low-Level)

For unit tests that need direct target manipulation:

```python
from stardag.target._in_memory import InMemoryTarget

target = InMemoryTarget[list[int]]()
target.save([1, 2, 3])
assert target.load() == [1, 2, 3]
```
