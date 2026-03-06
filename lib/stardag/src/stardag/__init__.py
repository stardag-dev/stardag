"""Stardag: Declarative and composable DAG framework for Python.

Stardag provides a clean Python API for representing persistently stored assets
as a declarative Directed Acyclic Graph (DAG).

Basic usage::

    import stardag as sd

    @sd.task
    def get_range(limit: int) -> list[int]:
        return list(range(limit))

    @sd.task
    def get_sum(integers: sd.Depends[list[int]]) -> int:
        return sum(integers)

    task = get_sum(integers=get_range(limit=10))
    sd.build(task)
    print(task.target().load())  # 45

Core components:

- :func:`task` - Decorator for creating tasks from functions
- :class:`Task` - Task with automatic serialization and filesystem targets
- :class:`LoadableTask` - Abstract base for tasks with ``load() -> T``
- :class:`TargetTask` - Base class for tasks with typed target outputs
- :class:`Depends` - Dependency injection type annotation
- :func:`build` - Execute task and its dependencies

See https://docs.stardag.com for full documentation.

TODO: Expand docstrings for all public API components.
"""

from importlib.metadata import PackageNotFoundError, version

from stardag._core.alias_task import AliasedMetadata, AliasTask
from stardag._core.base_task import (
    BaseTask,
    LoadableTask,
    TargetTask,
    TaskRef,
    TaskStruct,
    auto_namespace,
    flatten_task_struct,
    namespace,
)
from stardag._core.decorator import Depends, task
from stardag._core.hashable_set import HashableSet, HashSafeSetSerializer
from stardag._core.task import Task
from stardag._core.task_loads import TaskLoads
from stardag.base_model import StardagBaseModel, StardagField
from stardag.build import build, build_aio, build_sequential, build_sequential_aio
from stardag.config import config_provider
from stardag.exceptions import (
    APIError,
    AuthenticationError,
    AuthorizationError,
    StardagError,
    TokenExpiredError,
)
from stardag.polymorphic import Polymorphic, SubClass
from stardag.registry import registry_provider
from stardag.target import (
    DirectoryTarget,
    FileSystemTarget,
    FileTarget,
    LocalTarget,
    get_directory_target,
    get_file_target,
    target_factory_provider,
)

try:
    __version__ = version("stardag")
except PackageNotFoundError:
    # Package not installed (e.g., running from source in Modal container)
    __version__ = "0.0.0.dev"


__all__ = [
    "__version__",
    "AliasedMetadata",
    "AliasTask",
    "APIError",
    "AuthenticationError",
    "AuthorizationError",
    "auto_namespace",
    "TargetTask",
    "BaseTask",
    "build",
    "build_aio",
    "build_sequential",
    "build_sequential_aio",
    "config_provider",
    "Depends",
    "DirectoryTarget",
    "FileSystemTarget",
    "FileTarget",
    "get_directory_target",
    "get_file_target",
    "HashableSet",
    "HashSafeSetSerializer",
    "LoadableTask",
    "LocalTarget",
    "namespace",
    "Polymorphic",
    "registry_provider",
    "StardagError",
    "StardagBaseModel",
    "StardagField",
    "SubClass",
    "Task",
    "TaskRef",
    "TaskLoads",
    "TaskStruct",
    "target_factory_provider",
    "task",
    "TokenExpiredError",
    "flatten_task_struct",
]
