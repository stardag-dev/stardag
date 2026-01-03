import abc
import inspect
import logging
from abc import abstractmethod
from collections import abc as collections_abc
from dataclasses import dataclass
from functools import cached_property, total_ordering
from typing import (
    Any,
    ClassVar,
    Generator,
    Generic,
    Mapping,
    Protocol,
    Sequence,
    TypeVar,
    runtime_checkable,
)
from uuid import UUID

from pydantic import ConfigDict, Field, SerializationInfo
from typing_extensions import TypeAlias, Union

from stardag._task_id import _get_task_id_from_jsonable
from stardag.base_model import CONTEXT_MODE_KEY
from stardag.polymorphic import PolymorphicRoot

logger = logging.getLogger(__name__)


TaskStruct: TypeAlias = Union[
    "TaskBase", Sequence["TaskStruct"], Mapping[str, "TaskStruct"]
]


@total_ordering
class TaskBase(
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

    @abstractmethod
    def run(self) -> None | Generator[TaskStruct, None, None]:
        """Execute the task logic."""
        ...

    def run_version_checked(self):
        if not self.version == self.__version__:
            raise ValueError("TODO")

        self.run()

    def requires(self) -> TaskStruct | None:
        return None

    @classmethod
    def has_dynamic_deps(cls) -> bool:
        return inspect.isgeneratorfunction(cls.run)

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

    def __lt__(self, other: "TaskBase") -> bool:
        return self.id < other.id


@runtime_checkable
class Target(Protocol):
    def exists(self) -> bool: ...


TargetT = TypeVar("TargetT", bound=Target, covariant=True)


class Task(TaskBase, Generic[TargetT], metaclass=abc.ABCMeta):
    def complete(self) -> bool:
        """Check if the task is complete."""
        return self.output().exists()

    @abstractmethod
    def output(self) -> TargetT:
        """The task output target."""
        ...


def flatten_task_struct(task_struct: TaskStruct) -> list[TaskBase]:
    """Flatten a TaskStruct into a list of Tasks.

    TaskStruct: TypeAlias = Union[
        "TaskBase", Sequence["TaskStruct"], Mapping[str, "TaskStruct"]
    ]
    """
    if isinstance(task_struct, TaskBase):
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
    type_name: str
    version: str | None
    id: UUID

    @classmethod
    def from_task(cls, task: TaskBase) -> "TaskRef":
        return cls(
            type_name=task.get_type_name(),
            version=task.version,
            id=task.id,
        )

    @property
    def slug(self) -> str:
        version_slug = f"v{self.version}" if self.version else ""
        return f"{self.type_name}-{version_slug}-{str(self.id)[:8]}"

    def __str__(self) -> str:
        return self.slug
