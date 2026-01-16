from functools import cached_property
from typing import Annotated, Any, Generic, get_args
from uuid import UUID

from pydantic import BaseModel, SerializationInfo, ValidationInfo

from stardag._core.auto_task import AutoTask, LoadedT
from stardag.base_model import StardagField
from stardag.target.serialize import get_serializer

_run_error_msg = "AliasTask does not implement run(), it can only be used to reference another task's output."


class AliasedMetadata(BaseModel):
    """Metadata for an aliased task."""

    id: UUID
    uri: str
    body: dict[str, Any] | None = None


class AliasTask(AutoTask[LoadedT], Generic[LoadedT]):
    """A task that acts as an alias to another task's output."""

    aliased: Annotated[AliasedMetadata, StardagField(hash_exclude=True)]

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
