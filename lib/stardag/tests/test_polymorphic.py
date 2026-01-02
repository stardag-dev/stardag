from abc import abstractmethod
from typing import Generic, TypeVar

from pydantic import BaseModel

from stardag.polymorphic import (
    NAMESPACE_KEY,
    TYPE_KEY,
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
        TYPE_KEY: "Dog",
        "bark_volume": 10,
    }

    cat_data = {
        NAMESPACE_KEY: "",
        TYPE_KEY: "Cat",
        "mood": "happy",
    }

    parrot_data = {
        NAMESPACE_KEY: "",
        TYPE_KEY: "Parrot",
        "vocabulary_size": 50,
    }

    tool_data = {
        NAMESPACE_KEY: "",
        TYPE_KEY: "Hammer",
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
