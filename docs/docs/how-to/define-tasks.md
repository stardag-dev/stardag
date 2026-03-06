_NOTE this page is WIP. It is not complete and contains inaccuracies, currently excluded from docs site._

# Define Tasks

Stardag provides three APIs for defining tasks, each suited to different use cases.

## Choosing an API

| API                     | Best For                                   | Dependency Declaration |
| ----------------------- | ------------------------------------------ | ---------------------- |
| `@task` decorator       | Simple tasks with injected dependencies    | `sd.Depends[T]`        |
| `Task` class            | Dynamic dependencies, custom behavior      | `sd.TaskLoads[T]`      |
| `LoadableTask` class    | Custom `load()` without filesystem targets | `sd.TaskLoads[T]`      |
| Base `TargetTask` class | Full control over target type              | Manual                 |

## The `@task` Decorator

The simplest and most common approach:

```python
import stardag as sd

@sd.task
def clean_data(raw: sd.Depends[str]) -> str:
    """Clean raw text data."""
    return raw.strip().lower()

@sd.task
def count_words(text: sd.Depends[str]) -> int:
    """Count words in text."""
    return len(text.split())
```

### Key Features

- Dependencies are automatically loaded and injected
- Return type determines serialization
- Minimal boilerplate

### Customization Options

```python
@sd.task(
    name="CleanData",           # Custom task name (default: function name)
    version="1.0",              # Updates output URI and task ID when changed
)
def clean_data(raw: sd.Depends[str]) -> str:
    return raw.strip()
```

## The `Task` Class

Use when dependencies need computation or you need more control:

```python
import stardag as sd

class CleanData(sd.Task[str]):
    """Clean raw text data."""

    raw_data: sd.TaskLoads[str]
    lowercase: bool = True

    def requires(self):
        return self.raw_data

    def run(self):
        text = self.raw_data.load()
        if self.lowercase:
            text = text.lower()
        self._save(text.strip())
```

### Key Features

- `Task[T]` - type parameter defines output type
- `requires()` - return dependencies
- `run()` - implement execution logic
- Automatic target creation

### Dynamic Dependencies

```python
class ProcessBatch(sd.Task[list[int]]):
    batch_size: int
    data_sources: list[str]

    def requires(self):
        # Generate dependencies from parameters
        return [
            FetchData(source=src)
            for src in self.data_sources
        ]

    def run(self):
        results = []
        for dep in self.requires():
            results.extend(dep.load())
        self._save(results[:self.batch_size])
```

## The Base `TargetTask` Class

For complete control over all aspects:

```python
import stardag as sd
from stardag.target import LoadableSaveableFileSystemTarget
from stardag.target.serialize import JSONSerializer, FileSerializable

class CustomTask(sd.TargetTask[LoadableSaveableFileSystemTarget[dict]]):
    """Task with custom target configuration."""

    input_data: sd.TaskLoads[list[int]]
    config_key: str

    def requires(self):
        return self.input_data

    def target(self) -> LoadableSaveableFileSystemTarget[dict]:
        # Custom path structure
        path = f"custom/{self.config_key}/{self.task_id[:8]}.json"
        return FileSerializable(
            wrapped=sd.get_file_target(path),
            serializer=JSONSerializer(dict),
        )

    def run(self):
        data = self.input_data.load()
        result = {"sum": sum(data), "config": self.config_key}
        self.target().save(result)
```

### Use Cases

- Non-filesystem targets (databases, APIs)
- Custom serialization formats
- Custom path structures
- Integration with external systems

## Comparison Example

The same task in all three APIs:

=== "@task Decorator"

    ```python
    @sd.task
    def sum_values(values: sd.Depends[list[int]]) -> int:
        return sum(values)
    ```

=== "Task"

    ```python
    class SumValues(sd.Task[int]):
        values: sd.TaskLoads[list[int]]

        def requires(self):
            return self.values

        def run(self):
            self._save(sum(self.values.load()))
    ```

=== "Base TargetTask"

    ```python
    class SumValues(sd.TargetTask[LoadableSaveableFileSystemTarget[int]]):
        values: sd.SubClass[sd.TargetTask[LoadableSaveableFileSystemTarget[list[int]]]]

        def requires(self):
            return self.values

        def target(self):
            return FileSerializable(
                wrapped=sd.get_file_target(f"sum/{self.task_id}.json"),
                serializer=JSONSerializer(int),
            )

        def run(self):
            self.target().save(sum(self.values.target().load()))
    ```

## Best Practices

1. **Start with `@task`** - Use the decorator for simple tasks, or `Task` for more control
2. **Use type hints** - They enable automatic serialization
3. **Keep tasks focused** - Each task should do one thing
4. **Make tasks pure** - Avoid side effects; same inputs = same outputs
5. **Use versioning** - Bump version when logic changes significantly

<!-- TODO: Add more examples and patterns -->
