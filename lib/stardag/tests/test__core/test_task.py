import asyncio
from datetime import datetime
from typing import Annotated, Type
from unittest.mock import Mock

import pytest

from stardag._core.auto_task import AutoTask
from stardag._core.decorator import task as task_decorator
from stardag._core.task import (
    BaseTask,
    Task,
    TaskImplementationError,
    TaskStruct,
    _has_custom_run,
    _has_custom_run_aio,
    flatten_task_struct,
)
from stardag._core.task_id import _get_task_id_from_jsonable, _get_task_id_jsonable
from stardag.base_model import StardagBaseModel, StardagField
from stardag.polymorphic import NAME_KEY, NAMESPACE_KEY, SubClass, TypeId
from stardag.registry._base import RegistryABC, TaskMetadata
from stardag.target._in_memory import InMemoryTarget
from stardag.utils.testing.generic import assert_serialize_validate_roundtrip
from stardag.utils.testing.namepace import (
    ClearNamespaceByArg,
    ClearNamespaceByDunder,
    CustomNameByArgFromIntermediate,
    CustomNameByArgFromIntermediateChild,
    CustomNameByArgFromTask,
    CustomNameByArgFromTaskChild,
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

    # Task without run() or run_aio() raises at class definition time
    with pytest.raises(
        TaskImplementationError,
        match="must implement either run\\(\\) or run_aio\\(\\)",
    ):

        class TaskNoRun(BaseTask):
            def complete(self) -> bool:
                return False


def test_run_version_checked():
    class VersionedTask(MockBaseTask):
        __version__ = "1.0"

        version: str = "1.0"

    task = VersionedTask(version="1.0")
    # Should not raise - version matches
    task.run()

    task_invalid = VersionedTask(version="2.0")
    with pytest.raises(ValueError, match="Task version mismatch"):
        task_invalid.run()


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
                NAME_KEY: "BasicTask",
                NAMESPACE_KEY: "",
                "version": "",
                "a": 5,
            },
        ),
        (
            "hash exclude (a) should be excluded",
            HashExcludeTask(a=10),
            {
                NAME_KEY: "HashExcludeTask",
                NAMESPACE_KEY: "",
                "version": "",
            },
        ),
        (
            "compat default (a == compat_default) should be excluded",
            CompatDefaultTask(a=42),
            {
                NAME_KEY: "CompatDefaultTask",
                NAMESPACE_KEY: "",
                "version": "",
            },
        ),
        (
            "compat default (a != compat_default) should be included",
            CompatDefaultTask(a=10),
            {
                NAME_KEY: "CompatDefaultTask",
                NAMESPACE_KEY: "",
                "version": "",
                "a": 10,
            },
        ),
        (
            'nested task should be hash serialized as {"id": task.id}',
            WithNestedTask(task=BasicTask(a=7)),
            {
                NAME_KEY: "WithNestedTask",
                NAMESPACE_KEY: "",
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
                NAME_KEY: "ComplexNestedTask",
                NAMESPACE_KEY: "",
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

    def run(self) -> None:
        pass


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
        # name override
        (
            CustomNameByArgFromIntermediate,
            TypeId(namespace=_testing_module, name="custom_name"),
        ),
        (
            CustomNameByArgFromIntermediateChild,
            TypeId(
                namespace=_testing_module,
                name="CustomNameByArgFromIntermediateChild",
            ),
        ),
        (
            CustomNameByArgFromTask,
            TypeId(namespace=_testing_module, name="custom_name_2"),
        ),
        (
            CustomNameByArgFromTaskChild,
            TypeId(namespace=_testing_module, name="CustomNameByArgFromTaskChild"),
        ),
    ],
)
def test_auto_namespace(task_class: Type[Task], expected_type_id: TypeId):
    assert task_class.__type_id__ == expected_type_id
    namespace = task_class.get_namespace()
    name = task_class.get_name()
    assert TypeId(namespace, name) == expected_type_id
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


# =============================================================================
# Tests for run/run_aio symmetry
# =============================================================================


class TestRunRunAioSymmetry:
    """Tests for bidirectional run() / run_aio() default implementations."""

    def test_sync_only_task(self):
        """Task implementing only run() should have working run_aio()."""

        class SyncOnlyTask(BaseTask):
            result: list = []

            def complete(self) -> bool:
                return bool(self.result)

            def run(self) -> None:
                self.result.append("sync")

        task = SyncOnlyTask()

        # Verify detection
        assert _has_custom_run(task) is True
        assert _has_custom_run_aio(task) is False

        # run() should work directly
        task.run()
        assert task.result == ["sync"]

        # run_aio() should delegate to run()
        task2 = SyncOnlyTask()
        asyncio.run(task2.run_aio())
        assert task2.result == ["sync"]

    def test_async_only_task(self):
        """Task implementing only run_aio() should have working run()."""

        class AsyncOnlyTask(BaseTask):
            result: list = []

            def complete(self) -> bool:
                return bool(self.result)

            async def run_aio(self) -> None:
                await asyncio.sleep(0)  # Simulate async work
                self.result.append("async")

        task = AsyncOnlyTask()

        # Verify detection
        assert _has_custom_run(task) is False
        assert _has_custom_run_aio(task) is True

        # run_aio() should work directly
        asyncio.run(task.run_aio())
        assert task.result == ["async"]

        # run() should use asyncio.run() to call run_aio()
        task2 = AsyncOnlyTask()
        task2.run()
        assert task2.result == ["async"]

    def test_both_implemented(self):
        """Task implementing both run() and run_aio() should use each directly."""

        class BothTask(BaseTask):
            result: list = []

            def complete(self) -> bool:
                return bool(self.result)

            def run(self) -> None:
                self.result.append("sync")

            async def run_aio(self) -> None:
                await asyncio.sleep(0)
                self.result.append("async")

        task = BothTask()

        # Verify detection
        assert _has_custom_run(task) is True
        assert _has_custom_run_aio(task) is True

        # Each method should use its own implementation
        task.run()
        assert task.result == ["sync"]

        task2 = BothTask()
        asyncio.run(task2.run_aio())
        assert task2.result == ["async"]

    def test_neither_implemented_raises_at_class_definition(self):
        """Task implementing neither run() nor run_aio() should fail at class definition."""
        with pytest.raises(
            TaskImplementationError,
            match="must implement either run\\(\\) or run_aio\\(\\)",
        ):

            class NeitherTask(BaseTask):
                def complete(self) -> bool:
                    return True

    def test_sync_default_from_async_context_raises(self):
        """Calling run() from async context when only run_aio() is implemented should raise."""

        class AsyncOnlyTask(BaseTask):
            def complete(self) -> bool:
                return True

            async def run_aio(self) -> None:
                await asyncio.sleep(0)

        task = AsyncOnlyTask()

        async def call_run_from_async():
            # This should raise because we're in an async context
            # and the task only has run_aio()
            task.run()

        with pytest.raises(
            RuntimeError,
            match="Cannot call AsyncOnlyTask.run\\(\\) from within an async context",
        ):
            asyncio.run(call_run_from_async())

    def test_sync_default_from_async_context_error_message(self):
        """Error message when calling run() from async context should be helpful."""

        class MyAsyncTask(BaseTask):
            def complete(self) -> bool:
                return True

            async def run_aio(self) -> None:
                pass

        task = MyAsyncTask()

        async def call_run_from_async():
            task.run()

        with pytest.raises(RuntimeError) as exc_info:
            asyncio.run(call_run_from_async())

        error_msg = str(exc_info.value)
        # Check that the error message contains helpful suggestions
        assert "await task.run_aio()" in error_msg
        assert "Implement run()" in error_msg
        assert "asyncio.to_thread" in error_msg

    def test_abstract_class_skips_validation(self):
        """Abstract subclasses should not be required to implement run/run_aio."""
        import abc

        # This should not raise - it's abstract
        class AbstractTask(BaseTask, abc.ABC):
            @abc.abstractmethod
            def complete(self) -> bool: ...

        # Concrete subclass must implement run or run_aio
        with pytest.raises(TaskImplementationError):

            class ConcreteWithoutRun(AbstractTask):
                def complete(self) -> bool:
                    return True

    def test_async_generator_run_aio_raises_on_sync_call(self):
        """Async generator run_aio() cannot be auto-converted to sync run()."""

        class AsyncDynamicTask(BaseTask):
            depth: int = 2

            def complete(self) -> bool:
                return self.depth == 0

            async def run_aio(self):  # type: ignore[override]
                if self.depth > 0:
                    yield AsyncDynamicTask(depth=self.depth - 1)

        task = AsyncDynamicTask(depth=1)
        assert task.has_dynamic_deps() is True

        # run() should raise NotImplementedError for async generators
        with pytest.raises(
            NotImplementedError,
            match="async generator.*cannot be automatically converted",
        ):
            task.run()

    @pytest.mark.asyncio
    async def test_async_generator_run_aio_works_directly(self):
        """Async generator run_aio() works when called directly."""

        class AsyncDynamicTask(BaseTask):
            depth: int = 2
            result: list = []

            def complete(self) -> bool:
                return self.depth == 0

            async def run_aio(self):  # type: ignore[override]
                if self.depth > 0:
                    yield AsyncDynamicTask(depth=self.depth - 1)
                self.result.append(f"depth-{self.depth}")

        task = AsyncDynamicTask(depth=1)

        # run_aio() should work directly as an async generator
        gen = task.run_aio()
        yielded = await gen.asend(None)
        assert isinstance(yielded, AsyncDynamicTask)
        assert yielded.depth == 0


def test_from_registry(default_in_memory_fs_target):
    class MockTask(AutoTask[str]):
        a: int
        b: str

        def run(self):
            self.output().save(self.a * self.b)

    task = MockTask(a=5, b="test")
    task.run()

    mock_registry = Mock(spec=RegistryABC)
    mock_registry.task_get_metadata.return_value = TaskMetadata(
        id=task.id,
        body=task.model_dump(),
        name=task.get_name(),
        namespace=task.get_namespace(),
        version=task.version,
        output_uri=task.output().uri,
        status="completed",
        registered_at=datetime.now(),
        started_at=datetime.now(),
        completed_at=datetime.now(),
        error_message=None,
    )
    loaded_task = MockTask.from_registry(id=task.id, registry=mock_registry)
    assert loaded_task == task
