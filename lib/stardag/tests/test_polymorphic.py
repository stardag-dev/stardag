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
                shape: Shape  # type: ignore[valid-type]  # naked abstract

    def test_bare_abstract_base_in_list_rejected(self):
        class Shape(PolymorphicRoot):
            @abstractmethod
            def area(self) -> float: ...

        with pytest.raises(NakedPolymorphicFieldError):

            class Canvas(PolymorphicRoot):
                shapes: list[Shape]  # type: ignore[valid-type]

    def test_bare_abstract_base_in_dict_value_rejected(self):
        class Shape(PolymorphicRoot):
            @abstractmethod
            def area(self) -> float: ...

        with pytest.raises(NakedPolymorphicFieldError):

            class Canvas(PolymorphicRoot):
                shapes: dict[str, Shape]  # type: ignore[valid-type]

    def test_bare_abstract_base_in_optional_rejected(self):
        class Shape(PolymorphicRoot):
            @abstractmethod
            def area(self) -> float: ...

        with pytest.raises(NakedPolymorphicFieldError):

            class Canvas(PolymorphicRoot):
                shape: Optional[Shape] = None  # type: ignore[valid-type]

    def test_stardag_abstract_marker_rejected(self):
        """Abstract bases marked only via ``__stardag_abstract__`` are caught."""

        class Node(PolymorphicRoot):
            __stardag_abstract__: ClassVar[bool] = True

        with pytest.raises(NakedPolymorphicFieldError):

            class Graph(PolymorphicRoot):
                node: Node  # type: ignore[valid-type]

    def test_abc_base_marker_rejected(self):
        """Abstract bases marked via ``abc.ABC`` in bases are caught."""

        class Node(PolymorphicRoot, ABC):
            pass

        with pytest.raises(NakedPolymorphicFieldError):

            class Graph(PolymorphicRoot):
                node: Node  # type: ignore[valid-type]

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
