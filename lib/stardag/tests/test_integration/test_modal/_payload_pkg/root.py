"""A task holding another task inside a dataclass, which the payload's
object walk must enter to name the upstream's module (see test_payload)."""

from dataclasses import dataclass

import stardag as sd


@dataclass
class Holder:
    upstream: sd.TaskLoads[int]


class DataclassRoot(sd.Task[int]):
    holder: Holder

    def run(self) -> None:
        self._save(0)
