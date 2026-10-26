"""An ``@sd.task`` function task survives cloudpickle.

cloudpickle pickles a class it cannot import by name by value, and an
``@sd.task`` class often is one: renamed with ``name=``, or defined in
``__main__`` or a function. With it go the validators pydantic keeps on the
class, and every global their code names. A validator naming a module-level
``ContextVar`` made every such task unpicklable (``TypeError: cannot pickle
'_contextvars.ContextVar' object``), which Modal hit returning a worker's
yielded dependencies to a hybrid driver.
"""

import threading
from typing import Annotated, ClassVar, TypeVar

import pytest

import stardag as sd
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
# cloudpickle rebuilds a by-value class as an empty skeleton and sets its
# attributes, `run` among them, only afterwards; the run() check therefore
# runs at construction, not class creation (STA-133).


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
    # The precheck wrapping is applied at class creation, so it reaches the
    # skeleton through the pickled class `__dict__`.
    assert getattr(type(restored).run, "__precheck_wrapped__", False)


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


def _construct_an_unpicklable_class_task() -> None:
    # In its own function: pydantic keeps the defining function's locals on a
    # model class, so a sibling class would reach the pickle that way instead.
    class HoldsALock(sd.Task[int]):
        lock: ClassVar[object] = threading.Lock()

        def run(self) -> None:
            self._save(0)

    HoldsALock()


def test_a_class_task_round_trips_after_an_unpicklable_one_was_constructed():
    # The run() check remembers the classes it has passed. If the validator
    # carried on every task class named that record directly, cloudpickle
    # would pickle it along with a by-value class, and with it every class
    # constructed so far, picklable or not.
    _construct_an_unpicklable_class_task()

    class Scaled(sd.Task[int]):
        value: int

        def run(self) -> None:
            self._save(self.value * 2)

    task = Scaled(value=3)
    assert _round_trip(task).id == task.id
