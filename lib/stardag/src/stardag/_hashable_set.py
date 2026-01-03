from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, Callable, TypeVar, get_args

from pydantic import GetCoreSchemaHandler, TypeAdapter
from pydantic_core import core_schema

from stardag.base_model import CONTEXT_MODE_KEY, SerializationContextMode


def _identity(x: Any) -> Any:
    return x


class HashSafeSetSerializer:
    """
    For a field typed as frozenset[T], serialize as a list.
    Only sort deterministically when context mode is "hash".
    """

    def __init__(self, sort_key: Callable[[Any], Any] | None = None) -> None:
        self.sort_key = sort_key or _identity

    def __get_pydantic_core_schema__(
        self, source_type: Any, handler: GetCoreSchemaHandler
    ):
        # Get the item type from frozenset[T] or set[T]
        args = get_args(source_type)
        item_type = args[0] if args else Any

        # Build the full schema for the frozenset
        schema = handler(source_type)

        # Create TypeAdapter once at schema-building time (not per-call).
        # TypeAdapter properly includes all definitions needed for complex types.
        item_adapter = TypeAdapter(item_type)

        sort_key = self.sort_key

        def serialize_set(v: frozenset, info) -> list:
            serialized_items = [
                (
                    item,
                    item_adapter.dump_python(
                        item, mode=info.mode, context=info.context
                    ),
                )
                for item in v
            ]

            mode: SerializationContextMode = (
                info.context.get(CONTEXT_MODE_KEY) if info.context else None
            )
            if mode == "hash":
                serialized_items.sort(key=lambda x: sort_key(x[0]))

            return [item[1] for item in serialized_items]

        schema["serialization"] = core_schema.plain_serializer_function_ser_schema(
            serialize_set,
            info_arg=True,
        )
        return schema


class _HashableSet:
    def __class_getitem__(cls, item, sort_key=None):
        return Annotated[frozenset[item], HashSafeSetSerializer(sort_key=sort_key)]


T = TypeVar("T")

if TYPE_CHECKING:
    HashableSet = Annotated[frozenset[T], "hashable set with sorted serialization"]
else:
    HashableSet = _HashableSet
