"""A task holding another task inside a dataclass, which the Modal payload's
walk must enter to name the upstream's module. The upstream lives in
``payload_upstream``, which this module deliberately does not import: a
receiver learns of it from the payload or not at all."""

from dataclasses import dataclass

import stardag as sd


@dataclass
class Holder:
    upstream: sd.TaskLoads[int]


class DataclassRoot(sd.Task[int]):
    holder: Holder

    def run(self) -> None:
        self._save(0)
