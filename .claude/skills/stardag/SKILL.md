---
name: stardag
description: >
  Stardag SDK usage guide (0.27 / registry v2). Use when writing code that imports stardag,
  defining tasks/DAGs, choosing significant vs non-significant parameters, passing build
  settings, configuring builds, working with targets/serialization, deploying to Modal, or
  interacting with the Stardag Registry API/UI/CLI. Covers the full SDK surface: task
  definitions, dependencies, build execution, targets, deployments, configuration, CLI, and
  the Registry platform.
user-invocable: false
---

# Stardag SDK & Platform Guide

Stardag is a declarative, composable DAG framework for Python with persistent asset management.
Tasks are Pydantic models with deterministic output paths based on parameter hashing.

**Always `import stardag as sd`** — this is the standard convention. Python 3.11 or newer.

This guide describes the **v2 line** (SDK `0.27`, registry server `0.6`). Anything you remember
about `significance=`, `hash_exclude=`, `build_config`, `build_config_scope`, scope keys or
global locks is v1 and gone — see [registry-and-platform.md](registry-and-platform.md#upgrading-from-v1).

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
- **Two hashes**: `task.id` (namespace + name + version + **significant** fields) is the
  promise about output — completion, the claim and the target path key on it.
  `task.instance_hash` covers every field and identifies one construction.
- **Significant vs non-significant**: every field is significant by default;
  `sd.StardagField(significant=False)` marks one that changes how the work is done or which
  upstreams it has, never the output. Both are ordinary constructor arguments.
- **Settings**: build-wide environment variables (`sd.build(root, settings={...})`), applied in
  every process of the build; may change structure and execution, never output.
- **Deployments**: `stardag modal deploy` records one per deploy; a local build plans under
  one keyed on its code id. A build's scope is `(deployment, settings)`.
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

- **[sdk-core.md](sdk-core.md)**: Task hierarchy, decorators, dependencies, parameters and the two hashes, settings, build execution
- **[sdk-targets.md](sdk-targets.md)**: Targets, serialization, storage configuration, target roots
- **[sdk-advanced.md](sdk-advanced.md)**: Async, dynamic dependencies, namespaces, artifacts, cancellation, Modal (deployments, reactive builds, `TickConfig`)
- **[registry-and-platform.md](registry-and-platform.md)**: Registry entities and API, CLI, configuration, upgrading from v1
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
sd.AliasTask[T]         # Reference an existing task output by task id
sd.LoadValidator[T]     # Automatic validation on _save() and load()

# Decorators & Types
sd.task                 # @sd.task decorator
sd.Depends[T]           # Dependency injection (decorator API)
sd.TaskLoads[T]         # Polymorphic dependency (class API)
sd.TaskRef              # Immutable task reference (name, version, id)

# Parameters
sd.StardagField(...)               # significant=False, or compat_default=... (significant fields)
sd.StardagBaseModel                # Base Pydantic model for nested parameter models
sd.HashableSet[T]                  # Hashable frozenset for parameters
sd.check_serialization_stability   # The registration-time round-trip check, for tests

# Build
sd.build(tasks)         # Concurrent build (default); settings=, resume_build_id=, ...
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
sd.task_from_registry_data(data)   # Rebuild a task from a stored instance body (pickle-free)
sd.TaskRehydrationError            # Raised when reconstruction fails

# Cooperative cancellation (inside run())
sd.cancellation_requested()        # Throttled "is my execution still wanted?"
sd.ExecutionCancelled              # Raise it to stop cleanly (no output, no completion)

# Artifacts
from stardag.artifact import MarkdownArtifact, JSONArtifact

# Testing
from stardag.testing import test_harness, InMemoryRegistry

# Exceptions
sd.StardagError, sd.APIError, sd.AuthenticationError, sd.AuthorizationError
sd.InstanceConflictError           # Two constructions of one task id in one build
sd.UnstableSerializationError      # A field whose dump is not a fixed point
sd.ResumableInterruption           # The one you RAISE, after checkpointing
from stardag.integration.modal import MODAL_INTERRUPTIONS  # what to catch
from stardag.build import BuildFailed          # Raised by BuildSummary.raise_on_failure()
from stardag.build import SettingsError        # Reserved key, or conflicting settings in-process
from stardag.build import TaskExecutionError   # Wraps task executor exceptions
```
