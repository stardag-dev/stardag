from typing import Annotated, Any

import pytest
from pydantic import TypeAdapter

from stardag._hashable_set import HashableSet, HashSafeSetSerializer
from stardag._task import BaseTask
from stardag.base_model import CONTEXT_MODE_KEY, StardagField


class CustomHashTask(BaseTask):
    a: Annotated[int, StardagField(hash_exclude=True)]

    def complete(self) -> bool:
        return True

    def run(self) -> None:
        pass


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
        # (
        #     "set of tasks (with hash_exclude)",
        #     HashableSet[CustomHashTask],
        #     [CustomHashTask(a=2), CustomHashTask(a=1)],
        #     [
        #         {"id": str(task.id)}
        #         for task in sorted([CustomHashTask(a=2), CustomHashTask(a=1)])
        #     ],
        # ),
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
