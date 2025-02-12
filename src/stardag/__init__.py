from stardag._base import (
    Task,
    TaskDeps,
    TaskIDRef,
    TaskStruct,
    auto_namespace,
    namespace,
)
from stardag.auto_task import AutoTask
from stardag.decorator import Depends, task
from stardag.parameter import IDHasher, IDHasherABC, IDHashInclude, IDHashIncludeABC
from stardag.target import (
    DirectoryTarget,
    FileSystemTarget,
    LocalTarget,
    get_directory_target,
    get_target,
    target_factory_provider,
)
from stardag.task_parameter import TaskLoads, TaskParam, TaskSet

__all__ = [
    "auto_namespace",
    "AutoTask",
    "Depends",
    "DirectoryTarget",
    "FileSystemTarget",
    "get_directory_target",
    "get_target",
    "LocalTarget",
    "namespace",
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
