"""
Implements a base model with advanced polymorphic serialization + validation features:

1) Polymorph registry (namespace + class name) with:
   - ALWAYS include discriminator keys in serialization for *all* MyBase subclasses
   - Polymorphic ("registered subclass") validation + "serialize as any" is OPT-IN per field via SubClass[T]

2) Context-based custom serialization + validation modes (mode in context):
   - mode="hash": the dump the task id is computed from. Drops every field
     marked ``StardagField(significant=False)``, and a significant field
     whose value equals its ``compat_default``. Custom serializers may
     special-case this mode: it is the user's control over completion
     identity.
   - mode="registry": the instance body — **every** field, defaults
     included, nothing dropped. The instance hash is the hash of this dump
     (see ``stardag._core.instance``); it has no user-facing hash mode.
   - both modes sort sets at every nesting level (a set inside a list,
     dict or ``Any`` field too), so a set's per-process iteration order
     never reaches a hash or a stored body. The order is defined in one
     place, ``stardag._core.task_id.canonicalize_sets``.
   - mode="compat": validation of a stored body. A missing field with a
     ``compat_default`` is populated with it; an unknown key (a field the
     class no longer declares) is dropped with a warning.

3) Auto-register any child class of MyBase when declared (via __init_subclass__).

4) Two kinds of parameter (see ``StardagField.significant``): a significant
   field is part of the task id — the promise about output — and a
   non-significant one is not. Both are ordinary fields, passed at init and
   stored in the instance body, so both are covered by the instance hash.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal, Type, TypeVar

from pydantic import (
    BaseModel,
    ConfigDict,
    SerializationInfo,
    ValidationInfo,
    model_serializer,
    model_validator,
)
from pydantic.fields import FieldInfo
from typing_extensions import Never

logger = logging.getLogger(__name__)

SerializationContextMode = Literal["hash", "registry", None]
ValidationContextMode = Literal["compat", None]
CONTEXT_MODE_KEY = "mode"


_UNSET = object()

_REMOVED_FIELD_OPTIONS = {
    "significance": (
        "StardagField(significance=...) was removed in v2; use "
        "StardagField(significant=False) for a parameter that is not part of "
        "the task id (every former dependencies_only / execution_only field), "
        "and pass it at init like any other parameter."
    ),
    "hash_exclude": (
        "StardagField(hash_exclude=...) was removed in v2; use "
        "StardagField(significant=False)."
    ),
}


@dataclass(frozen=True, init=False)
class StardagField:
    """Per-field annotation controlling what a parameter is significant for.
    Attach via ``Annotated[T, StardagField(...)]``.

    A task has two identities (see ``BaseTask.id`` and
    ``BaseTask.instance_hash``): the task id hashes the significant fields,
    the instance hash hashes all of them.
    """

    compat_default: Any
    """Backward-compatible default. In compat-validation mode a missing field is
    populated with this value; in hash-mode serialization a field whose value
    equals this is dropped from the hash dump, so adding the field doesn't change
    existing hashes / task identities.

    Supply it in the field's *natural, validated Python form* — i.e. what the
    field holds after validation, not its serialized form. Both the validate and
    serialize sides compare against this raw value, so a non-idempotent input
    (e.g. ``[1, 2]`` for a ``tuple[int, ...]`` field, which validation coerces to
    ``(1, 2)``) would fail the serialize-side equality check and silently not be
    dropped.

    Only valid on a significant field: a non-significant field is not in the
    task id, so there is nothing for it to keep stable.
    """
    significant: bool
    """Whether the parameter is part of the task id (default ``True``).

    The task id is a promise about **output**: significant parameters, and
    nothing else, determine what the task produces, so completion and the
    execution claim are keyed by them. A parameter that changes *how* the
    work is done or *which upstreams* it has, but never the output — a
    fan-out partition size, a thread count, an annotation — is
    ``significant=False``. It is still an ordinary field: passed at init,
    stored in the instance body, covered by the instance hash, and
    rehydrated from the body (leniently: a stored value for a field the
    class no longer has is dropped with a warning; a missing one takes the
    class default).
    """

    def __init__(
        self,
        compat_default: Any = _UNSET,
        *,
        significant: bool = True,
        **removed: Never,
    ) -> None:
        for name in removed:
            message = _REMOVED_FIELD_OPTIONS.get(name)
            if message is not None:
                raise TypeError(message)
            raise TypeError(
                f"StardagField() got an unexpected keyword argument {name!r}"
            )
        if not isinstance(significant, bool):
            # A truthy string would silently read as significant.
            raise TypeError(
                f"StardagField(significant=...) must be a bool, got {significant!r}."
            )
        if compat_default is not _UNSET and not significant:
            raise ValueError(
                "compat_default has no effect on a non-significant field: it "
                "exists to keep the task id stable when a field is added, and a "
                "significant=False field is not part of the task id. Drop "
                "compat_default, or make the field significant."
            )
        object.__setattr__(self, "compat_default", compat_default)
        object.__setattr__(self, "significant", significant)


def is_significant(field: FieldInfo) -> bool:
    """Whether a pydantic field is part of the task id: from its
    ``StardagField`` annotation, ``True`` when it has none."""
    meta = _get_annotation(field, StardagField)
    return meta.significant if meta is not None else True


class StardagBaseModel(BaseModel):
    """Custom, swap-in-replace for pydantic BaseModel, with features for hash mode +
    compat mode.

    Implements:
      - Validation with info.context["mode"] == "compat":
        - defaults for fields marked ``StardagField(compat_default=...)``
        - unknown keys are dropped with a warning
      - Serialization with info.context["mode"] == "hash":
        - dropping of compat-default-valued fields on dump
        - dropping every ``significant=False`` field
        - sets sorted
      - Serialization with info.context["mode"] == "registry":
        - every field, sets sorted, nothing dropped

    ``significant`` is read per model, so a nested ``StardagBaseModel``
    carries it on its own fields; it affects the task id only.

    NOTE: This applies to any model inheriting StardagBaseModel.
    """

    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        frozen=True,
        validate_default=True,
    )

    @model_validator(mode="before")
    @classmethod
    def _check_add_compatibility_defaults(cls, data: Any, info: ValidationInfo) -> Any:
        """Compat mode (a stored body): fill compat defaults, drop unknown keys.

        Strict for significant fields by way of the task id: a missing one
        without a ``compat_default`` fails validation, and a value that
        validates to something else moves the recomputed id, which
        rehydration checks. Lenient for the rest: a key the class does not
        declare is dropped with a warning (a non-significant field removed
        under new code; a removed *significant* field moves the id and is
        caught there), and a missing non-significant field takes the class
        default.
        """
        mode: ValidationContextMode = (
            info.context.get(CONTEXT_MODE_KEY) if info.context else None
        )
        if mode != "compat" or not isinstance(data, dict):
            return data

        data = dict(data)
        if cls.model_config.get("extra") != "allow":
            known = cls._known_input_keys()
            unknown = [
                key
                for key in data
                # Discriminators and other framework keys (``__namespace``,
                # ``__name``, ``__aliased``) are consumed elsewhere.
                if key not in known and not key.startswith("__")
            ]
            if unknown:
                logger.warning(
                    "Dropping stored field(s) %s unknown to %s.%s while "
                    "rehydrating: the class no longer declares them.",
                    ", ".join(repr(k) for k in unknown),
                    cls.__module__,
                    cls.__qualname__,
                )
                for key in unknown:
                    data.pop(key)

        for name, field in cls.model_fields.items():
            if name in data:
                continue
            maybe_stardag_field = _get_annotation(field, StardagField)
            if (
                maybe_stardag_field is not None
                and maybe_stardag_field.compat_default is not _UNSET
            ):
                data[name] = maybe_stardag_field.compat_default

        return data

    @classmethod
    def _known_input_keys(cls) -> frozenset[str]:
        """Field names and validation aliases this class accepts."""
        keys: set[str] = set()
        for name, field in cls.model_fields.items():
            keys.add(name)
            if isinstance(field.alias, str):
                keys.add(field.alias)
            if isinstance(field.validation_alias, str):
                keys.add(field.validation_alias)
        return frozenset(keys)

    @model_serializer(mode="wrap")
    def _wrap_serialize(self, handler, info: SerializationInfo):
        """Apply the hash / registry serialization modes (see module docs)."""
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
        if mode not in ("hash", "registry") or not isinstance(data, dict):
            return data
        # Local import: task_id imports this module.
        from stardag._core.task_id import canonicalize_sets

        out: dict[str, Any] = {}
        for name, value in data.items():
            field = self.__class__.model_fields.get(name)
            if field is None:
                out[name] = value
                continue

            maybe_stardag_field = _get_annotation(field, StardagField)
            if mode == "hash" and maybe_stardag_field is not None:
                stardag_field: StardagField = maybe_stardag_field
                if not stardag_field.significant:
                    continue
                # Compare the *raw* Python value (not the already-serialized
                # `value`) against compat_default. The serialized form differs
                # from the Python value for enums (-> .value), tuples (-> list),
                # and fields with custom/hash-only serializers, so comparing the
                # serialized value would silently fail to drop the field for
                # those types. Using getattr(self, name) is also symmetric with
                # _check_add_compatibility_defaults, which injects the raw
                # compat_default on the validate side. Hash mode only: the
                # body keeps the value, since it is what a rehydrated task
                # should carry.
                if (
                    stardag_field.compat_default is not _UNSET
                    and getattr(self, name) == stardag_field.compat_default
                ):
                    continue

            out[name] = canonicalize_sets(
                getattr(self, name, None), value, info.context
            )

        if mode == "registry":
            return out
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
