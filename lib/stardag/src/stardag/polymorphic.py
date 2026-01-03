import logging
import typing
from dataclasses import dataclass
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    ClassVar,
    Generic,
    Tuple,
    Type,
    TypeVar,
    Union,
)

from pydantic import (
    BaseModel,
    GetCoreSchemaHandler,
    SerializationInfo,
)
from pydantic_core import core_schema

from stardag.base_model import StardagBaseModel

logger = logging.getLogger(__name__)

TYPE_NAMESPACE_KEY = "__type_namespace__"
TYPE_NAME_KEY = "__type_name__"


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

            raise ValueError(
                "A class is already registered for the "
                f'type_id "{type_id}".\n'
                f"Existing: {existing.__module__}.{existing.__name__}\n"
                f"New: {cls.__module__}.{cls.__name__}"
            )
        self._type_id_to_class[type_id] = cls

        return type_id

    def _resolve_type_id(
        self,
        cls: Type[BaseModel],
        name_override: str | None,
        namespace_override: str | None,
    ) -> TypeId:
        return TypeId(
            name=name_override or cls.__name__,
            namespace=self._resolve_namespace(
                cls,
                namespace_override=namespace_override,
            ),
        )

    def _resolve_namespace(
        self,
        cls: Type[BaseModel],
        namespace_override: str | None,
    ) -> str:
        if namespace_override is not None:
            return namespace_override

        cls_namespace = getattr(cls, TYPE_NAMESPACE_KEY, None)
        if cls_namespace is not None:
            # Already set explicitly on task class
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


class _Generic(Generic[_TBaseModel]):
    pass


class PolymorphicRoot(StardagBaseModel):
    """
    Base class for a polymorphic family.
    Each subclass family has its own registry stored on the base class.
    """

    __type_id__: ClassVar[TypeId]

    # IMPORTANT: per-family registry lives on the base class
    __registry__: ClassVar[_TypeRegistry] = _TypeRegistry()

    if TYPE_CHECKING:
        # Optionally set on subclasses to override default type id resolution
        __type_namespace__: ClassVar[str]

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
        type_name: str | None = None,
        type_namespace: str | None = None,
        **kwargs: Any,
    ) -> None:
        # Need to avoid forwarding the family and namespace kwarg to the BaseModel
        super().__init_subclass__(**kwargs)

    @classmethod
    def __pydantic_init_subclass__(
        cls,
        type_name: str | None = None,
        type_namespace: str | None = None,
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
                    # TODO pass overrides from class args
                    name_override=type_name,
                    namespace_override=type_namespace,
                )

    def __class_getitem__(
        cls: Type[BaseModel],
        params: Union[Type[Any], Tuple[Type[Any], ...]],
    ) -> Type[Any]:
        """Hack to be able to access the generic type of the class from subclasses. See:
        https://github.com/pydantic/pydantic/discussions/4904#discussioncomment-4592052
        """
        create_model = super().__class_getitem__(params)  # type: ignore

        create_model.__orig_class__ = _Generic[params]  # type: ignore
        return create_model

    def _serialize_extra(
        self,
        data: Any,
        info: SerializationInfo,
    ):
        """Always add discriminator keys. This runs for all subclasses too."""
        if isinstance(data, dict):
            tid = self.__class__.__type_id__
            data = dict(data)
            data[TYPE_NAMESPACE_KEY] = tid.namespace
            data[TYPE_NAME_KEY] = tid.name
        return data

    @classmethod
    def get_type_name(cls) -> str:
        """Get the type name for this class."""
        return cls.__type_id__.name

    @classmethod
    def get_type_namespace(cls) -> str:
        """Get the type namespace for this class."""
        return cls.__type_id__.namespace


class Polymorphic:
    """TODO"""

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

        base: type[PolymorphicRoot] = source_type

        def dispatch(v: Any, info):
            if isinstance(v, base):
                return v

            if not isinstance(v, dict):
                return base.model_validate(v, context=info.context)

            namespace = v.get(TYPE_NAMESPACE_KEY)
            name = v.get(TYPE_NAME_KEY)
            if namespace is None or name is None:
                raise ValueError(
                    f"Missing discriminator keys: {TYPE_NAMESPACE_KEY}, {TYPE_NAME_KEY}"
                )

            subcls = base.resolve(str(namespace), str(name))

            payload = dict(v)
            payload.pop(TYPE_NAMESPACE_KEY, None)
            payload.pop(TYPE_NAME_KEY, None)

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
