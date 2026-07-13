---
name: stardag
description: >
  Stardag SDK usage guide. Use when writing code that imports stardag, defining tasks/DAGs,
  configuring builds, working with targets/serialization, or interacting with the Stardag
  Registry API/UI. Covers the full SDK surface: task definitions, dependencies, build execution,
  targets, configuration, CLI, and the Registry platform.
user-invocable: false
---

# Stardag SDK & Platform Guide

Stardag is a declarative, composable DAG framework for Python with persistent asset management.
Tasks are Pydantic models with deterministic output paths based on parameter hashing.

**Always `import stardag as sd`** — this is the standard convention.

## Quick Reference

```python
import stardag as sd

# Decorator API (simplest)
@sd.task
def get_range(limit: int) -> list[int]:
    return list(range(limit))

@sd.task
def get_sum(integers: sd.Depends[list[int]]) -> int:
    return sum(integers)

root = get_sum(integers=get_range(limit=10))
sd.build(root)
print(root.load())  # 45

# Class API (recommended default)
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

root = Sum(integers=Range(limit=10))
sd.build(root)
print(root.load())  # 45
```

## Core Concepts

- **Tasks**: Pydantic models that define computation units with typed parameters
- **Dependencies**: Declared via `sd.TaskLoads[T]` (class API) or `sd.Depends[T]` (decorator API)
- **Targets**: Persistence layer (filesystem by default) with automatic serialization
- **Build**: Bottom-up execution that skips already-completed tasks (Makefile-style)
- **Task IDs**: Deterministic UUID-5 from namespace + name + version + parameter hash
- **Namespaces**: Organize tasks into logical groups via `sd.namespace()`

## Three-Tier API Design

| Level      | Base Class         | Best For                     | Control |
| ---------- | ------------------ | ---------------------------- | ------- |
| Decorator  | `@sd.task`         | Simple pure functions        | Least   |
| Task Class | `sd.Task[T]`       | Most use cases (recommended) | Medium  |
| TargetTask | `sd.TargetTask[T]` | Custom targets/serialization | Most    |

All three produce semantically equivalent results — choose based on complexity needs.

## Additional Resources

For detailed reference on specific topics, see these supporting files:

- **[sdk-core.md](sdk-core.md)**: Task hierarchy, decorators, dependencies, build execution, type system
- **[sdk-targets.md](sdk-targets.md)**: Targets, serialization, storage configuration, target roots
- **[sdk-advanced.md](sdk-advanced.md)**: Async support, dynamic dependencies, namespaces, artifacts, versioning
- **[registry-and-platform.md](registry-and-platform.md)**: Registry API, UI, CLI, authentication, configuration
- **[examples.md](examples.md)**: Complete code examples and common patterns

For the latest documentation, visit [docs.stardag.com](https://docs.stardag.com/).

## Key Imports

```python
import stardag as sd

# Core
sd.Task[T]              # Main task base class
sd.LoadableTask[T]      # Task with load() but no filesystem target
sd.TargetTask[T]        # Task with explicit target control
sd.BaseTask             # Abstract base (rarely used directly)
sd.AliasTask[T]         # Reference a remote/existing task output
sd.LoadValidator[T]     # Automatic validation on _save() and load()

# Decorators & Types
sd.task                 # @sd.task decorator
sd.Depends[T]           # Dependency injection (decorator API)
sd.TaskLoads[T]         # Polymorphic dependency (class API)
sd.TaskRef              # Immutable task reference (name, version, id)

# Build
sd.build(tasks)         # Concurrent build (default)
sd.build_aio(tasks)     # Async concurrent build
sd.build_sequential()   # Sequential (debugging)

# Targets
sd.get_file_target(relpath)       # File target factory
sd.get_directory_target(relpath)  # Directory target factory
sd.target_factory_provider        # Custom target factory provider

# Configuration
sd.config_provider      # Configuration provider
sd.registry_provider    # Registry provider (use .get() to access)

# Utilities
sd.namespace(ns, scope=__name__)   # Set task namespace
sd.auto_namespace(scope=__name__)  # Auto namespace from module
sd.flatten_task_struct()           # Flatten nested task structures
sd.get_default_relpath(task)       # Construct default task output relpath
sd.HashableSet[T]                  # Hashable frozenset for parameters
sd.StardagField(...)               # Field annotation (hash_exclude, etc.)
sd.StardagBaseModel                # Base Pydantic model
sd.task_from_registry_data(data)   # Rebuild a task from registry task_data (pickle-free)
sd.TaskRehydrationError            # Raised when reconstruction fails

# Artifacts
from stardag.artifact import MarkdownArtifact, JSONArtifact

# Testing
from stardag.testing import test_harness  # Isolated test environment

# Exceptions
sd.StardagError, sd.APIError, sd.AuthenticationError, sd.AuthorizationError
from stardag.build import BuildFailed          # Raised by BuildSummary.raise_on_failure()
from stardag.build import TaskExecutionError   # Wraps task executor exceptions
```
