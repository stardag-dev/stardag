"""Stardag tasks *are* Pydantic models"""
from typing import Annotated
from pydantic import Field

import stardag as sd

class Range(sd.Task[list[int]]):
    # All Pydantic primitives such validation out of the box
    limit: Annotated[int, Field(gt=0)]

    def run(self):
        self._save(list(range(self.limit)))

class Sum(sd.Task[int]):
    values: sd.TaskLoads[list[int]]

    def requires(self):
        return self.values

    def run(self):
        self._save(sum(self.values.load()))

# Polymorphic composability - pass any task that produces `list[int]` to `values`
root_task = Sum(values=Range(limit=4))

# Tasks are Pydantic models with all the familiar convenience methods
assert root_task.model_dump() =={
  "__namespace": "",
  "__name": "Sum",
  "version": "",
  "values": {
    "__namespace": "",
    "__name": "Range",
    "version": "",
    "limit": 4
  }
}
