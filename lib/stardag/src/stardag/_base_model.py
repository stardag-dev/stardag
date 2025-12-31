from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Type, TypeVar

from pydantic import (
    BaseModel,
    ConfigDict,
    SerializationInfo,
    ValidationInfo,
    field_validator,
    model_serializer,
)
from pydantic.fields import FieldInfo
from pydantic_core import PydanticUndefined

ValidationMode = Literal["compat", None]
SerializationMode = Literal["hash", None]

MODE_KEY = "mode"


@dataclass(frozen=True)
class Compatability:
    """
    Used for:
      - validation with mode="compat": if key missing -> use default
      - serialization with mode="hash": if value == default -> drop key
    """

    default: Any


class StardagBaseModel(BaseModel):
    """
    Implements:
      - mode="compat" defaults for fields marked BackwardCompat(default=...)
      - mode="hash" dropping of BackwardCompat-default-valued fields on dump

    NOTE: This applies to any model inheriting CustomBase.
    """

    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        frozen=True,
        validate_default=True,
    )

    @field_validator("*", mode="wrap")
    @classmethod
    def _compat_defaults(cls, value: Any, handler, info: ValidationInfo) -> Any:
        """If mode="compat" and value is missing, use Compatability default."""
        mode: ValidationMode = info.context.get(MODE_KEY) if info.context else None

        if mode == "compat" and value is PydanticUndefined:
            field = cls.model_fields[info.field_name]  # type: ignore[attr-defined]
            maybe_compat = _get_annotation(field, Compatability)
            if maybe_compat is not None:
                compat: Compatability = maybe_compat
                # Run the default through normal validation:
                return handler(compat.default)

        return handler(value)

    @model_serializer(mode="wrap")
    def _hash_drop_defaults(self, handler, info: SerializationInfo):
        """If mode="hash", drop fields with BackwardCompat default values."""
        data = handler(self)
        mode: SerializationMode = info.context.get(MODE_KEY) if info.context else None
        if mode != "hash" or not isinstance(data, dict):
            return data

        out: dict[str, Any] = {}
        for name, value in data.items():
            field = self.__class__.model_fields.get(name)
            if field is None:
                out[name] = value
                continue

            maybe_compat = _get_annotation(field, Compatability)
            if maybe_compat is not None and maybe_compat.default == value:
                continue

            out[name] = value

        return out


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
