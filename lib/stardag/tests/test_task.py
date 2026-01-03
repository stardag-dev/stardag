from typing import Annotated

import pytest

from stardag._task import TaskBase
from stardag._task_id import _get_task_id_from_jsonable, _get_task_id_jsonable
from stardag.base_model import StardagField
from stardag.polymorphic import TYPE_NAME_KEY, TYPE_NAMESPACE_KEY


class MockBaseTask(TaskBase):
    def complete(self) -> bool:
        return True

    def run(self) -> None:
        pass


def test_task_base_subclassing():
    task = MockBaseTask()
    assert isinstance(task, TaskBase)
    assert task.complete() is True
    assert task.run() is None

    class TaskNoComplete(TaskBase):
        def run(self) -> None:
            pass

    with pytest.raises(
        TypeError,
        match="Can't instantiate abstract class TaskNoComplete without an implementation for abstract method 'complete'",
    ):
        TaskNoComplete()  # type: ignore

    class TaskNoRun(TaskBase):
        def complete(self) -> bool:
            return False

    with pytest.raises(
        TypeError,
        match="Can't instantiate abstract class TaskNoRun without an implementation for abstract method 'run'",
    ):
        TaskNoRun()  # type: ignore


def test_run_version_checked():
    class VersionedTask(MockBaseTask):
        __version__ = "1.0"

        version: str = "1.0"

    task = VersionedTask(version="1.0")
    # Should not raise
    task.run_version_checked()

    task_invalid = VersionedTask(version="2.0")
    with pytest.raises(ValueError, match="TODO"):
        task_invalid.run_version_checked()


def test_dynamic_deps():
    class DynamicDepsTask(TaskBase):
        a: int = 3

        def complete(self) -> bool:
            return True

        def run(self):
            yield DynamicDepsTask(a=self.a - 1)

    class StaticDepsTask(MockBaseTask):
        pass

    dynamic_task = DynamicDepsTask()
    static_task = StaticDepsTask()

    assert dynamic_task.has_dynamic_deps() is True
    assert static_task.has_dynamic_deps() is False


class BasicTask(MockBaseTask):
    a: int


class HashExcludeTask(MockBaseTask):
    a: Annotated[int, StardagField(hash_exclude=True)]


class CompatDefaultTask(MockBaseTask):
    a: Annotated[int, StardagField(compat_default=42)]


class WithNestedTask(MockBaseTask):
    task: BasicTask


@pytest.mark.parametrize(
    "description, task, expected_task_id_jsonable",
    [
        (
            "basic task should include all fields",
            BasicTask(a=5),
            {
                TYPE_NAME_KEY: "BasicTask",
                TYPE_NAMESPACE_KEY: "",
                "version": "",
                "a": 5,
            },
        ),
        (
            "hash exclude (a) should be excluded",
            HashExcludeTask(a=10),
            {
                TYPE_NAME_KEY: "HashExcludeTask",
                TYPE_NAMESPACE_KEY: "",
                "version": "",
            },
        ),
        (
            "compat default (a == compat_default) should be excluded",
            CompatDefaultTask(a=42),
            {
                TYPE_NAME_KEY: "CompatDefaultTask",
                TYPE_NAMESPACE_KEY: "",
                "version": "",
            },
        ),
        (
            "compat default (a != compat_default) should be included",
            CompatDefaultTask(a=10),
            {
                TYPE_NAME_KEY: "CompatDefaultTask",
                TYPE_NAMESPACE_KEY: "",
                "version": "",
                "a": 10,
            },
        ),
        (
            'nested task should be hash serialized as {"id": task.id}',
            WithNestedTask(task=BasicTask(a=7)),
            {
                TYPE_NAME_KEY: "WithNestedTask",
                TYPE_NAMESPACE_KEY: "",
                "version": "",
                "task": {"id": str(BasicTask(a=7).id)},
            },
        ),
    ],
)
def test__id_hashable_jsonable(
    description: str,
    task: TaskBase,
    expected_task_id_jsonable: dict,
):
    assert (
        _get_task_id_jsonable(task) == expected_task_id_jsonable
    ), f"Unexpected id_hashable_jsonable: {description}"
    assert task.id == _get_task_id_from_jsonable(
        expected_task_id_jsonable
    ), f"Unexpected id: {description}"
