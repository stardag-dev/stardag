"""
Implements a base model with advanced polymorphic serialization + validation features:

1) Polymorph registry (namespace + class name) with:
   - ALWAYS include discriminator keys in serialization for *all* MyBase subclasses
   - Polymorphic ("registered subclass") validation + "serialize as any" is OPT-IN per field via SubClass[T]

2) Context-based custom serialization + validation modes (mode in context):
   - mode="hash": drop fields annotated BackwardCompat(default=...) when value == default
   - mode="compat": if a BackwardCompat field is missing, populate it with the compat default

3) Auto-register any child class of MyBase when declared (via __init_subclass__).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Type, TypeVar

from pydantic import (
    BaseModel,
    ConfigDict,
    SerializationInfo,
    model_serializer,
    model_validator,
)
from pydantic.fields import FieldInfo

SerializationContextMode = Literal["hash", None]
ValidationContextMode = Literal["compat", None]
CONTEXT_MODE_KEY = "mode"


_UNSET = object()


@dataclass(frozen=True)
class StardagField:
    """TODO"""

    compat_default: Any = _UNSET
    hash_exclude: bool = False


class StardagBaseModel(BaseModel):
    """Custom, swap-in-replace for pydantic BaseModel, with features for hash mode +
    compat mode.

    Implements:
      - Validation with info.context["mode"] == "compat":
        - defaults for fields marked BackwardCompat(default=...)
      - Serialization with info.context["mode"] == "hash":
        - dropping of BackwardCompat-default-valued fields on dump
        - dropping fields marked hash_exclude=True

    NOTE: This applies to any model inheriting StardagBaseModel.
    """

    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        frozen=True,
        validate_default=True,
    )

    @model_validator(mode="before")
    @classmethod
    def _before_validate(cls, data: Any, info):
        """If mode="compat" and value is missing, use Compatability default."""
        mode: ValidationContextMode = (
            info.context.get(CONTEXT_MODE_KEY) if info.context else None
        )

        if mode != "compat" or not isinstance(data, dict):
            return data

        data = dict(data)
        for name, field in cls.model_fields.items():
            if name in data:
                # Value provided, skip
                continue

            # Value missing, check for compat default
            maybe_stardag_field = _get_annotation(field, StardagField)
            if (
                maybe_stardag_field is not None
                and maybe_stardag_field.compat_default is not _UNSET
            ):
                data[name] = maybe_stardag_field.compat_default

        return data

    @model_serializer(mode="wrap")
    def _wrap_serialize(self, handler, info: SerializationInfo):
        """If mode="hash", drop fields with BackwardCompat default values."""
        data = handler(self)
        data = self._serialize_extra(data, info)
        return self._handle_hash_mode(data, info)

    def _serialize_extra(self, data: Any, info: SerializationInfo) -> Any:
        """Allow for injection of additional serialization logic."""
        return data

    def _handle_hash_mode(self, data: Any, info: SerializationInfo) -> Any:
        mode: SerializationContextMode = (
            info.context.get(CONTEXT_MODE_KEY) if info.context else None
        )
        if mode != "hash" or not isinstance(data, dict):
            return data

        out: dict[str, Any] = {}
        for name, value in data.items():
            field = self.__class__.model_fields.get(name)
            if field is None:
                out[name] = value
                continue

            maybe_stardag_field = _get_annotation(field, StardagField)
            if maybe_stardag_field is not None:
                stardag_field: StardagField = maybe_stardag_field
                if stardag_field.compat_default == value or stardag_field.hash_exclude:
                    continue

            out[name] = value

        return self._hash_mode_finalize(out, info)

    def _hash_mode_finalize(self, data: dict[str, Any], info: SerializationInfo) -> Any:
        """Final cleanup for hash mode serialization."""
        # Currently no-op, but could be used for additional processing if needed.
        return data


_AnnotationType = TypeVar("_AnnotationType")


def _get_annotation(
    field: FieldInfo, type: Type[_AnnotationType]
) -> _AnnotationType | None:
    """Get exactly one metadata of given type from field, or None."""
    matches = [meta for meta in field.metadata if isinstance(meta, type)]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ValueError(f"Multiple metadata of type {type} found on field {field}")
    return None
