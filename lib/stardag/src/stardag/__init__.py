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
    print(task.output().load())  # 45

Core components:

- :func:`task` - Decorator for creating tasks from functions
- :class:`Task` - Base class for all tasks
- :class:`AutoTask` - Task with automatic filesystem targets
- :class:`Depends` - Dependency injection type annotation
- :func:`build` - Execute task and its dependencies

See https://docs.stardag.com for full documentation.

TODO: Expand docstrings for all public API components.
"""

from importlib.metadata import PackageNotFoundError, version

from stardag._core.alias_task import AliasedMetadata, AliasTask
from stardag._core.auto_task import AutoTask
from stardag._core.decorator import Depends, task
from stardag._core.hashable_set import HashableSet, HashSafeSetSerializer
from stardag._core.task import (
    BaseTask,
    Task,
    TaskRef,
    TaskStruct,
    auto_namespace,
    flatten_task_struct,
    namespace,
)
from stardag._core.task_loads import TaskLoads
from stardag.base_model import StardagBaseModel, StardagField
from stardag.build import build, build_aio, build_sequential, build_sequential_aio
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
    LocalTarget,
    get_directory_target,
    get_target,
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
    "AutoTask",
    "BaseTask",
    "build",
    "build_aio",
    "build_sequential",
    "build_sequential_aio",
    "Depends",
    "DirectoryTarget",
    "FileSystemTarget",
    "get_directory_target",
    "get_target",
    "HashableSet",
    "HashSafeSetSerializer",
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
