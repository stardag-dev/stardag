import os
import subprocess
import sys
from enum import Enum
from pathlib import Path
from typing import Annotated, Any, Literal

import pytest
from pydantic import Discriminator, TypeAdapter

from stardag._core.hashable_set import HashableSet, HashSafeSetSerializer
from stardag._core.base_task import BaseTask, auto_namespace
from stardag._core.task_id import sorted_if_total
from stardag.base_model import CONTEXT_MODE_KEY, StardagBaseModel, StardagField

auto_namespace(__name__)  # Avoid collisions in task registry


@pytest.mark.parametrize(
    "description, annotation, values, expected_values",
    [
        ("unordered int set", HashableSet[int], [3, 1, 2], [1, 2, 3]),
        ("unordered str set", HashableSet[str], ["c", "a", "b"], ["a", "b", "c"]),
        (
            "unordered mixed set",
            Annotated[
                frozenset[int | str],
                HashSafeSetSerializer(sort_key=lambda x: (str(type(x)), x)),
            ],
            [3, "a", 2, "b", 1],
            [1, 2, 3, "a", "b"],
        ),
    ],
)
def test_hashable_set_serialization(
    description: str,
    annotation: Any,
    values: list,
    expected_values: list,
):
    dumped = TypeAdapter(annotation).dump_python(
        frozenset(values), mode="json", context={CONTEXT_MODE_KEY: "hash"}
    )
    assert dumped == expected_values, f"Failed: {description}"


class CustomHashTask(BaseTask):
    a: int
    b: Annotated[str, StardagField(significant=False)] = "constant"

    def complete(self) -> bool:
        return True

    def run(self) -> None:
        pass


def test_hashable_set_serialization_complex_type():
    annotation = HashableSet[CustomHashTask]
    values = [
        CustomHashTask(a=3),
        CustomHashTask(a=1),
        CustomHashTask(a=2),
    ]
    values_sorted = sorted(values)
    assert values != values_sorted, (
        "Precondition failed: values should be in non-sorted order"
    )

    expected_dumped = [{"id": str(task.id)} for task in values_sorted]

    dumped = TypeAdapter(annotation).dump_python(
        frozenset(values), mode="json", context={CONTEXT_MODE_KEY: "hash"}
    )
    assert dumped == expected_dumped, (
        "Unexpected serialization for HashableSet of complex type"
    )


# --- members that the default sort key does not totally order (STA-180) ------
#
# The default sort key is the member itself. For nested sets ``<`` is
# inclusion, so disjoint members do not compare and the sort used to keep the
# set's iteration order (which, for strings, differs per process); models
# define no ``<`` at all, so serialization raised. Both now order by the
# members' canonical JSON. Members that *are* totally ordered keep their order
# by value, so existing ids do not move: the golden ids below were computed
# before the fallback existed.


class _A(StardagBaseModel):
    type: Literal["A"]
    w: float


class _B(StardagBaseModel):
    type: Literal["B"]
    w: float


class NestedStrSets(BaseTask):
    __namespace__ = "sta180"
    groups: HashableSet[HashableSet[str]]

    def complete(self) -> bool:
        return False

    def run(self) -> None:
        pass


class ModelSet(BaseTask):
    __namespace__ = "sta180"
    aux: HashableSet[Annotated[_A | _B, Discriminator("type")]]

    def complete(self) -> bool:
        return False

    def run(self) -> None:
        pass


GROUPS = [["x", "y"], ["a"], ["q", "b"], ["c", "d", "e"]]
MODELS = [_A(type="A", w=1.0), _B(type="B", w=2.0), _A(type="A", w=0.5)]


def test_a_set_of_disjoint_sets_orders_by_canonical_json():
    task = NestedStrSets(groups=frozenset(frozenset(g) for g in GROUPS))
    assert task.instance_body()["groups"] == [
        ["a"],
        ["b", "q"],
        ["c", "d", "e"],
        ["x", "y"],
    ]
    reordered = NestedStrSets(groups=frozenset(frozenset(g) for g in GROUPS[::-1]))
    assert reordered.id == task.id


def test_a_set_of_models_serializes_in_canonical_json_order():
    task = ModelSet(aux=frozenset(MODELS))
    assert task.instance_body()["aux"] == [
        {"type": "A", "w": 0.5},
        {"type": "A", "w": 1.0},
        {"type": "B", "w": 2.0},
    ]
    assert ModelSet(aux=frozenset(MODELS[::-1])).id == task.id


_SEED_SCRIPT = """
from tests.test__core.test_hashable_set import (
    GROUPS, MODELS, ModelSet, NestedStrSets,
)
print(NestedStrSets(groups=frozenset(frozenset(g) for g in GROUPS)).id)
print(ModelSet(aux=frozenset(MODELS)).id)
"""


def test_ids_are_identical_across_hash_seeds():
    """String hashing is seeded per process, so the bug only shows across
    processes: run the same construction under several seeds."""
    root = Path(__file__).parents[2]
    outputs = {
        subprocess.run(
            [sys.executable, "-c", _SEED_SCRIPT],
            cwd=root,
            env={**os.environ, "PYTHONHASHSEED": str(seed)},
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        for seed in range(1, 6)
    }
    assert len(outputs) == 1, outputs


class Color(str, Enum):
    RED = "red"
    GREEN = "green"
    BLUE = "blue"


class Leaf(BaseTask):
    __namespace__ = "sta180"
    a: int

    def complete(self) -> bool:
        return False

    def run(self) -> None:
        pass


def _task_with(name: str, annotation: Any) -> type[BaseTask]:
    return type(
        name,
        (BaseTask,),
        {
            "__namespace__": "sta180",
            "__annotations__": {"v": annotation},
            "__module__": __name__,
            "complete": lambda self: False,
            "run": lambda self: None,
        },
    )


@pytest.mark.parametrize(
    "name, annotation, value, expected_id",
    [
        (
            "Ints",
            HashableSet[int],
            frozenset({10, 9, 100, -3}),
            "95808c1c-51f4-5e5b-a45e-31b224c5ffc7",
        ),
        (
            "Strs",
            HashableSet[str],
            frozenset({"b", "a", "B", "10", "9"}),
            "45f731f6-e512-59e5-90c0-73f1848797ea",
        ),
        (
            "Floats",
            HashableSet[float],
            frozenset({1.5, -2.0, 10.0, 9.25}),
            "be95e740-7822-507d-9e79-57c1f50994d3",
        ),
        (
            "Tuples",
            HashableSet[tuple[str, int]],
            frozenset({("b", 1), ("a", 10), ("a", 9)}),
            "51186ae6-443b-5ba8-9064-a2cb4b75c73e",
        ),
        (
            "Colors",
            HashableSet[Color],
            frozenset(Color),
            "eec5fdb5-bd1d-5540-9b85-976966f920e3",
        ),
        (
            "Leaves",
            HashableSet[Leaf],
            frozenset(Leaf(a=i) for i in range(4)),
            "575fbc58-7fd4-5a7f-8e49-71c5e7653578",
        ),
        (
            "ChainSets",
            HashableSet[HashableSet[int]],
            frozenset({frozenset({1}), frozenset({1, 2}), frozenset({1, 2, 30})}),
            "b4d3e7ba-fea2-55f0-b08a-ba6568be11b0",
        ),
        (
            "PlainNested",
            frozenset[frozenset[str]],
            frozenset(frozenset(g) for g in GROUPS),
            "198f67ab-41da-5681-83ff-646ac763b1e4",
        ),
    ],
)
def test_totally_ordered_members_keep_their_ids(
    name: str, annotation: Any, value: Any, expected_id: str
):
    task_class: Any = _task_with(name, annotation)
    assert str(task_class(v=value).id) == expected_id


@pytest.mark.parametrize(
    "items, expected",
    [
        ([3, 1, 2], [1, 2, 3]),
        ([frozenset({1}), frozenset({1, 2})], [frozenset({1}), frozenset({1, 2})]),
        ([frozenset({1}), frozenset({2})], None),  # disjoint: incomparable
        ([1, "a"], None),  # raises
    ],
)
def test_sorted_if_total(items: list, expected: list | None):
    assert sorted_if_total(items, key=lambda x: x) == expected
    assert sorted_if_total(items[::-1], key=lambda x: x) == expected
