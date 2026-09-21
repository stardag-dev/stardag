"""
Implements a base model with advanced polymorphic serialization + validation features:

1) Polymorph registry (namespace + class name) with:
   - ALWAYS include discriminator keys in serialization for *all* MyBase subclasses
   - Polymorphic ("registered subclass") validation + "serialize as any" is OPT-IN per field via SubClass[T]

2) Context-based custom serialization + validation modes (mode in context):
   - mode="hash": drop fields annotated BackwardCompat(default=...) when value == default,
     and every field whose significance is not "identity"
   - mode="registry": drop every field with an explicit non-identity
     significance (a legacy ``hash_exclude=True`` field is kept: it may still
     be passed at init, so its value must survive a round trip), keep
     everything else (the payload the registry stores — a pure function of the
     task id)
   - mode="compat": if a BackwardCompat field is missing, populate it with the compat
     default; a non-identity field present in the input is dropped rather than
     refused (registry data written before significance existed)

3) Auto-register any child class of MyBase when declared (via __init_subclass__).

4) Three levels of parameter significance (see ``StardagField.significance``):
   a non-identity field cannot be passed at init and is resolved from the
   build config instead (see ``stardag.build_config``).
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass
from typing import Any, Literal, Type, TypeVar, get_args

from pydantic import (
    BaseModel,
    ConfigDict,
    SerializationInfo,
    ValidationInfo,
    model_serializer,
    model_validator,
)
from pydantic.fields import FieldInfo

from stardag.build_config import (
    register_build_config_class,
    resolve_field_value,
    task_config_key,
)

SerializationContextMode = Literal["hash", "registry", None]
ValidationContextMode = Literal["compat", None]
CONTEXT_MODE_KEY = "mode"

Significance = Literal["identity", "dependencies_only", "execution_only"]
"""What a parameter is significant *for*.

- ``"identity"`` (the default): the output. Part of the task id and of the
  registered ``task_data``; passed at init like any parameter.
- ``"dependencies_only"``: the upstream set — static or dynamic — but not the
  output. A fan-out partition size. Read from the build config, never passed
  at init; hashed into the build's structure scope.
- ``"execution_only"``: neither output nor structure, only how the work is
  done. A thread count. Read from the build config, never passed at init;
  not part of any hash.

See ``docs/design/scope-keyed-dependency-structure.md``.
"""


_UNSET = object()


@dataclass(frozen=True)
class StardagField:
    """Per-field annotation controlling hash-mode serialization and compat
    validation. Attach via ``Annotated[T, StardagField(...)]``."""

    compat_default: Any = _UNSET
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

    Only meaningful on an identity field: a non-identity field's default lives
    in code, and the code identity already covers it, so combining the two is
    refused.
    """
    hash_exclude: bool = False
    """Deprecated: use ``significance="execution_only"``.

    Drops the field from the hash dump, which is what ``execution_only`` does
    — with one difference: a field marked this way could be passed at init,
    and an ``execution_only`` field cannot. The old option keeps working for
    one release, with a warning; the new levels enforce immediately.
    """
    significance: Significance = "identity"
    """What the parameter is significant for; see :data:`Significance`.

    A ``dependencies_only`` or ``execution_only`` field should carry a
    default: the build config overrides it, and without one every
    constructor call would demand a value that only the config may supply.
    """

    def __post_init__(self) -> None:
        if self.significance not in get_args(Significance):
            # A Literal is a hint, not a check; an unchecked typo would read
            # as non-identity here and as execution-only in the structure
            # hash, so two different dependency configs could share a scope.
            raise ValueError(
                f"StardagField(significance={self.significance!r}): expected one "
                f"of {', '.join(repr(v) for v in get_args(Significance))}."
            )
        if self.hash_exclude:
            warnings.warn(
                "StardagField(hash_exclude=True) is deprecated; use "
                'StardagField(significance="execution_only") and supply the '
                "value through the build config instead of at init.",
                DeprecationWarning,
                stacklevel=3,
            )
        if self.compat_default is not _UNSET and self.significance != "identity":
            raise ValueError(
                "compat_default has no effect on a non-identity field: its "
                "default lives in code, which the code identity already covers. "
                f"Drop compat_default or make the field identity-significant "
                f"(got significance={self.significance!r})."
            )
        if self.hash_exclude and self.significance != "identity":
            # The two disagree about init: a legacy hash_exclude field may be
            # passed at init for one release, an explicit non-identity field
            # may not. One or the other, never both.
            raise ValueError(
                "hash_exclude=True is the deprecated spelling of "
                'significance="execution_only"; combining it with an explicit '
                f"significance={self.significance!r} is contradictory. Drop "
                "hash_exclude."
            )

    @property
    def is_identity(self) -> bool:
        """Whether the field is part of the task's identity."""
        return self.significance == "identity" and not self.hash_exclude

    @property
    def is_legacy_hash_exclude(self) -> bool:
        """The deprecated form: ``hash_exclude=True`` with no explicit
        significance. Dropped from the hash like an ``execution_only`` field,
        but still accepted at init and kept in the registry payload for one
        release. Readers should use this and :attr:`is_build_config_field`
        rather than the raw pair."""
        return self.hash_exclude and self.significance == "identity"

    @property
    def is_build_config_field(self) -> bool:
        """An explicit ``dependencies_only`` / ``execution_only`` field: never
        passed at init, never hashed, never stored; resolved from the build
        config."""
        return self.significance != "identity"

    @property
    def effective_significance(self) -> Significance:
        """``significance``, with the deprecated ``hash_exclude`` folded in."""
        if self.significance != "identity":
            return self.significance
        return "execution_only" if self.hash_exclude else "identity"


def field_significance(field: FieldInfo) -> Significance:
    """The significance of a pydantic field: from its ``StardagField``
    annotation, ``"identity"`` when it has none."""
    meta = _get_annotation(field, StardagField)
    return meta.effective_significance if meta is not None else "identity"


class StardagBaseModel(BaseModel):
    """Custom, swap-in-replace for pydantic BaseModel, with features for hash mode +
    compat mode.

    Implements:
      - Validation with info.context["mode"] == "compat":
        - defaults for fields marked BackwardCompat(default=...)
        - non-identity fields present in the input are dropped (old data)
      - Serialization with info.context["mode"] == "hash":
        - dropping of BackwardCompat-default-valued fields on dump
        - dropping fields whose significance is not "identity"
      - Serialization with info.context["mode"] == "registry":
        - dropping fields whose significance is not "identity", nothing else
      - Non-identity fields are resolved from the build config at validation
        and refused at init (see ``stardag.build_config``)

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
        """Compat defaults, and the non-identity fields' single mechanism.

        For every field whose significance is not ``identity``:

        - present in the input under plain init → refused. Passing it is
          what would let two downstreams build two versions of one upstream
          under one task id; see ``stardag.build_config``.
        - present in the input under ``mode="compat"`` (registry data,
          possibly written before significance existed) → dropped, then
          resolved like an absent one.
        - absent → resolved from the installed build config when it names
          this class and field; otherwise the field's own default applies.
        """
        mode: ValidationContextMode = (
            info.context.get(CONTEXT_MODE_KEY) if info.context else None
        )
        if not isinstance(data, dict):
            return data

        data = dict(data)
        non_identity = cls._non_identity_fields()
        if non_identity:
            config_key = cls._build_config_key()
            for name in non_identity:
                if name in data:
                    if mode != "compat":
                        raise ValueError(
                            f"{cls.__name__}.{name} has significance="
                            f"{field_significance(cls.model_fields[name])!r} and "
                            "is read from the build config; it cannot be passed "
                            "at init. Set it through build_config "
                            f'({{"{config_key}": {{"{name}": ...}}}}) or a '
                            "stardag.build_config_scope(...) block."
                        )
                    data.pop(name)
                found, value = resolve_field_value(config_key, name)
                if found:
                    data[name] = value

        if mode != "compat":
            return data

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

    @classmethod
    def _non_identity_fields(cls) -> tuple[str, ...]:
        """Names of the fields whose significance is not ``identity``.

        Cached per class on first use; ``model_fields`` is fixed once the
        class is built.
        """
        cached = cls.__dict__.get("__stardag_non_identity_fields__")
        if cached is not None:
            return cached
        # Build-config fields only, not the legacy ``hash_exclude`` form: a
        # legacy field is excluded from the hash like an execution_only
        # field, but it may still be passed at init and is kept in the
        # registry payload for one release — that is the whole difference
        # the deprecation note describes.
        names = tuple(
            name
            for name, field in cls.model_fields.items()
            if (meta := _get_annotation(field, StardagField)) is not None
            and meta.is_build_config_field
        )
        try:
            setattr(cls, "__stardag_non_identity_fields__", names)
        except (AttributeError, TypeError):  # pragma: no cover - exotic classes
            pass
        return names

    @classmethod
    def __pydantic_init_subclass__(cls, **kwargs: Any) -> None:
        super().__pydantic_init_subclass__(**kwargs)
        # A class the build config can name has to be findable by that name
        # when the structure scope is hashed, not only when a field is
        # resolved. Tasks are found through the task registry; this covers
        # the rest. See ``stardag.build_config.register_build_config_class``.
        register_build_config_class(cls)

    @classmethod
    def _build_config_key(cls) -> str:
        """The build-config key for this class. A class registered in a
        polymorphic family uses the namespace and name it was registered
        under; anything else its ``__namespace__`` (usually unset) and class
        name — the same shape, and the escape hatch when two plain models
        would otherwise share a bare name.

        ``__type_id__`` has to be this class's own. It is inherited like any
        class attribute, and the classes a family does not register — an
        abstract base, a family root — would otherwise answer with their
        nearest registered ancestor's key, which belongs to a different
        class.
        """
        if "__type_id__" in cls.__dict__:
            get_namespace = getattr(cls, "get_namespace", None)
            get_name = getattr(cls, "get_name", None)
            if callable(get_namespace) and callable(get_name):
                try:
                    return task_config_key(str(get_namespace()), str(get_name()))
                except AttributeError:  # pragma: no cover - defensive
                    pass
        return task_config_key(
            str(getattr(cls, "__namespace__", "") or ""), cls.__name__
        )

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
        if mode not in ("hash", "registry") or not isinstance(data, dict):
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
                # A build-config field is neither hashed nor stored: it is
                # not part of what the task promises, and its value comes
                # from the build config, never from the payload.
                if stardag_field.is_build_config_field:
                    continue
                # The legacy form is dropped from the hash but KEPT in the
                # registry payload: it may still be passed at init for one
                # release, so a task registered with a non-default value
                # must rehydrate with that value, not the default.
                if stardag_field.is_legacy_hash_exclude and mode == "hash":
                    continue
                # Compare the *raw* Python value (not the already-serialized
                # `value`) against compat_default. The serialized form differs
                # from the Python value for enums (-> .value), tuples (-> list),
                # and fields with custom/hash-only serializers, so comparing the
                # serialized value would silently fail to drop the field for
                # those types. Using getattr(self, name) is also symmetric with
                # _check_add_compatibility_defaults, which injects the raw
                # compat_default on the validate side. Hash mode only: the
                # registry payload keeps the value, since it is what a
                # rehydrated task should carry.
                if (
                    mode == "hash"
                    and stardag_field.compat_default is not _UNSET
                    and getattr(self, name) == stardag_field.compat_default
                ):
                    continue

            out[name] = value

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
