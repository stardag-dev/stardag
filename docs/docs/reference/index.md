# Reference

Technical reference documentation for Stardag.

## API Reference

Auto-generated documentation from source code:

- **[API Reference](api.md)** - Core SDK classes and functions
- **[Exceptions](exceptions.md)** - Error types and handling

## Quick Links

### Core Classes

| Class          | Description                                |
| -------------- | ------------------------------------------ |
| `BaseTask`     | Minimal core API for all tasks             |
| `LoadableTask` | Abstract base with `load() -> T`           |
| `TargetTask`   | Base class for tasks with typed targets    |
| `Task`         | Default: auto filesystem targets (diamond) |
| `@task`        | Decorator for function-based tasks         |

### Dependency Types

| Type           | Description           |
| -------------- | --------------------- |
| `Depends[T]`   | Inject loaded output  |
| `TaskLoads[T]` | Access task object    |
| `TaskSet[T]`   | Handle multiple tasks |

### Build Functions

| Function  | Description                   |
| --------- | ----------------------------- |
| `build()` | Execute task and dependencies |

### Target Types

| Class                    | Description               |
| ------------------------ | ------------------------- |
| `FileSystemTarget`       | Base filesystem target    |
| `LoadableSaveableTarget` | Target with serialization |

### Configuration

| Function                  | Description                      |
| ------------------------- | -------------------------------- |
| `get_target()`            | Create target from relative path |
| `target_factory_provider` | Manage target factory            |

## External Resources

- [GitHub Repository](https://github.com/stardag-dev/stardag)
- [PyPI Package](https://pypi.org/project/stardag/)
