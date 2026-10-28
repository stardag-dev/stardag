# pyright: reportUnnecessaryTypeIgnoreComment=true
"""Static typing of ``Task.from_store`` and ``sd.task_from_store``, checked
by pyright (pre-commit runs it over ``tests``). Each ``assert_type`` must
hold, and each negative case must be an error: its ``pyright: ignore``
would otherwise be reported as unnecessary.
"""

from typing import assert_type

import stardag as sd

sd.auto_namespace(__name__)

ID = "6f1c2c62-1f5c-5a0e-9b8e-3a4f0e1d2c3b"


class Dataset(sd.Task[list[int]]):
    size: int

    def run(self) -> None:
        self._save(list(range(self.size)))


class TrainedModel(sd.Task[str]):
    data: sd.TaskLoads[list[int]]

    def run(self) -> None:
        self._save(str(sum(self.data.load())))


class PlainBase(sd.BaseTask):
    def complete(self) -> bool:
        return True

    def run(self) -> None:
        pass


def check_from_store() -> None:
    """The classmethod returns the class it is called on."""
    assert_type(TrainedModel.from_store(ID), TrainedModel)
    assert_type(TrainedModel.from_store(ID, instance_hash=ID, store="s"), TrainedModel)
    assert_type(PlainBase.from_store(ID), PlainBase)
    assert_type(TrainedModel.from_store(ID).data, sd.TaskLoads[list[int]])
    wrong: Dataset = TrainedModel.from_store(ID)  # pyright: ignore[reportAssignmentType]
    del wrong


def check_function_form() -> None:
    """``sd.task_from_store`` is as precise as the method."""
    assert_type(sd.task_from_store(TrainedModel, ID), TrainedModel)
    assert_type(sd.task_from_store(PlainBase, ID, ID), PlainBase)
    sd.task_from_store(int, ID)  # pyright: ignore[reportArgumentType]


def check_write_to_store() -> None:
    stored: list[sd.StoredTask] = sd.write_to_store([Dataset(size=1)], store="s")
    del stored
