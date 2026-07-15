from abc import ABC, abstractmethod
from typing import Annotated, ClassVar, Generic, Optional, TypeVar

import pytest
from pydantic import BaseModel, TypeAdapter

from stardag.exceptions import StardagError
from stardag.polymorphic import (
    NAME_KEY,
    NAMESPACE_KEY,
    NakedPolymorphicFieldError,
    Polymorphic,
    PolymorphicRoot,
    StrictPolymorphicTypeError,
    SubClass,
    TypeId,
)


def test_smoke():
    class Animal(PolymorphicRoot):
        pass

    class Dog(Animal):
        bark_volume: int

    class Cat(Animal):
        mood: str

    T = TypeVar("T")

    class BirdBase(Animal, Generic[T]):
        @abstractmethod
        def extra_info(self) -> T:
            return None  # type: ignore

    class Parrot(BirdBase[str]):
        vocabulary_size: int = 10

        def extra_info(self) -> str:
            return f"Parrot with vocabulary size {self.vocabulary_size}"

    class Sparrow(BirdBase[int]):
        wing_span_cm: int = 25

        def extra_info(self) -> int:
            return self.wing_span_cm

    # create a different family
    class Tool(PolymorphicRoot):
        pass

    class Hammer(Tool):
        weight_kg: float

    class Screwdriver(Tool):
        length_cm: int

    class Container(BaseModel):
        strict_item: Dog
        poly_item: SubClass[Animal]
        poly_items: list[SubClass[Animal]]
        bird: SubClass[BirdBase]
        tool: SubClass[Tool]

    dog_data = {
        NAMESPACE_KEY: "",
        NAME_KEY: "Dog",
        "bark_volume": 10,
    }

    cat_data = {
        NAMESPACE_KEY: "",
        NAME_KEY: "Cat",
        "mood": "happy",
    }

    parrot_data = {
        NAMESPACE_KEY: "",
        NAME_KEY: "Parrot",
        "vocabulary_size": 50,
    }

    tool_data = {
        NAMESPACE_KEY: "",
        NAME_KEY: "Hammer",
        "weight_kg": 2.5,
    }

    container_data = {
        "strict_item": dog_data,
        "poly_item": cat_data,
        "poly_items": [dog_data, cat_data],
        "bird": parrot_data,
        "tool": tool_data,
    }

    container = Container.model_validate(container_data)

    assert isinstance(container.strict_item, Dog)
    assert container.strict_item.bark_volume == 10

    assert isinstance(container.poly_item, Cat)
    assert container.poly_item.mood == "happy"

    assert isinstance(container.poly_items[0], Dog)
    assert container.poly_items[0].bark_volume == 10

    assert isinstance(container.poly_items[1], Cat)
    assert container.poly_items[1].mood == "happy"

    assert isinstance(container.bird, Parrot)
    assert container.bird.vocabulary_size == 50

    assert isinstance(container.tool, Hammer)
    assert container.tool.weight_kg == 2.5

    animal_registry = Animal._registry()
    tool_registry = Tool._registry()
    assert tool_registry is not animal_registry

    assert Dog._registry() is animal_registry
    assert Cat._registry() is animal_registry
    assert BirdBase._registry() is animal_registry
    assert Parrot._registry() is animal_registry
    assert Sparrow._registry() is animal_registry
    expected_animal_type_id_to_class = {
        TypeId(namespace="", name="Dog"): Dog,
        TypeId(namespace="", name="Cat"): Cat,
        TypeId(namespace="", name="BirdBase"): BirdBase,
        TypeId(namespace="", name="Parrot"): Parrot,
        TypeId(namespace="", name="Sparrow"): Sparrow,
    }
    assert animal_registry._type_id_to_class == expected_animal_type_id_to_class

    assert Hammer._registry() is tool_registry
    assert Screwdriver._registry() is tool_registry
    expected_tool_type_id_to_class = {
        TypeId(namespace="", name="Hammer"): Hammer,
        TypeId(namespace="", name="Screwdriver"): Screwdriver,
    }
    assert tool_registry._type_id_to_class == expected_tool_type_id_to_class

    # serialize back to dict
    serialized = container.model_dump()
    assert serialized == container_data


def test_root_is_generic():
    T = TypeVar("T")

    class Wrapper(PolymorphicRoot, Generic[T]):
        value: T

    class IntWrapper(Wrapper[int]):
        pass

    class StrWrapper(Wrapper[str]):
        pass

    data = {
        NAMESPACE_KEY: "",
        NAME_KEY: "IntWrapper",
        "value": 42,
    }

    wrapped = TypeAdapter(SubClass[Wrapper]).validate_python(data)
    assert isinstance(wrapped, IntWrapper)
    assert wrapped.value == 42

    assert Wrapper._registry()._type_id_to_class == {
        TypeId(namespace="", name="IntWrapper"): IntWrapper,
        TypeId(namespace="", name="StrWrapper"): StrWrapper,
    }


def test_namespace_handling():
    class Root(PolymorphicRoot):
        pass

    class ChildA(Root):
        pass

    class ChildB(Root, namespace_override="custom_namespace_b"):
        pass

    class ChildC(Root):
        __namespace__ = "custom_namespace_c"

    registry = Root._registry()
    expected = {
        TypeId(namespace="", name="ChildA"): ChildA,
        TypeId(namespace="custom_namespace_b", name="ChildB"): ChildB,
        TypeId(namespace="custom_namespace_c", name="ChildC"): ChildC,
    }
    assert registry._type_id_to_class == expected

    assert ChildA.get_namespace() == ""
    assert ChildB.get_namespace() == "custom_namespace_b"
    assert ChildC.get_namespace() == "custom_namespace_c"

    # Class arg namespace_override does not propagate to subclasses
    class ChildB_A(ChildB):
        pass

    assert ChildB_A.get_namespace() == "", (
        "Subclass should not inherit namespace_override from class arg"
    )

    # Class var __namespace__ propagates to subclasses
    class ChildC_A(ChildC):
        pass

    assert ChildC_A.get_namespace() == "custom_namespace_c", (
        "Subclass should inherit __namespace__"
    )


def test_name_handling():
    class Root(PolymorphicRoot):
        pass

    class ChildA(Root):
        pass

    class ChildB(Root, name_override="CustomNameB"):
        pass

    registry = Root._registry()
    expected = {
        TypeId(namespace="", name="ChildA"): ChildA,
        TypeId(namespace="", name="CustomNameB"): ChildB,
    }
    assert registry._type_id_to_class == expected

    assert ChildA.get_name() == "ChildA"
    assert ChildB.get_name() == "CustomNameB"


class TestNakedPolymorphicFieldRejection:
    """A field annotated with a *bare* abstract PolymorphicRoot subclass is a
    silent data-loss trap: serialization drops subclass-specific parameters and
    deserialization crashes trying to instantiate the abstract base. Such
    annotations are rejected at class-construction time.
    """

    def test_bare_abstract_base_rejected(self):
        class Shape(PolymorphicRoot):
            @abstractmethod
            def area(self) -> float: ...

        with pytest.raises(NakedPolymorphicFieldError, match="SubClass"):

            class Canvas(PolymorphicRoot):
                shape: Shape  # naked abstract

    def test_bare_abstract_base_in_list_rejected(self):
        class Shape(PolymorphicRoot):
            @abstractmethod
            def area(self) -> float: ...

        with pytest.raises(NakedPolymorphicFieldError):

            class Canvas(PolymorphicRoot):
                shapes: list[Shape]

    def test_bare_abstract_base_in_dict_value_rejected(self):
        class Shape(PolymorphicRoot):
            @abstractmethod
            def area(self) -> float: ...

        with pytest.raises(NakedPolymorphicFieldError):

            class Canvas(PolymorphicRoot):
                shapes: dict[str, Shape]

    def test_bare_abstract_base_in_optional_rejected(self):
        class Shape(PolymorphicRoot):
            @abstractmethod
            def area(self) -> float: ...

        with pytest.raises(NakedPolymorphicFieldError):

            class Canvas(PolymorphicRoot):
                shape: Optional[Shape] = None

    def test_stardag_abstract_marker_rejected(self):
        """Abstract bases marked only via ``__stardag_abstract__`` are caught."""

        class Node(PolymorphicRoot):
            __stardag_abstract__: ClassVar[bool] = True

        with pytest.raises(NakedPolymorphicFieldError):

            class Graph(PolymorphicRoot):
                node: Node

    def test_abc_base_marker_rejected(self):
        """Abstract bases marked via ``abc.ABC`` in bases are caught."""

        class Node(PolymorphicRoot, ABC):
            pass

        with pytest.raises(NakedPolymorphicFieldError):

            class Graph(PolymorphicRoot):
                node: Node

    def test_error_is_stardag_error(self):
        assert issubclass(NakedPolymorphicFieldError, StardagError)

    # --- accepted forms (no false positives) -------------------------------

    def test_subclass_annotation_accepted(self):
        class Shape(PolymorphicRoot):
            @abstractmethod
            def area(self) -> float: ...

        class Canvas(PolymorphicRoot):
            shape: SubClass[Shape]
            shapes: list[SubClass[Shape]]
            annotated: Annotated[Shape, Polymorphic()]

    def test_concrete_strict_annotation_accepted(self):
        """A bare *concrete* subclass is an intentional 'strict' field (only ever
        holds exactly that type) and must keep working."""

        class Animal(PolymorphicRoot):
            pass

        class Dog(Animal):
            bark_volume: int = 3

        class Kennel(PolymorphicRoot):
            resident: Dog  # concrete -> allowed

    def test_plain_scalar_fields_accepted(self):
        class Plain(PolymorphicRoot):
            a: int
            b: str = "x"
            c: list[int] = []


# Module-level so they are proper static types usable in annotations below
# (a concrete family: ``_Animal`` is the concrete root, ``_Dog`` a subclass).
class _Animal(PolymorphicRoot):
    legs: int = 4


class _Dog(_Animal):
    bark_volume: int = 3


# A registered (non-root) concrete class + subclass, used to exercise the
# deserialize path: a *registered* strict type has a ``__type_id__`` to compare
# an input dict's discriminator against.
class _Vehicle(PolymorphicRoot):
    pass


class _Car(_Vehicle):
    wheels: int = 4


class _SportsCar(_Car):
    spoiler: bool = True


def _serialized(cls: type, **fields) -> dict:
    """Build a discriminator-carrying payload (as ``model_dump`` would) for cls."""
    tid = cls.__type_id__  # type: ignore[attr-defined]
    return {NAMESPACE_KEY: tid.namespace, NAME_KEY: tid.name, "version": "", **fields}


class TestStrictConcretePolymorphicField:
    """A bare *concrete* PolymorphicRoot field is a "strict" field: it means
    exactly that type. Passing a subclass instance would silently drop the
    subclass's extra params on serialization (and collide identities), so it is
    rejected at validation time — use SubClass[...] to accept subclasses.
    """

    def test_exact_type_accepted(self):
        class Zoo(PolymorphicRoot):
            star: _Animal  # bare concrete = strict

        assert Zoo.__stardag_strict_polymorphic_fields__ == ("star",)
        zoo = Zoo(star=_Animal(legs=4))
        assert type(zoo.star) is _Animal

    def test_subclass_instance_rejected(self):
        class Zoo(PolymorphicRoot):
            star: _Animal

        with pytest.raises(StrictPolymorphicTypeError, match="SubClass"):
            Zoo(star=_Dog(legs=4, bark_volume=9))

    def test_subclass_in_list_rejected(self):
        class Zoo(PolymorphicRoot):
            animals: list[_Animal]

        Zoo(animals=[_Animal(), _Animal()])  # exact types OK
        with pytest.raises(StrictPolymorphicTypeError):
            Zoo(animals=[_Animal(), _Dog()])

    def test_subclass_in_tuple_rejected(self):
        class Zoo(PolymorphicRoot):
            animals: tuple[_Animal, ...] = ()

        Zoo(animals=(_Animal(), _Animal()))
        with pytest.raises(StrictPolymorphicTypeError):
            Zoo(animals=(_Animal(), _Dog()))

    def test_subclass_in_dict_value_rejected(self):
        class Zoo(PolymorphicRoot):
            animals: dict[str, _Animal]

        Zoo(animals={"a": _Animal()})
        with pytest.raises(StrictPolymorphicTypeError):
            Zoo(animals={"a": _Dog()})

    def test_optional_strict_none_and_exact_ok_subclass_rejected(self):
        class Zoo(PolymorphicRoot):
            star: Optional[_Animal] = None

        Zoo(star=None)
        Zoo(star=_Animal())
        with pytest.raises(StrictPolymorphicTypeError):
            Zoo(star=_Dog())

    def test_subclass_annotation_accepts_subclasses(self):
        # Container is a *registered* subclass (a family root has no __type_id__
        # and can't be serialized directly), mirroring real usage.
        class ZooBase(PolymorphicRoot):
            pass

        class Zoo(ZooBase):
            star: SubClass[_Animal]

        # SubClass[...] is not a strict field, so subclasses round-trip fully.
        assert Zoo.__stardag_strict_polymorphic_fields__ == ()
        zoo = Zoo(star=_Dog(bark_volume=7))
        assert type(zoo.star) is _Dog
        reloaded = Zoo.model_validate(zoo.model_dump())
        assert type(reloaded.star) is _Dog
        assert reloaded.star.bark_volume == 7

    def test_error_is_stardag_error(self):
        assert issubclass(StrictPolymorphicTypeError, StardagError)


class TestStrictConcretePolymorphicDeserialize:
    """Deserialize-path counterpart to strict-field enforcement: serialized data
    (a discriminator-carrying dict) for a *subclass* at a strict concrete field
    is rejected, instead of being silently coerced into the base type. Plain
    dicts without a discriminator are still validated as the exact strict type.
    """

    def test_exact_type_discriminator_dict_accepted(self):
        class Garage(PolymorphicRoot):
            car: _Car

        garage = Garage.model_validate({"car": _serialized(_Car, wheels=4)})
        assert type(garage.car) is _Car

    def test_plain_dict_without_discriminator_accepted(self):
        class Garage(PolymorphicRoot):
            car: _Car

        garage = Garage.model_validate({"car": {"wheels": 6}})
        assert type(garage.car) is _Car
        assert garage.car.wheels == 6

    def test_subclass_discriminator_dict_rejected(self):
        class Garage(PolymorphicRoot):
            car: _Car

        with pytest.raises(StrictPolymorphicTypeError, match="SubClass"):
            Garage.model_validate(
                {"car": _serialized(_SportsCar, wheels=4, spoiler=True)}
            )

    def test_subclass_discriminator_in_list_rejected(self):
        class Garage(PolymorphicRoot):
            cars: list[_Car]

        Garage.model_validate({"cars": [_serialized(_Car, wheels=4)]})  # exact OK
        with pytest.raises(StrictPolymorphicTypeError):
            Garage.model_validate(
                {"cars": [_serialized(_Car), _serialized(_SportsCar, spoiler=True)]}
            )

    def test_subclass_discriminator_in_optional_rejected(self):
        class Garage(PolymorphicRoot):
            car: Optional[_Car] = None

        Garage.model_validate({"car": None})
        Garage.model_validate({"car": _serialized(_Car)})
        with pytest.raises(StrictPolymorphicTypeError):
            Garage.model_validate({"car": _serialized(_SportsCar)})

    def test_family_root_strict_type_rejects_subclass_discriminator(self):
        """A concrete *family root* (direct PolymorphicRoot child) is a valid
        strict type but has no ``__type_id__`` — a subclass discriminator payload
        must still be rejected, not silently coerced to the root."""

        class Cage(PolymorphicRoot):
            occupant: _Animal  # _Animal is a family root (unregistered)

        # Plain dict without a discriminator is coerced to the exact root type.
        cage = Cage.model_validate({"occupant": {"legs": 4}})
        assert type(cage.occupant) is _Animal

        # A discriminator payload for a subclass is rejected.
        with pytest.raises(StrictPolymorphicTypeError, match="SubClass"):
            Cage.model_validate({"occupant": _serialized(_Dog, legs=4, bark_volume=2)})
