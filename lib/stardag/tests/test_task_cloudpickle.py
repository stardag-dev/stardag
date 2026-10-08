"""An ``@sd.task`` function task survives cloudpickle.

cloudpickle pickles a class it cannot import by name by value, and an
``@sd.task`` class often is one: renamed with ``name=``, or defined in
``__main__`` or a function. With it go the validators pydantic keeps on the
class, and every global their code names. A validator naming a module-level
``ContextVar`` made every such task unpicklable (``TypeError: cannot pickle
'_contextvars.ContextVar' object``), which Modal hit returning a worker's
yielded dependencies to a hybrid driver.
"""

import pytest

import stardag as sd

# Not a core dependency: it comes with the `modal` extra, as Modal's
# serializer.
cloudpickle = pytest.importorskip("cloudpickle")


@sd.task(name="RenamedAddOne")
def add_one(value: int) -> int:
    return value + 1


def _round_trip(task: sd.BaseTask) -> sd.BaseTask:
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
