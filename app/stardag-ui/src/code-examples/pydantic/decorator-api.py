"""Stardag tasks *are* Pydantic models and serve as a
declarative specification of the assets they produce."""
from typing import Annotated
from pydantic import Field

import stardag as sd

@sd.task
def get_range(
    # Get all Pydantic primitives such as validation out of the box
    limit: Annotated[int, Field(gt=0)]
) -> list[int]:
    return list(range(limit))

@sd.task
def get_sum(values: sd.Depends[list[int]]) -> int:
    return sum(values)

# Polymorphic composability - pass any task that produces `list[int]` to `values`
root_task = get_sum(values=get_range(limit=4))

# Tasks are Pydantic models with all the familiar convenience methods
assert root_task.model_dump() == {
  "__namespace": "",
  "__name": "get_sum",
  "version": "",
  "values": {
    "__namespace": "",
    "__name": "get_range",
    "version": "",
    "limit": 4
  }
}
