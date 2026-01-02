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
    def _inject_compat_defaults(cls, data: Any, info):
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
    def _hash_drop_defaults(self, handler, info: SerializationInfo):
        """If mode="hash", drop fields with BackwardCompat default values."""
        data = handler(self)
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


# _REGISTRY: dict[TypeId, type["MyBase"]] = {}


# def resolve_polymorph(ns: str, name: str) -> type["MyBase"]:
#     tid = TypeId(ns, name)
#     try:
#         return _REGISTRY[tid]
#     except KeyError as e:
#         raise ValueError(f"Unknown registered MyBase type: {ns}:{name}") from e


# # -------------------------
# # MyBase: auto-register subclasses + always include discriminator keys
# # -------------------------
# class MyBase(StardagBaseModel):
#     """
#     - Auto-register every subclass in the external registry when declared
#     - Always serialize with discriminator keys (TYPE_NS_KEY / TYPE_NAME_KEY)
#     """

#     __type_id__: ClassVar[TypeId]

#     # You can override these per-class if you want:
#     __namespace__: ClassVar[str | None] = None
#     __type_name__: ClassVar[str | None] = None

#     def __init_subclass__(cls, **kwargs):
#         super().__init_subclass__(**kwargs)

#         # Avoid registering the base itself
#         if cls is MyBase:
#             return

#         ns = cls.__namespace__ or cls.__module__
#         name = cls.__type_name__ or cls.__name__

#         tid = TypeId(namespace=ns, name=name)
#         # Allow idempotent redefinition in reloads only if it's the same class object
#         if tid in _REGISTRY and _REGISTRY[tid] is not cls:
#             raise ValueError(
#                 f"Duplicate MyBase registration for {tid} -> {_REGISTRY[tid]} vs {cls}"
#             )

#         cls.__type_id__ = tid
#         _REGISTRY[tid] = cls

#     @model_serializer(mode="wrap")
#     def _tag_discriminator(self, handler, info: SerializationInfo):
#         """
#         Always add discriminator keys. This runs for all subclasses too.
#         """
#         data = handler(self)
#         if isinstance(data, dict):
#             tid = self.__class__.__type_id__
#             data = dict(data)
#             data[TYPE_NS_KEY] = tid.namespace
#             data[TYPE_NAME_KEY] = tid.name
#         return data


# # -------------------------
# # Polymorphic opt-in annotation + SubClass[T] sugar
# # -------------------------
# class Polymorphic:
#     """
#     Field-level opt-in:
#       - Validation: if tags exist -> lookup registered subclass and validate into it
#       - Serialization: "as any" (duck-typed) so runtime subclass defines shape
#         (discriminator keys are still added by MyBase._tag_discriminator)
#     """

#     def __init__(self, *, strict_tags: bool = False):
#         self.strict_tags = strict_tags

#     def __get_pydantic_core_schema__(
#         self, source_type: Any, handler: GetCoreSchemaHandler
#     ):
#         # Keep normal schema around (in case you want it for json schema etc)
#         _ = handler(source_type)  # ensure schema generated for source_type

#         def dispatch(v: Any, info):
#             # Already validated object
#             if isinstance(v, MyBase):
#                 return v

#             # Only dict inputs can carry discriminator tags
#             if not isinstance(v, dict):
#                 # strict fallback: validate as the declared (base) type
#                 return source_type.model_validate(v, context=info.context)

#             ns = v.get(TYPE_NS_KEY)
#             name = v.get(TYPE_NAME_KEY)

#             if ns is None or name is None:
#                 if self.strict_tags:
#                     raise ValueError(
#                         f"Missing discriminator keys: {TYPE_NS_KEY}, {TYPE_NAME_KEY}"
#                     )
#                 # strict fallback (no dispatch)
#                 return source_type.model_validate(v, context=info.context)

#             subcls = resolve_polymorph(str(ns), str(name))

#             payload = dict(v)
#             payload.pop(TYPE_NS_KEY, None)
#             payload.pop(TYPE_NAME_KEY, None)

#             # Important: propagate context (so mode="compat"/"hash" keep working)
#             return subcls.model_validate(payload, context=info.context)

#         return core_schema.with_info_plain_validator_function(
#             dispatch,
#             json_schema_input_schema=core_schema.any_schema(),
#             # "Serialize as any": runtime type drives serialization (duck typing)
#             serialization=core_schema.plain_serializer_function_ser_schema(
#                 lambda v: v,
#                 return_schema=core_schema.any_schema(),
#             ),
#         )


# class _SubClass:
#     """
#     Sugar:
#       - SubClass[T]                          -> Annotated[T, Polymorphic()]
#       - SubClass[T, {"strict_tags": True}]   -> Annotated[T, Polymorphic(strict_tags=True)]
#     """

#     def __class_getitem__(cls, item):
#         if isinstance(item, tuple):
#             base, *rest = item
#             kwargs: dict[str, Any] = {}
#             for r in rest:
#                 if isinstance(r, dict):
#                     kwargs.update(r)
#             return Annotated[base, Polymorphic(**kwargs)]
#         return Annotated[item, Polymorphic()]


# _T = TypeVar("_T", bound=MyBase)

# if typing.TYPE_CHECKING:
#     SubClass: typing.TypeAlias = typing.Annotated[_T, "polymorphic"]
# else:
#     SubClass = _SubClass


# # -------------------------
# # Example polymorphic types
# # -------------------------
# class Dog(MyBase):
#     # normal default is 1, compat default is 0, hash drops when == 0
#     loudness: Annotated[int, StardagField(default=0)] = 1
#     bark_db: int


# class Cat(MyBase):
#     # demonstrate a different namespace/name override
#     __namespace__ = "animals"
#     __type_name__ = "kitty"

#     lives: int
#     mood: Annotated[str, StardagField(default="neutral")] = "happy"


# T = TypeVar("T")


# class BirdBase(MyBase, Generic[T]):
#     @abstractmethod
#     def extra_info(self) -> T:
#         return None  # type: ignore


# class Parrot(BirdBase[str]):
#     vocabulary_size: int = 10

#     def extra_info(self) -> str:
#         return f"Parrot with vocabulary size {self.vocabulary_size}"


# class Sparrow(BirdBase[int]):
#     wing_span_cm: int = 25

#     def extra_info(self) -> int:
#         return self.wing_span_cm


# # -------------------------
# # Containers: strict vs opt-in polymorphic + duck typing
# # -------------------------
# class NotMyBase(BaseModel):
#     """
#     - strict_item: typed MyBase => strict validation as MyBase (no registry dispatch)
#                    and serialization uses *base schema* unless model_dump(serialize_as_any=True)
#                    BUT discriminator keys still included for clarity (MyBase serializer)
#     - poly_item: SubClass[MyBase] => registry dispatch + serialize-as-any
#     - items: list[...] works too
#     """

#     strict_item: Optional[MyBase] = None
#     poly_item: Optional[SubClass[MyBase]] = None
#     items: list[SubClass[MyBase]] = []
#     bird: Optional[SubClass[BirdBase[str]]] = None


# # -------------------------
# # Demo
# # -------------------------
# def main():
#     print("== Registry contents ==")
#     for k, v in sorted(_REGISTRY.items(), key=lambda kv: (kv[0].namespace, kv[0].name)):
#         print(f"  {k.namespace}:{k.name} -> {v.__name__}")

#     print("\n== Validate polymorphic payload (opt-in fields) ==")
#     payload = {
#         "poly_item": {
#             TYPE_NS_KEY: Dog.__type_id__.namespace,
#             TYPE_NAME_KEY: Dog.__type_id__.name,
#             "bark_db": 90,
#             # loudness is missing here
#         },
#         "items": [
#             {
#                 TYPE_NS_KEY: "animals",
#                 TYPE_NAME_KEY: "kitty",
#                 "lives": 9,
#                 # mood is missing here
#             }
#         ],
#         "bird": {
#             TYPE_NS_KEY: Parrot.__type_id__.namespace,
#             TYPE_NAME_KEY: Parrot.__type_id__.name,
#             "vocabulary_size": 42,
#         },
#     }

#     # compat-mode: missing BackwardCompat fields get the compat default (0 / "neutral")
#     m_compat = NotMyBase.model_validate(payload, context={CONTEXT_MODE_KEY: "compat"})
#     print("poly_item type:", type(m_compat.poly_item).__name__)
#     print("items[0] type:", type(m_compat.items[0]).__name__)
#     print(
#         "poly_item.loudness (compat default expected 0):",
#         m_compat.poly_item.loudness,  # type: ignore
#     )
#     print(
#         "items[0].mood (compat default expected 'neutral'):",
#         m_compat.items[0].mood,  # type: ignore
#     )
#     print("bird.extra_info():", m_compat.bird.extra_info() if m_compat.bird else None)

#     print("\n== Normal instantiation (no mode) uses declared defaults ==")
#     d = Dog(bark_db=80)
#     c = Cat(lives=7)
#     p = Parrot(vocabulary_size=15)
#     print("Dog.loudness default (expected 1):", d.loudness)
#     print("Cat.mood default (expected 'happy'):", c.mood)
#     print("Parrot.extra_info():", p.extra_info())

#     print("\n== Serialization (default mode) always includes discriminator keys ==")
#     out_default = NotMyBase(poly_item=d, items=[c], bird=p).model_dump()
#     print(out_default)

#     print(
#         "\n== Serialization with mode='hash' drops BackwardCompat-default-valued keys =="
#     )
#     # Make defaults equal to compat default so they get dropped in hash mode
#     d2 = Dog(bark_db=70, loudness=0)
#     c2 = Cat(lives=9, mood="neutral")
#     p2 = Parrot(vocabulary_size=42)

#     out_hash = NotMyBase(poly_item=d2, items=[c2], bird=p2).model_dump(
#         context={CONTEXT_MODE_KEY: "hash"}
#     )
#     print(out_hash)

#     wrong_generic = NotMyBase(bird=Sparrow(wing_span_cm=30))
#     print(wrong_generic)


# if __name__ == "__main__":
#     main()
