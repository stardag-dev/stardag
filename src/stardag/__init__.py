from stardag._auto_task import AutoTask
from stardag._base import (
    Task,
    TaskDeps,
    TaskIDRef,
    TaskStruct,
    auto_namespace,
    namespace,
)
from stardag._decorator import Depends, task
from stardag._parameter import IDHasher, IDHasherABC, IDHashInclude, IDHashIncludeABC
from stardag._task_parameter import TaskLoads, TaskParam, TaskSet
from stardag.build.registry import registry_provider
from stardag.build.sequential import build
from stardag.target import (
    DirectoryTarget,
    FileSystemTarget,
    LocalTarget,
    get_directory_target,
    get_target,
    target_factory_provider,
)

__all__ = [
    "auto_namespace",
    "AutoTask",
    "build",
    "Depends",
    "DirectoryTarget",
    "FileSystemTarget",
    "get_directory_target",
    "get_target",
    "LocalTarget",
    "namespace",
    "registry_provider",
    "Task",
    "TaskDeps",
    "TaskIDRef",
    "TaskLoads",
    "TaskParam",
    "TaskSet",
    "TaskStruct",
    "target_factory_provider",
    "task",
    "IDHasher",
    "IDHashInclude",
    "IDHasherABC",
    "IDHashIncludeABC",
]
