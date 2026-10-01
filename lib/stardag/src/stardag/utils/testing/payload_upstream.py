"""A task class in a module of its own, for the Modal payload tests: the
task in ``payload_root`` holds one inside a dataclass, and that module does
not import this one, so a receiver learns of it from the payload or not at
all."""

import stardag as sd


class PkgUpstream(sd.Task[int]):
    x: int = 0

    def run(self) -> None:
        self._save(self.x)
