"""A task class in a module of its own, imported only by ``root``: the
receiver learns of it from the payload or not at all (see test_payload)."""

import stardag as sd


class PkgUpstream(sd.Task[int]):
    x: int = 0

    def run(self) -> None:
        self._save(self.x)
