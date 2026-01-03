from typing import Annotated, Any

import pytest
from pydantic import TypeAdapter

from stardag._hashable_set import HashableSet, HashSafeSetSerializer
from stardag._task import BaseTask
from stardag.base_model import CONTEXT_MODE_KEY, StardagField


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
    b: Annotated[str, StardagField(hash_exclude=True)] = "constant"

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
    assert (
        values != values_sorted
    ), "Precondition failed: values should be in non-sorted order"

    expected_dumped = [{"id": str(task.id)} for task in values_sorted]

    dumped = TypeAdapter(annotation).dump_python(
        frozenset(values), mode="json", context={CONTEXT_MODE_KEY: "hash"}
    )
    assert (
        dumped == expected_dumped
    ), "Unexpected serialization for HashableSet of complex type"
