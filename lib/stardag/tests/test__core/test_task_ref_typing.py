# pyright: reportUnnecessaryTypeIgnoreComment=true
"""Static typing of ``Task.ref``, checked by pyright (pre-commit runs it
over ``tests``). Each ``assert_type`` must hold, and each negative case
must be an error: its ``pyright: ignore`` would otherwise be reported as
unnecessary (``reportUnnecessaryTypeIgnoreComment``, enabled above).
"""

from typing import assert_type

import stardag as sd
from stardag._core.task_ref import TaskRef

ID = "6f1c2c62-1f5c-5a0e-9b8e-3a4f0e1d2c3b"


class Dataset(sd.Task[list[int]]):
    size: int

    def run(self) -> None:
        self._save(list(range(self.size)))


class TrainedModel(sd.Task[str]):
    data: sd.TaskLoads[list[int]]

    def run(self) -> None:
        self._save(str(sum(self.data.load())))


class Evaluate(sd.Task[float]):
    model: sd.TaskLoads[str]

    def run(self) -> None:
        self._save(float(len(self.model.load())))


def check_types() -> None:
    model_ref = TrainedModel.ref(ID, instance_id=ID)
    assert_type(model_ref, TaskRef[TrainedModel, str])
    assert_type(model_ref.hydrate(), TrainedModel)
    assert_type(model_ref.load(), str)

    data_ref = Dataset.ref(ID)
    assert_type(data_ref.hydrate().size, int)

    # Accepted where the loaded type matches, nested at any depth.
    Evaluate(model=model_ref)
    Evaluate(model=TrainedModel(data=data_ref))

    # Rejected where it does not.
    Evaluate(model=data_ref)  # pyright: ignore[reportArgumentType]
    TrainedModel(data=model_ref)  # pyright: ignore[reportArgumentType]


def test_typing_module_imports() -> None:
    """The checks above are static; this only keeps the module collected."""
    assert callable(check_types)
