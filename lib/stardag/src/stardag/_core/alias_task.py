from functools import cached_property
from typing import Annotated, Any, Generic, get_args
from uuid import UUID

from pydantic import BaseModel, SerializationInfo, ValidationInfo, model_validator

from stardag._core.auto_task import AutoTask, LoadedT
from stardag._core.task import Task
from stardag.base_model import StardagField
from stardag.registry import RegistryABC, registry_provider
from stardag.target import FileSystemTarget
from stardag.target.serialize import get_serializer

_run_error_msg = "AliasTask does not implement run(), it can only be used to reference another task's output."


class AliasedMetadata(BaseModel):
    """Metadata for an aliased task."""

    id: UUID
    uri: str
    body: dict[str, Any] | None = None

    @classmethod
    def from_task(cls, task: Task[FileSystemTarget]) -> "AliasedMetadata":
        """Create AliasedMetadata from a given task."""
        return cls(
            id=task.id,
            uri=task.output().path,
            body=task.model_dump(),  # Optional
        )


class AliasTask(AutoTask[LoadedT], Generic[LoadedT]):
    """A task that acts as an alias to another task's output.

    The purpose of `AliasTask` is to reference the output of another task that has been
    completed but can no longer be directly instantiated, for example due to breaking
    changes to the task's parameters. As long as the *loaded* type is still the same,
    an `AliasTask` can be used in its place. Critically, the aliased task's ID will be
    the same as the original task's ID, so downstream tasks depending on it will also
    have the same IDs. It is the responsibility of the user to ensure that the aliased
    task's output is compatible with the expected loaded type and the task ID. This is
    typically achieved by loading the `AliasTask` from the Stardag APIRegistry, which
    stores the necessary metadata.

    An alias task can also be used to explicitly require a task to be completed and not
    updated by code changes, but still maintian a reference to the upstream task (and
    DAG) that produced the data.

    Example:
    ```python
    import stardag as sd


    class OriginalTask(sd.AutoTask[int]):
        def run(self):
            self.output().save(42)

    original_task = OriginalTask()
    original_task.run()

    alias_task = sd.AliasTask[int](aliased=sd.AliasedMetadata.from_task(original_task))
    assert alias_task.aliased.id == original_task.id
    assert alias_task.complete()


    class DownstreamTask(sd.AutoTask[int]):
        loads_int: sd.TaskLoads[int]
        def run(self):
            self.output().save(self.loads_int.output().load() + 1)


    downstream_task = DownstreamTask(loads_int=original_task)
    downstream_task_with_alias = DownstreamTask(loads_int=alias_task)
    assert downstream_task.id == downstream_task_with_alias.id
    """

    aliased: Annotated[AliasedMetadata, StardagField(hash_exclude=True)]

    @classmethod
    def from_registry(
        cls,
        id: UUID,
        registry: RegistryABC | None = None,
    ) -> "AliasTask[LoadedT]":
        """Create an AliasTask by loading metadata from the Stardag APIRegistry.

        Args:
            id: The UUID of the task to alias.
            registry: An optional registry instance to use for loading metadata. If not
                provided, the default registry from `registry_provider` will be used.
        Returns:
            An AliasTask instance referencing the specified task.
        """
        registry = registry or registry_provider.get()
        metadata = registry.task_get_metadata(id)
        if metadata.output_uri is None:
            raise ValueError(
                f"Cannot create AliasTask for task {id} without a FileSystemTarget "
                "output."
            )

        return cls(
            aliased=AliasedMetadata(
                id=metadata.id,
                uri=metadata.output_uri,
                body=metadata.body,
            )
        )

    @model_validator(mode="after")
    def _validate_specifies_generic_loads_type(self) -> "AliasTask[LoadedT]":
        """Ensure that the generic LoadedT type parameter is specified."""
        orig_class = getattr(self, "__orig_class__", None)
        assert orig_class is not None
        loaded_type_arg = get_args(orig_class)[0]
        if loaded_type_arg is LoadedT:
            raise TypeError(
                "AliasTask must be instantiated with a specific loaded type, e.g., "
                "`AliasTask[int](...)`, or. `AliasTask[int].from_registry(...)`."
            )
        return self

    def run(self):
        raise NotImplementedError(_run_error_msg)

    def run_aio(self):
        raise NotImplementedError(_run_error_msg)

    @property
    def _relpath(self) -> str:
        """Override to customize the relative path of the task output.

        When a fully qualified URI is provided, the `get_target` function ignores
        target roots.
        """
        return self.aliased.uri

    @cached_property
    def id(self) -> UUID:
        return self.aliased.id

    def _hash_mode_finalize(self, data: dict[str, Any], info: SerializationInfo) -> Any:
        """Make hash mode serialization of tasks a container of just their ID."""
        # NOTE: UUID is stringified to match serialization mode "json"
        return {"id": str(self.id)}

    # Override pydantic serialization for this model to use `aliased_body` if provided,
    # with extra ID and URI fields
    def _serialize_extra(self, data: Any, info: SerializationInfo[Any | None]):
        data = self.aliased.body or {}
        return {
            "__aliased": {"id": str(self.aliased.id), "uri": self.aliased.uri},
            **data,
        }

    # Override validation to extract ID and URI from aliased body, and put the rest in _body
    @classmethod
    def _before_validate(cls, payload: Any, info: ValidationInfo) -> Any:
        if not isinstance(payload, dict):
            return payload

        aliased = payload.pop("__aliased", None)
        if aliased is not None:
            return {
                "aliased": AliasedMetadata(
                    id=UUID(aliased["id"]),
                    uri=aliased["uri"],
                    body=payload,
                )
            }

        return payload

    @property
    def serializer(self):
        """The serializer used for this task's output."""
        orig_class = getattr(self, "__orig_class__", None)
        assert orig_class is not None
        args = get_args(orig_class)
        assert len(args) == 1
        loaded_t = args[0]
        return get_serializer(loaded_t)
