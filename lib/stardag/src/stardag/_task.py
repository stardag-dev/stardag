import abc
import inspect
import logging
from abc import abstractmethod
from collections import abc as collections_abc
from dataclasses import dataclass
from functools import cached_property, total_ordering
from typing import (
    TYPE_CHECKING,
    Any,
    ClassVar,
    Generator,
    Generic,
    Mapping,
    Sequence,
)

if TYPE_CHECKING:
    from stardag._registry_asset import RegistryAsset

from uuid import UUID

from pydantic import ConfigDict, Field, SerializationInfo
from typing_extensions import TypeAlias, Union

from stardag._target_base import TargetType
from stardag._task_id import _get_task_id_from_jsonable
from stardag.base_model import CONTEXT_MODE_KEY
from stardag.polymorphic import PolymorphicRoot

logger = logging.getLogger(__name__)


TaskStruct: TypeAlias = Union[
    "BaseTask", Sequence["TaskStruct"], Mapping[str, "TaskStruct"]
]


@total_ordering
class BaseTask(
    PolymorphicRoot,
    metaclass=abc.ABCMeta,
):
    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        frozen=True,
        validate_default=True,
    )

    __version__: ClassVar[str] = ""

    version: str = Field(
        default="",
        description="Version of the task run implementation.",
    )

    @abstractmethod
    def complete(self) -> bool:
        """Declare if the task is complete."""
        ...

    async def complete_aio(self) -> bool:
        """Asynchronously declare if the task is complete."""
        return self.complete()

    @abstractmethod
    def run(self) -> None | Generator[TaskStruct, None, None]:
        """Execute the task logic."""
        ...

    async def run_aio(self) -> None | Generator[TaskStruct, None, None]:
        """Asynchronously execute the task logic."""
        return self.run()

    def run_version_checked(self) -> None | Generator[TaskStruct, None, None]:
        if not self.version == self.__version__:
            raise ValueError("TODO")

        return self.run()

    async def run_version_checked_aio(self) -> None | Generator[TaskStruct, None, None]:
        if not self.version == self.__version__:
            raise ValueError("TODO")

        return await self.run_aio()

    def requires(self) -> TaskStruct | None:
        return None

    def registry_assets(self) -> list["RegistryAsset"]:
        """Return assets to be stored in the registry after task completion.

        Override this method to expose rich outputs (reports, summaries,
        structured data) that will be viewable in the registry UI.

        This method is called after the task completes successfully. It should
        be stateless - loading any required data from the task's target rather
        than relying on in-memory state.

        Returns:
            List of registry assets (MarkdownRegistryAsset, JSONRegistryAsset, etc.)
        """
        return []

    def registry_assets_aio(self) -> list["RegistryAsset"]:
        """Asynchronously return assets to be stored in the registry after task
        completion.
        """
        return self.registry_assets()

    @classmethod
    def has_dynamic_deps(cls) -> bool:
        return inspect.isgeneratorfunction(cls.run) or inspect.isasyncgenfunction(
            cls.run_aio
        )  # TODO is this correct?

    @cached_property
    def id(self) -> UUID:
        return UUID(
            self.model_dump(
                mode="json",
                context={CONTEXT_MODE_KEY: "hash"},
            )["id"]
        )

    def _hash_mode_finalize(self, data: dict[str, Any], info: SerializationInfo) -> Any:
        """Make hash mode serialization of tasks a container of just their ID."""
        # NOTE: UUID is stringified to match serialization mode "json"
        return {"id": str(_get_task_id_from_jsonable(data))}

    def __lt__(self, other: "BaseTask") -> bool:
        return self.id < other.id


def auto_namespace(scope: str):
    """Set the task namespace for the module to the module import path.

    Usage:
    ```python
    import stardag as sd

    sd.auto_namespace(__name__)

    class MyTask(Task):
        ...
    ```
    """
    module = scope
    BaseTask._registry().add_namespace(module, module)


def namespace(namespace: str, scope: str):
    BaseTask._registry().add_namespace(scope, namespace)


class Task(BaseTask, Generic[TargetType]):
    def complete(self) -> bool:
        """Check if the task is complete."""
        return self.output().exists()

    async def complete_aio(self) -> bool:
        """Asynchronously check if the task is complete."""
        return await self.output().exists_aio()

    @abstractmethod
    def output(self) -> TargetType:
        """The task output target."""
        ...


def flatten_task_struct(task_struct: TaskStruct | None) -> list[BaseTask]:
    """Flatten a TaskStruct into a list of Tasks.

    TaskStruct: TypeAlias = Union[
        "TaskBase", Sequence["TaskStruct"], Mapping[str, "TaskStruct"]
    ]
    """
    if task_struct is None:
        return []

    if isinstance(task_struct, BaseTask):
        return [task_struct]

    if isinstance(task_struct, collections_abc.Sequence):
        return [
            task
            for sub_task_struct in task_struct
            for task in flatten_task_struct(sub_task_struct)
        ]

    if isinstance(task_struct, collections_abc.Mapping):
        return [
            task
            for sub_task_struct in task_struct.values()
            for task in flatten_task_struct(sub_task_struct)
        ]

    ValueError(f"Unsupported task struct type: {task_struct!r}")


@dataclass(frozen=True)
class TaskRef:
    name: str
    version: str | None
    id: UUID

    @classmethod
    def from_task(cls, task: BaseTask) -> "TaskRef":
        return cls(
            name=task.get_name(),
            version=task.version,
            id=task.id,
        )

    @property
    def slug(self) -> str:
        version_slug = f"v{self.version}" if self.version else ""
        return f"{self.name}-{version_slug}-{str(self.id)[:8]}"

    def __str__(self) -> str:
        return self.slug
