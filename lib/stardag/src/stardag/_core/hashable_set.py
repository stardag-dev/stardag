from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, Callable, TypeVar, get_args

from pydantic import GetCoreSchemaHandler, TypeAdapter
from pydantic_core import core_schema

from stardag._core.task_id import (
    canonical_item_key,
    canonicalize_sets,
    record_ordered_set,
)
from stardag.base_model import CONTEXT_MODE_KEY, SerializationContextMode


def _identity(x: Any) -> Any:
    return x


class HashSafeSetSerializer:
    """
    For a field typed as frozenset[T], serialize as a list.

    Sorted deterministically in the two modes that are hashed or stored —
    "hash" (the task id) and "registry" (the instance body, whose hash is
    the instance hash) — because a set's iteration order differs between
    processes: by ``sort_key`` first, and by each item's canonical JSON where
    two items share a key (a stable sort would otherwise keep the set's
    iteration order for ties). Other dumps keep iteration order.
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
            if mode not in ("hash", "registry"):
                return [item[1] for item in serialized_items]

            # Items may hold sets of their own; order those first, so the
            # tie-break key below is itself canonical.
            # (raw member, its canonicalised dump): the dump is what is
            # returned, so a member's nested sets are ordered in the output.
            canonical_items = [
                (raw, canonicalize_sets(raw, dumped, info.context))
                for raw, dumped in serialized_items
            ]
            canonical_items.sort(
                key=lambda pair: (sort_key(pair[0]), canonical_item_key(pair[1]))
            )
            ordered = [canonical for _, canonical in canonical_items]
            record_ordered_set(info.context, v, ordered)
            return ordered

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
