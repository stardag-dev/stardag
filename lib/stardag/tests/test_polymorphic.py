from abc import abstractmethod
from typing import Generic, TypeVar

from pydantic import BaseModel

from stardag.polymorphic import PolymorphicRoot, SubClass


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

    class Container(BaseModel):
        strict_item: Dog
        poly_item: SubClass[Animal]
        poly_items: list[SubClass[Animal]]
        bird: SubClass[BirdBase]

    dog_data = {
        "__namespace__": "",
        "__name__": "Dog",
        "bark_volume": 10,
    }

    cat_data = {
        "__namespace__": "",
        "__name__": "Cat",
        "mood": "happy",
    }

    parrot_data = {
        "__namespace__": "",
        "__name__": "Parrot",
        "vocabulary_size": 50,
    }

    container_data = {
        "strict_item": dog_data,
        "poly_item": cat_data,
        "poly_items": [dog_data, cat_data],
        "bird": parrot_data,
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

    assert Dog._registry() is Animal._registry()
    assert Cat._registry() is Animal._registry()
    assert BirdBase._registry() is Animal._registry()
    assert Parrot._registry() is BirdBase._registry()
    assert Sparrow._registry() is BirdBase._registry()
