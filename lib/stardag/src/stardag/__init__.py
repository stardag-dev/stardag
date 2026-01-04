from importlib.metadata import PackageNotFoundError, version

from stardag._auto_task import AutoTask
from stardag._decorator import Depends, task
from stardag._hashable_set import HashableSet, HashSafeSetSerializer
from stardag._task import (
    BaseTask,
    Task,
    TaskRef,
    TaskStruct,
    auto_namespace,
    namespace,
)
from stardag._task_loads import TaskLoads
from stardag.build.registry import registry_provider
from stardag.build.sequential import build
from stardag.exceptions import (
    APIError,
    AuthenticationError,
    AuthorizationError,
    StardagError,
    TokenExpiredError,
)
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
    "APIError",
    "AuthenticationError",
    "AuthorizationError",
    "auto_namespace",
    "AutoTask",
    "BaseTask",
    "build",
    "Depends",
    "DirectoryTarget",
    "FileSystemTarget",
    "get_directory_target",
    "get_target",
    "LocalTarget",
    "namespace",
    "registry_provider",
    "StardagError",
    "Task",
    "TaskRef",
    "TaskLoads",
    "HashableSet",
    "HashSafeSetSerializer",
    "TaskStruct",
    "target_factory_provider",
    "task",
    "TokenExpiredError",
]
