"""An ``@sd.task`` function task survives cloudpickle.

cloudpickle pickles a class it cannot import by name by value, and an
``@sd.task`` class often is one: renamed with ``name=``, or defined in
``__main__`` or a function. With it go the validators pydantic keeps on the
class, and every global their code names. A validator naming a module-level
``ContextVar`` made every such task unpicklable (``TypeError: cannot pickle
'_contextvars.ContextVar' object``), which Modal hit returning a worker's
yielded dependencies to a hybrid driver.
"""

from typing import Annotated, TypeVar

import pytest

import stardag as sd
from stardag._core.base_task import TaskImplementationError
from stardag.base_model import StardagBaseModel, StardagField

# Not a core dependency: it comes with the `modal` extra, as Modal's
# serializer.
cloudpickle = pytest.importorskip("cloudpickle")

_T = TypeVar("_T", bound=sd.BaseTask)


@sd.task(name="RenamedAddOne")
def add_one(value: int) -> int:
    return value + 1


def _round_trip(task: _T) -> _T:
    return cloudpickle.loads(cloudpickle.dumps(task))


def test_a_renamed_function_task_round_trips_through_cloudpickle():
    task = add_one(value=1)
    assert _round_trip(task).id == task.id


def test_a_function_local_task_with_a_dependency_round_trips():
    @sd.task
    def total(values: sd.Depends[int]) -> int:
        return values

    task = total(values=add_one(value=1))
    assert _round_trip(task).id == task.id


# The class-based shapes. Each is defined inside the test, so cloudpickle
# cannot import it by name and takes the class by value, with every validator
# and global its fields bring along: a non-significant field, a nested model,
# a concrete task parameter, and a polymorphic one. A shape that stops round-tripping
# here fails the base suite instead of a Modal worker.
#
# Both fail today, and not on their fields: cloudpickle rebuilds a by-value
# class as an empty skeleton before setting its attributes, and the run()
# check in `BaseTask.__init_subclass__` rejects the skeleton (STA-133).
# Strict, so the fix has to remove the marker.
_SKELETON_CHECK = pytest.mark.xfail(
    raises=TaskImplementationError,
    strict=True,
    reason="run() check fires on cloudpickle's skeleton class (STA-133)",
)


@_SKELETON_CHECK
def test_a_function_local_class_task_round_trips():
    class Scaled(sd.Task[int]):
        value: int
        threads: Annotated[int, StardagField(significant=False)] = 1

        def run(self) -> None:
            self._save(self.value * 2)

    task = Scaled(value=3, threads=4)
    restored = _round_trip(task)
    assert restored.id == task.id
    assert restored.threads == 4


@_SKELETON_CHECK
def test_a_function_local_class_task_with_nested_and_task_params_round_trips():
    class Options(StardagBaseModel):
        pattern: str
        max_workers: Annotated[int, StardagField(significant=False)] = 4

    class Scale(sd.Task[int]):
        value: int

        def run(self) -> None:
            self._save(self.value)

    class Parse(sd.Task[int]):
        options: Options
        source: Scale
        upstream: sd.TaskLoads[int]

        def run(self) -> None:
            self._save(0)

    task = Parse(
        options=Options(pattern="*"),
        source=Scale(value=2),
        upstream=add_one(value=1),
    )
    restored = _round_trip(task)
    assert restored.id == task.id
    assert restored.source.id == task.source.id
    assert restored.upstream.id == task.upstream.id
