import logging
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

from pydantic import (
    BaseModel,
    GetCoreSchemaHandler,
    SerializationInfo,
)
from pydantic_core import core_schema

from stardag.base_model import StardagBaseModel

logger = logging.getLogger(__name__)

OnGenericTypeMismatch = Literal["raise", "warn", "ignore"]


def _is_type_compatible(expected: Any, actual: Any) -> bool:
    """
    Best-effort check if actual type is compatible with expected type.

    Returns True if compatible or if we can't determine compatibility.
    Returns False only for obvious mismatches.
    """
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

    # Handle different origins (e.g., Task vs AutoTask) by checking if the actual
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
        cls: type[_TPolymorphicRoot], namespace: str, name: str
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
            if cls is not family and not is_generic_model(cls):
                cls.__type_id__ = family._registry().add(
                    cls,
                    name_override=name_override,
                    namespace_override=namespace_override,
                )

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
    def get_name(cls) -> str:
        """Get the name for this class."""
        return cls.__type_id__.name

    @classmethod
    def get_namespace(cls) -> str:
        """Get the namespace for this class."""
        return cls.__type_id__.namespace


class Polymorphic:
    """Pydantic annotation for polymorphic validation of PolymorphicRoot subclasses.

    Args:
        on_type_mismatch: Behavior when generic type args don't match.
            - "raise" (default): Raise a ValidationError
            - "warn": Log a warning but accept the value
            - "ignore": Silently accept the value
    """

    def __init__(
        self,
        on_generic_type_mismatch: OnGenericTypeMismatch = "raise",
    ) -> None:
        self.on_generic_type_mismatch = on_generic_type_mismatch

    def __get_pydantic_core_schema__(
        self,
        source_type: Any,
        handler: GetCoreSchemaHandler,
    ):
        _ = handler(source_type)  # ensure schema exists

        # require source_type to be a PolymorphicRoot subclass
        if not isinstance(source_type, type) or not issubclass(
            source_type, PolymorphicRoot
        ):
            raise TypeError(
                "Polymorphic() can only be used with PolymorphicRoot subclasses"
            )

        # For parameterized generics like Task[LoadableTarget[str]], get the origin class
        # (Task) for isinstance checks, but keep source_type for generic args checking
        pydantic_meta = getattr(source_type, "__pydantic_generic_metadata__", None)
        base_origin: type[PolymorphicRoot] = (
            pydantic_meta.get("origin") if pydantic_meta else None
        ) or source_type

        on_generic_type_mismatch = self.on_generic_type_mismatch

        def dispatch(v: Any, info):
            if isinstance(v, base_origin):
                # Best-effort generic args check for already-instantiated values
                is_compatible, error_msg = _check_generic_args_compatibility(
                    source_type, type(v)
                )
                if not is_compatible:
                    message = (
                        f"Value of type {type(v).__name__} is not compatible with "
                        f"expected type {source_type}: {error_msg}"
                    )
                    if on_generic_type_mismatch == "raise":
                        raise ValueError(message)
                    elif on_generic_type_mismatch == "warn":
                        warnings.warn(message, UserWarning, stacklevel=2)
                return v

            if not isinstance(v, dict):
                return base_origin.model_validate(v, context=info.context)

            namespace = v.get(NAMESPACE_KEY)
            name = v.get(NAME_KEY)
            if namespace is None or name is None:
                raise ValueError(
                    f"Missing discriminator keys: {NAMESPACE_KEY}, {NAME_KEY}"
                )

            subcls = base_origin.resolve(str(namespace), str(name))

            payload = dict(v)
            payload.pop(NAMESPACE_KEY, None)
            payload.pop(NAME_KEY, None)

            return subcls.model_validate(payload, context=info.context)

        return core_schema.with_info_plain_validator_function(
            dispatch,
            json_schema_input_schema=core_schema.any_schema(),
            serialization=core_schema.plain_serializer_function_ser_schema(
                lambda v: v,
                return_schema=core_schema.any_schema(),
            ),
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
