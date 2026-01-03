from typing import Annotated, Type

import pytest

from stardag._auto_task import AutoTask
from stardag._decorator import task as task_decorator
from stardag._task import (
    BaseTask,
    Task,
    TaskStruct,
    flatten_task_struct,
)
from stardag._task_id import _get_task_id_from_jsonable, _get_task_id_jsonable
from stardag.base_model import StardagBaseModel, StardagField
from stardag.polymorphic import TYPE_NAME_KEY, TYPE_NAMESPACE_KEY, SubClass, TypeId
from stardag.target._in_memory import InMemoryTarget
from stardag.utils.testing.generic import assert_serialize_validate_roundtrip
from stardag.utils.testing.namepace import (
    ClearNamespaceByArg,
    ClearNamespaceByDunder,
    CustomFamilyByArgFromIntermediate,
    CustomFamilyByArgFromIntermediateChild,
    CustomFamilyByArgFromTask,
    CustomFamilyByArgFromTaskChild,
    CustomFamilyByDUnder,
    CustomFamilyByDUnderChild,
    OverrideNamespaceByArg,
    OverrideNamespaceByArgChild,
    OverrideNamespaceByDUnder,
    OverrideNamespaceByDUnderChild,
    UnspecifiedNamespace,
)
from stardag.utils.testing.simple_dag import LeafTask


class MockBaseTask(BaseTask):
    def complete(self) -> bool:
        return True

    def run(self) -> None:
        pass


def test_task_base_subclassing():
    task = MockBaseTask()
    assert isinstance(task, BaseTask)
    assert task.complete() is True
    assert task.run() is None

    class TaskNoComplete(BaseTask):
        def run(self) -> None:
            pass

    with pytest.raises(
        TypeError,
        # NOTE message varies between Python versions (full below ok for >=3.11)
        # match="Can't instantiate abstract class TaskNoComplete without an implementation for abstract method 'complete'",
        match="Can't instantiate abstract class TaskNoComplete",
    ):
        TaskNoComplete()  # type: ignore

    class TaskNoRun(BaseTask):
        def complete(self) -> bool:
            return False

    with pytest.raises(
        TypeError,
        # NOTE message varies between Python versions (full below ok for >=3.11)
        # match="Can't instantiate abstract class TaskNoRun without an implementation for abstract method 'run'",
        match="Can't instantiate abstract class TaskNoRun",
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
    class DynamicDepsTask(BaseTask):
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


class NonTaskModel(StardagBaseModel):
    a: Annotated[int, StardagField(hash_exclude=True)]
    b: Annotated[str, StardagField(compat_default="default")]
    tasks: tuple[SubClass[BaseTask], ...]


class ComplexNestedTask(MockBaseTask):
    model: NonTaskModel


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
        (
            "complex nested task with various hash/compat exclusions",
            ComplexNestedTask(
                model=NonTaskModel(
                    a=1,
                    b="non-default",
                    tasks=(
                        BasicTask(a=3),
                        WithNestedTask(task=BasicTask(a=4)),
                    ),
                )
            ),
            {
                TYPE_NAME_KEY: "ComplexNestedTask",
                TYPE_NAMESPACE_KEY: "",
                "version": "",
                "model": {
                    "b": "non-default",
                    "tasks": [
                        {"id": str(BasicTask(a=3).id)},
                        {"id": str(WithNestedTask(task=BasicTask(a=4)).id)},
                    ],
                },
            },
        ),
    ],
)
def test__id_hashable_jsonable(
    description: str,
    task: BaseTask,
    expected_task_id_jsonable: dict,
):
    assert _get_task_id_jsonable(task) == expected_task_id_jsonable, (
        f"Unexpected id_hashable_jsonable: {description}"
    )
    assert task.id == _get_task_id_from_jsonable(expected_task_id_jsonable), (
        f"Unexpected id: {description}"
    )

    # verify serialization roundtrip
    assert_serialize_validate_roundtrip(task.__class__, task)


class TaskWithTarget(Task[InMemoryTarget[str]]):
    data: str = "hello world"

    def run(self) -> None:
        self.output().save(self.data)

    def output(self) -> InMemoryTarget[str]:
        return InMemoryTarget(key=self.id)


def test_task_with_target_serialization():
    task = TaskWithTarget(data="test data")

    # verify serialization/deserialization roundtrip
    assert_serialize_validate_roundtrip(TaskWithTarget, task)


class MockTask(AutoTask[str]):
    a: int


_testing_module = "stardag.utils.testing"


@pytest.mark.parametrize(
    "task_class,expected_type_id",
    [
        # namespace
        (MockTask, TypeId(namespace="", name="MockTask")),
        (LeafTask, TypeId(namespace=_testing_module + ".simple_dag", name="LeafTask")),
        (
            UnspecifiedNamespace,
            TypeId(namespace=_testing_module, name="UnspecifiedNamespace"),
        ),
        # namespace override by dunder
        (
            OverrideNamespaceByDUnder,
            TypeId(namespace="override_namespace", name="OverrideNamespaceByDUnder"),
        ),
        (ClearNamespaceByDunder, TypeId(namespace="", name="ClearNamespaceByDunder")),
        (
            OverrideNamespaceByDUnderChild,
            TypeId(
                namespace="override_namespace", name="OverrideNamespaceByDUnderChild"
            ),
        ),
        # namespace override by arg
        (
            OverrideNamespaceByArg,
            TypeId(namespace="override_namespace", name="OverrideNamespaceByArg"),
        ),
        (ClearNamespaceByArg, TypeId(namespace="", name="ClearNamespaceByArg")),
        (
            OverrideNamespaceByArgChild,
            TypeId(namespace=_testing_module, name="OverrideNamespaceByArgChild"),
        ),
        # family override
        (
            CustomFamilyByArgFromIntermediate,
            TypeId(namespace=_testing_module, name="custom_family"),
        ),
        (
            CustomFamilyByArgFromIntermediateChild,
            TypeId(
                namespace=_testing_module, name="CustomFamilyByArgFromIntermediateChild"
            ),
        ),
        (
            CustomFamilyByArgFromTask,
            TypeId(namespace=_testing_module, name="custom_family_2"),
        ),
        (
            CustomFamilyByArgFromTaskChild,
            TypeId(namespace=_testing_module, name="CustomFamilyByArgFromTaskChild"),
        ),
        (
            CustomFamilyByDUnder,
            TypeId(namespace=_testing_module, name="custom_family_3"),
        ),
        (
            CustomFamilyByDUnderChild,
            TypeId(namespace=_testing_module, name="custom_family_3_child"),
        ),
    ],
)
def test_auto_namespace(task_class: Type[Task], expected_type_id: TypeId):
    assert task_class.__type_id__ == expected_type_id
    namespace = task_class.get_type_namespace()
    type_name = task_class.get_type_name()
    assert TypeId(namespace, type_name) == expected_type_id
    assert BaseTask._registry().get_class(expected_type_id) == task_class


@task_decorator
def mock_task(key: str) -> str:
    return key


@pytest.mark.parametrize(
    "task_struct, expected",
    [
        (
            mock_task(key="a"),
            [mock_task(key="a")],
        ),
        (
            [mock_task(key="a"), mock_task(key="b")],
            [mock_task(key="a"), mock_task(key="b")],
        ),
        (
            {"a": mock_task(key="a"), "b": mock_task(key="b")},
            [mock_task(key="a"), mock_task(key="b")],
        ),
        (
            {"a": mock_task(key="a"), "b": [mock_task(key="b"), mock_task(key="c")]},
            [mock_task(key="a"), mock_task(key="b"), mock_task(key="c")],
        ),
        (
            [mock_task(key="a"), {"b": mock_task(key="b"), "c": mock_task(key="c")}],
            [mock_task(key="a"), mock_task(key="b"), mock_task(key="c")],
        ),
    ],
)
def test_flatten_task_struct(task_struct: TaskStruct, expected: list[Task]):
    assert flatten_task_struct(task_struct) == expected
