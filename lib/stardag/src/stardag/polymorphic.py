import abc
import inspect
import logging
import os
import types
import typing
import warnings
from dataclasses import dataclass
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    ClassVar,
    Literal,
    Tuple,
    Type,
    TypeVar,
    Union,
    get_args,
    get_origin,
)

from pydantic import BaseModel, GetCoreSchemaHandler, SerializationInfo, ValidationInfo
from pydantic_core import core_schema

from stardag.base_model import StardagBaseModel
from stardag.exceptions import StardagError

logger = logging.getLogger(__name__)

OnGenericTypeMismatch = Literal["raise", "warn", "ignore"]


def _is_type_compatible(expected: Any, actual: Any) -> bool:
    """
    Best-effort check if actual type is compatible with expected type.

    Returns True if compatible or if we can't determine compatibility.
    Returns False only for obvious mismatches.
    """
    # Unwrap Annotated types — metadata is not a type constraint.
    # e.g. Annotated[str, SomeTag] is treated the same as str.
    # Loop to handle nested Annotated (e.g. Annotated[Annotated[str, A], B]).
    # Guard with `get_args` to avoid IndexError on bare `Annotated` with no args.
    while get_origin(expected) is Annotated and get_args(expected):
        expected = get_args(expected)[0]
    while get_origin(actual) is Annotated and get_args(actual):
        actual = get_args(actual)[0]

    # TypeVars match anything
    if isinstance(expected, TypeVar):
        return True
    if isinstance(actual, TypeVar):
        return True

    # Same type is always compatible
    if expected is actual:
        return True
    if expected == actual:
        return True

    # Handle generic types (e.g., LoadableTarget[str] vs LoadableTarget[int])
    expected_origin = get_origin(expected)
    actual_origin = get_origin(actual)

    if expected_origin is not None and actual_origin is not None:
        # Both are generic types - check origin compatibility
        if expected_origin is not actual_origin:
            # Different origins - check if actual_origin is subclass of expected_origin
            if isinstance(expected_origin, type) and isinstance(actual_origin, type):
                if not issubclass(actual_origin, expected_origin):
                    return False
            else:
                return False

        # Check args recursively
        expected_args = get_args(expected)
        actual_args = get_args(actual)

        if len(expected_args) != len(actual_args):
            return False

        for exp_arg, act_arg in zip(expected_args, actual_args):
            if not _is_type_compatible(exp_arg, act_arg):
                return False

        return True

    # One is generic, the other is not - incompatible
    # e.g., str vs list[str], or list[int] vs int
    if (expected_origin is None) != (actual_origin is None):
        return False

    # Handle simple class types
    if isinstance(expected, type) and isinstance(actual, type):
        return issubclass(actual, expected)

    # Can't determine - assume compatible
    return True


def _check_generic_args_compatibility(
    source_type: type, value_cls: type
) -> tuple[bool, str]:
    """
    Check if value_cls's generic args are compatible with source_type's expected args.

    Returns (is_compatible, error_message).
    error_message is empty string if compatible or if check is inconclusive.
    """
    # Get expected args from source_type's pydantic metadata
    pydantic_meta = getattr(source_type, "__pydantic_generic_metadata__", None)
    if pydantic_meta is None:
        return True, ""

    expected_args = pydantic_meta.get("args", ())
    if not expected_args:
        return True, ""

    expected_origin = pydantic_meta.get("origin")

    # Get actual args from value_cls's __orig_class__ (set by PolymorphicRoot.__class_getitem__)
    orig_class = getattr(value_cls, "__orig_class__", None)
    if orig_class is None:
        return True, ""

    actual_origin = get_origin(orig_class)
    actual_args = get_args(orig_class)
    if not actual_args:
        return True, ""

    # Handle different origins (e.g., TargetTask vs Task) by checking if the actual
    # class provides a mapping to translate its generic args to the expected origin
    if expected_origin is not None and actual_origin is not None:
        if expected_origin is not actual_origin:
            # Try to get mapped args from the actual origin
            mapper = getattr(actual_origin, "__map_generic_args_to_ancestor__", None)
            if mapper is not None:
                mapped_args = mapper(expected_origin, actual_args)
                if mapped_args is not None:
                    actual_args = mapped_args
                else:
                    # Mapping not applicable - can't reliably compare args
                    return True, ""
            else:
                # No mapper available - can't reliably compare args
                return True, ""

    # Compare args
    if len(expected_args) != len(actual_args):
        return (
            False,
            f"Generic arity mismatch: expected {len(expected_args)} type args, "
            f"got {len(actual_args)}",
        )

    for i, (exp, act) in enumerate(zip(expected_args, actual_args)):
        if not _is_type_compatible(exp, act):
            return (
                False,
                f"Generic type mismatch at position {i}: expected {exp}, got {act}",
            )

    return True, ""


NAMESPACE_KEY = "__namespace"
NAME_KEY = "__name"


@dataclass(frozen=True)
class TypeId:
    namespace: str
    name: str


class _TypeRegistry:
    def __init__(self):
        self._type_id_to_class: dict[TypeId, Type[BaseModel]] = {}
        self._class_to_type_id: dict[Type[BaseModel], TypeId] = {}
        self._module_to_namespace: dict[str, str] = {}

    def add_namespace(self, module: str, namespace: str | None = None):
        """Add ("register") a namespace for a module.

        Models defined in this module (or submodules) will get this namespace unless
        overridden on the model class itself.

        Args:
            module: Module name, e.g. "mypackage.mysubmodule", typically
                obtained via `__name__` in the module.
            namespace: Namespace to assign to models in this module. If None,
                the module name is used as namespace.

        Returns:
            The assigned namespace.
        """
        namespace = namespace or module
        self._module_to_namespace[module] = namespace

        return namespace

    def get_class(self, type_id: TypeId) -> Type[BaseModel]:
        """Get registered model class by namespace and name."""
        cls = self._type_id_to_class.get(type_id)
        if cls is None:
            raise KeyError(f"No class registered for type id: {type_id}")
        return cls

    def get_type_id(self, cls: Type[BaseModel]) -> TypeId:
        """Get registered type id for a model class."""
        type_id = self._class_to_type_id.get(cls)
        if type_id is None:
            raise KeyError(f"Class not registered: {cls}")
        return type_id

    def add(
        self,
        cls: Type[BaseModel],
        name_override: str | None,
        namespace_override: str | None,
    ) -> TypeId:
        if cls in self._class_to_type_id:
            raise ValueError(f"Class already registered: {cls}")

        type_id = self._resolve_type_id(
            cls,
            name_override=name_override,
            namespace_override=namespace_override,
        )
        self._class_to_type_id[cls] = type_id
        logger.debug(
            f"\nRegistering task class: {cls}\n"
            f"  type_id: {type_id}\n"
            f"  module.name: {cls.__module__}.{cls.__name__}\n"
            f"  __orig_bases__: {getattr(cls, '__orig_bases__', None)}\n"
            "  __pydantic_generic_metadata__: "
            f"{cls.__pydantic_generic_metadata__}\n"
        )
        existing = self._type_id_to_class.get(type_id)
        if existing:
            if (existing.__module__ == cls.__module__) and (
                existing.__name__ == cls.__name__
            ):
                # NOTE/TODO issue when cloudpickling
                logger.info(f"Class already registered: {cls} (type_id: {type_id})")
                return type_id

            error_msg = (
                "A class is already registered for the "
                f'type_id "{type_id}".\n'
                f"Existing: {existing.__module__}.{existing.__name__}\n"
                f"New: {cls.__module__}.{cls.__name__}"
            )
            raise ValueError(error_msg)
        self._type_id_to_class[type_id] = cls

        return type_id

    def _resolve_type_id(
        self,
        cls: Type[BaseModel],
        name_override: str | None,
        namespace_override: str | None,
    ) -> TypeId:
        return TypeId(
            name=self._resolve_name(cls, name_override),
            namespace=self._resolve_namespace(
                cls,
                namespace_override=namespace_override,
            ),
        )

    def _resolve_name(
        self,
        cls: Type[BaseModel],
        name_override: str | None,
    ) -> str:
        if name_override is not None:
            return name_override

        # Use Python's built-in class __name__
        return cls.__name__

    def _resolve_namespace(
        self,
        cls: Type[BaseModel],
        namespace_override: str | None,
    ) -> str:
        if namespace_override is not None:
            return namespace_override

        cls_namespace = getattr(cls, "__namespace__", None)
        if cls_namespace is not None:
            # Already set explicitly on class
            return cls_namespace

        # check if set by module or any parent module
        module_parts = cls.__module__.split(".")
        for idx in range(len(module_parts), 0, -1):
            module = ".".join(module_parts[:idx])
            namespace = self._module_to_namespace.get(module)
            if namespace:
                return namespace

        # No namespace set
        return ""


def is_generic_model(cls: Type[BaseModel]) -> bool:
    meta = cls.__pydantic_generic_metadata__
    if meta["origin"] or meta["parameters"]:
        return True
    return False


def _is_parameterized_generic_alias(cls: Type[BaseModel]) -> bool:
    """True for parameterized generic aliases like ``Task[int]``.

    These aren't real classes and must never be registered: the concrete subclass
    that extends them (``class MyTask(Task[int]): pass``) is what carries the id.
    """
    return bool(cls.__pydantic_generic_metadata__.get("origin"))


def _is_stardag_abstract(cls: type) -> bool:
    """True if this class is explicitly marked as an abstract stardag base.

    Only honored when set directly on the class (via ``cls.__dict__``), not
    inherited — so user subclasses of abstract bases still get registered.
    """
    return cls.__dict__.get("__stardag_abstract__", False) is True


class NakedPolymorphicFieldError(StardagError):
    """Raised at class-construction time for an unsafe polymorphic field annotation.

    A field annotated directly with an *abstract* ``PolymorphicRoot`` subclass
    (e.g. ``child: BaseTask``) rather than wrapping it in ``SubClass[...]`` /
    ``Annotated[..., Polymorphic()]`` is a silent data-loss trap: serialization
    keeps only the abstract base's fields (dropping every subclass-specific
    parameter) and deserialization then fails trying to instantiate the abstract
    base directly. This error rejects such annotations up front rather than
    letting them corrupt persisted data.
    """


def _is_abstract_polymorphic(cls: type) -> bool:
    """True if ``cls`` is a ``PolymorphicRoot`` subclass that cannot be safely
    used as a *bare* field annotation because it is an abstract base.

    Any value assigned to such a field is necessarily a concrete subclass whose
    extra parameters would be silently dropped on serialization; on load the
    framework would try to instantiate the abstract base and crash. Concrete
    ``PolymorphicRoot`` subclasses are intentionally *allowed* as bare ("strict")
    annotations and are therefore not considered abstract here.
    """
    # Resolve a parameterized generic alias (e.g. ``Task[int]``) to its origin
    # (``Task``); the abstractness markers live on the origin class.
    meta = getattr(cls, "__pydantic_generic_metadata__", None)
    origin = (meta.get("origin") if meta else None) or cls
    return (
        inspect.isabstract(origin)
        or _is_stardag_abstract(origin)
        or abc.ABC in getattr(origin, "__bases__", ())
    )


_TPolymorphicRoot = TypeVar("_TPolymorphicRoot", bound="PolymorphicRoot")
_TBaseModel = TypeVar("_TBaseModel", bound=StardagBaseModel)


class PolymorphicRoot(StardagBaseModel):
    """Base class for a polymorphic family.

    Each subclass family has its own registry stored on the base class. Subclasses are
    automatically registered unless they are generic models.

    Subclasses can override the default type id resolution by either providing the
    class constructor arguments `name_override` and `namespace_override` or setting
    the class variable `__namespace__`. NOTE that if the class variable is set
    directly, all subclasses will inherit the same value, so it should typically
    only be used for the family root class.

    Namespace can also be registered per-module via the registry of the base class
    extending PolymorphicRoot (TODO: make _registry a public API for this).

    Args:
        name_override: Optional explicit name for this class. If None, the class
            `__name__` is used.
        namespace_override: Optional explicit namespace for this class. If None,
            the module name (or registered module namespace) is used.
    """

    # IMPORTANT: per-family registry lives on the base class
    __registry__: ClassVar[_TypeRegistry] = _TypeRegistry()

    if TYPE_CHECKING:
        # Optionally set on subclasses to override default namespace resolution
        __namespace__: ClassVar[str]
        __type_id__: ClassVar[TypeId]

    @classmethod
    def _registry(cls) -> _TypeRegistry:
        # If you ever want a deep hierarchy, you can ensure the registry is owned by the root
        # but in many cases, "cls" itself is the desired owner.
        return cls.__registry__

    @classmethod
    def resolve(
        cls: type[_TPolymorphicRoot],
        namespace: str,
        name: str,
        extra: dict[str, Any],
    ) -> type[_TPolymorphicRoot]:
        type_id = TypeId(namespace=namespace, name=name)
        sub = cls._registry().get_class(type_id)
        # narrow + safety: only allow subclasses of the annotated base
        if not issubclass(sub, cls):
            raise TypeError(f"Registered class {sub} is not a subclass of {cls}")
        return sub  # type: ignore[return-value]

    @classmethod
    def __init_subclass__(
        cls,
        name_override: str | None = None,
        namespace_override: str | None = None,
        **kwargs: Any,
    ) -> None:
        # Need to avoid forwarding name_override and namespace_override to BaseModel
        super().__init_subclass__(**kwargs)

    @classmethod
    def __pydantic_init_subclass__(
        cls,
        name_override: str | None = None,
        namespace_override: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__pydantic_init_subclass__(**kwargs)

        # Direct child => new independent registry (new family)
        if PolymorphicRoot in cls.__bases__:
            cls.__registry__ = _TypeRegistry()
        else:
            # Find family root: the first base that is a direct child of PolymorphicRoot
            family: Type[PolymorphicRoot] = next(
                base
                for base in cls.__mro__
                if PolymorphicRoot in getattr(base, "__bases__", ())
            )
            if (
                cls is not family
                and not _is_parameterized_generic_alias(cls)
                and not _is_stardag_abstract(cls)
            ):
                cls.__type_id__ = family._registry().add(
                    cls,
                    name_override=name_override,
                    namespace_override=namespace_override,
                )

        # Reject unsafe bare abstract-polymorphic field annotations at class
        # construction time (see NakedPolymorphicFieldError). Skip parameterized
        # generic aliases (e.g. ``Task[int]``) — they mirror the origin class's
        # fields, which are validated when the origin itself is defined.
        if not _is_parameterized_generic_alias(cls):
            _validate_no_naked_polymorphic_fields(cls)

    def __class_getitem__(
        cls: Type[BaseModel],
        params: Union[Type[Any], Tuple[Type[Any], ...]],
    ) -> Type[Any]:
        """Hack to be able to access the generic type of the class from subclasses. See:
        https://github.com/pydantic/pydantic/discussions/4904#discussioncomment-4592052
        """
        create_model = super().__class_getitem__(params)  # type: ignore

        # Store params so get_args(cls.__orig_class__) returns them
        # Using types.GenericAlias so get_args() works correctly
        args = params if isinstance(params, tuple) else (params,)
        create_model.__orig_class__ = types.GenericAlias(cls, args)  # type: ignore
        return create_model

    def _serialize_extra(
        self,
        data: Any,
        info: SerializationInfo,
    ):
        """Always add discriminator keys. This runs for all subclasses too."""
        if isinstance(data, dict):
            tid = self.__class__.__type_id__
            data = {
                NAMESPACE_KEY: tid.namespace,
                NAME_KEY: tid.name,
                **data,
            }
        return data

    @classmethod
    def _before_validate(cls, payload: Any, info: ValidationInfo) -> Any:
        """No-op placeholder to ensure PolymorphicRoot subclasses have this method."""
        if not isinstance(payload, dict):
            return payload

        payload = dict(payload)
        payload.pop(NAMESPACE_KEY, None)
        payload.pop(NAME_KEY, None)

        return payload

    @classmethod
    def get_name(cls) -> str:
        """Get the name for this class."""
        return cls.__type_id__.name

    @classmethod
    def get_namespace(cls) -> str:
        """Get the namespace for this class."""
        return cls.__type_id__.namespace


ON_GENERIC_TYPE_MISMATCH_ENV_VAR = "STARDAG_POLYMORPHIC_ON_GENERIC_TYPE_MISMATCH"
_DEFAULT_ON_GENERIC_TYPE_MISMATCH: OnGenericTypeMismatch = "warn"


def _resolve_on_generic_type_mismatch(
    explicit: OnGenericTypeMismatch | None,
) -> OnGenericTypeMismatch:
    """Resolve the on-mismatch mode from explicit arg, env var, or default.

    Explicit (non-None) arg always wins. Otherwise reads
    ``STARDAG_POLYMORPHIC_ON_GENERIC_TYPE_MISMATCH``, falling back to
    ``"warn"`` when unset.
    """
    if explicit is not None:
        return explicit
    env_val = os.environ.get(ON_GENERIC_TYPE_MISMATCH_ENV_VAR)
    if env_val is None:
        return _DEFAULT_ON_GENERIC_TYPE_MISMATCH
    allowed = typing.get_args(OnGenericTypeMismatch)
    if env_val not in allowed:
        raise ValueError(
            f"Invalid value for env var {ON_GENERIC_TYPE_MISMATCH_ENV_VAR}: "
            f"{env_val!r}. Expected one of {allowed}."
        )
    return typing.cast(OnGenericTypeMismatch, env_val)


class Polymorphic:
    """Pydantic annotation for polymorphic validation of PolymorphicRoot subclasses.

    Args:
        on_generic_type_mismatch: Behavior when generic type args don't match.
            - ``"raise"``: Raise a ValidationError
            - ``"warn"``: Emit a warning but accept the value
            - ``"ignore"``: Silently accept the value
            - ``None`` (default): Resolve at validation time from env var
              ``STARDAG_POLYMORPHIC_ON_GENERIC_TYPE_MISMATCH``; if that env
              var is unset, fall back to ``"warn"``. An explicit non-None
              value always overrides the env var.
    """

    on_generic_type_mismatch: OnGenericTypeMismatch | None

    def __init__(
        self,
        on_generic_type_mismatch: OnGenericTypeMismatch | None = None,
    ) -> None:
        self.on_generic_type_mismatch = on_generic_type_mismatch

    def __get_pydantic_core_schema__(
        self,
        source_type: Any,
        handler: GetCoreSchemaHandler,
    ):
        _ = handler(source_type)  # ensure schema exists

        # Resolve TypeVars to their bound so that generic Pydantic models can
        # declare fields like ``field: SubClass[T]`` where ``T`` is a TypeVar
        # bound to a PolymorphicRoot subclass. At schema-build time for the
        # generic class, source_type is the TypeVar; at schema-build time for
        # the parameterized form (``MyModel[Concrete]``), Pydantic re-invokes
        # this method with the concrete class, so the TypeVar branch only
        # shapes the generic-form schema.
        resolved_source = source_type
        if isinstance(source_type, TypeVar):
            bound = source_type.__bound__
            if not (isinstance(bound, type) and issubclass(bound, PolymorphicRoot)):
                raise TypeError(
                    "Polymorphic() used with a TypeVar requires the TypeVar "
                    f"to be bound to a PolymorphicRoot subclass; got "
                    f"TypeVar {source_type!r} with bound {bound!r}"
                )
            resolved_source = bound

        if not isinstance(resolved_source, type) or not issubclass(
            resolved_source, PolymorphicRoot
        ):
            raise TypeError(
                "Polymorphic() can only be used with PolymorphicRoot subclasses"
            )

        # For parameterized generics like Task[LoadableTarget[str]], get the origin class
        # (Task) for isinstance checks, but keep source_type for generic args checking
        pydantic_meta = getattr(resolved_source, "__pydantic_generic_metadata__", None)
        base_origin: type[PolymorphicRoot] = (
            pydantic_meta.get("origin") if pydantic_meta else None
        ) or resolved_source

        explicit_on_mismatch = self.on_generic_type_mismatch

        def dispatch(v: Any, info):
            if isinstance(v, base_origin):
                # Best-effort generic args check for already-instantiated values
                is_compatible, error_msg = _check_generic_args_compatibility(
                    resolved_source, type(v)
                )
                if not is_compatible:
                    message = (
                        f"Value of type {type(v).__name__} is not compatible with "
                        f"expected type {resolved_source}: {error_msg}"
                    )
                    on_mismatch = _resolve_on_generic_type_mismatch(
                        explicit_on_mismatch
                    )
                    if on_mismatch == "raise":
                        raise ValueError(message)
                    elif on_mismatch == "warn":
                        warnings.warn(
                            f"{message} (suppress by setting "
                            f"{ON_GENERIC_TYPE_MISMATCH_ENV_VAR}=ignore)",
                            UserWarning,
                            stacklevel=2,
                        )
                return v

            if not isinstance(v, dict):
                return base_origin.model_validate(v, context=info.context)

            namespace = v.get(NAMESPACE_KEY)
            name = v.get(NAME_KEY)
            if namespace is None or name is None:
                raise ValueError(
                    f"Missing discriminator keys: {NAMESPACE_KEY}, {NAME_KEY}"
                )

            double_underscore_kwargs = {
                key: value
                for key, value in v.items()
                if key.startswith("__")
                and key
                not in (
                    NAMESPACE_KEY,
                    NAME_KEY,
                )
            }

            subcls = base_origin.resolve(
                str(namespace),
                str(name),
                extra=double_underscore_kwargs,
            )

            payload = subcls._before_validate(v, info)

            return subcls.model_validate(payload, context=info.context)

        return core_schema.with_info_plain_validator_function(
            dispatch,
            json_schema_input_schema=core_schema.any_schema(),
            serialization=core_schema.plain_serializer_function_ser_schema(
                lambda v: v,
                return_schema=core_schema.any_schema(),
            ),
        )


def _find_naked_polymorphic(annotation: Any, guarded: bool) -> type | None:
    """Return the first abstract ``PolymorphicRoot`` subclass reachable from
    ``annotation`` that is *not* wrapped in ``Polymorphic()`` / ``SubClass[...]``,
    or ``None`` if the annotation is safe.

    ``guarded`` is True when the current position is directly wrapped by a
    ``Polymorphic()`` annotation. The guard does not propagate through containers
    (``list``, ``dict``, unions, ...): a ``Polymorphic()`` only applies to the
    type it directly annotates, so recursing into container args resets it.
    """
    if get_origin(annotation) is Annotated:
        args = get_args(annotation)
        inner, extras = args[0], args[1:]
        guarded_here = guarded or any(isinstance(e, Polymorphic) for e in extras)
        return _find_naked_polymorphic(inner, guarded_here)

    if isinstance(annotation, type) and issubclass(annotation, PolymorphicRoot):
        if not guarded and _is_abstract_polymorphic(annotation):
            return annotation
        # Guarded, or a concrete "strict" annotation — both fine. Don't recurse
        # into the model's own fields; those are validated on their own class.
        return None

    # Container / union / other generic: recurse into type args with the guard
    # reset (a Polymorphic() cannot legally wrap a container).
    for arg in get_args(annotation):
        found = _find_naked_polymorphic(arg, False)
        if found is not None:
            return found
    return None


def _naked_polymorphic_field_message(
    cls: type, field_name: str, offending: type
) -> str:
    offending_name = getattr(offending, "__name__", str(offending))
    return (
        f"Field '{field_name}' on '{cls.__name__}' is annotated with the abstract "
        f"polymorphic type '{offending_name}' without polymorphic handling.\n\n"
        f"A bare abstract-base annotation silently drops subclass-specific "
        f"parameters when the model is serialized, and then fails to deserialize "
        f"(it would try to instantiate the abstract base directly). Wrap the type "
        f"with SubClass[...] (or Annotated[..., Polymorphic()]):\n\n"
        f"    from stardag import SubClass\n\n"
        f"    {field_name}: SubClass[{offending_name}]\n\n"
        f"For a container field, wrap the inner type, e.g. "
        f"'list[SubClass[{offending_name}]]' or "
        f"'dict[str, SubClass[{offending_name}]]'."
    )


def _validate_no_naked_polymorphic_fields(cls: type["PolymorphicRoot"]) -> None:
    """Raise ``NakedPolymorphicFieldError`` if any field of ``cls`` uses an
    abstract ``PolymorphicRoot`` subclass as a bare (non-polymorphic) annotation.
    """
    for name, field in cls.model_fields.items():
        # A top-level ``SubClass[X]`` lands here as annotation ``X`` plus a
        # ``Polymorphic()`` in ``field.metadata``; treat that as guarding the
        # field's annotation.
        guarded = any(isinstance(m, Polymorphic) for m in field.metadata)
        offending = _find_naked_polymorphic(field.annotation, guarded)
        if offending is not None:
            raise NakedPolymorphicFieldError(
                _naked_polymorphic_field_message(cls, name, offending)
            )


class _SubClass:
    """Syntactic sugar: `SubClass[T] -> Annotated[T, Polymorphic()]`"""

    def __class_getitem__(cls, item):
        # If suport for kwargs needed in future:
        # if isinstance(item, tuple):
        #     base, *rest = item
        #     kwargs: dict[str, Any] = {}
        #     for r in rest:
        #         if isinstance(r, dict):
        #             kwargs.update(r)
        #     return Annotated[base, Polymorphic(**kwargs)]
        return Annotated[item, Polymorphic()]


if typing.TYPE_CHECKING:
    SubClass: typing.TypeAlias = typing.Annotated[_TBaseModel, "polymorphic"]
else:
    SubClass = _SubClass
