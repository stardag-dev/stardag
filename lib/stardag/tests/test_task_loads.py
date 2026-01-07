from typing import Annotated

import pytest
from pydantic import ValidationError

from stardag._auto_task import AutoTask
from stardag._task import BaseTask, Task, auto_namespace
from stardag._task_loads import TaskLoads
from stardag.base_model import StardagField
from stardag.polymorphic import Polymorphic, SubClass
from stardag.target import InMemoryTarget, LoadableSaveableTarget, LoadableTarget
from stardag.utils.testing.generic import assert_serialize_validate_roundtrip

auto_namespace(__name__)  # Avoid collisions in task registry

# =============================================================================
# Task classes for testing various generic type scenarios
# =============================================================================


class LoadsStrTask(Task[LoadableTarget[str]]):
    """Basic task that loads str."""

    data: str = "hello world"

    def run(self) -> None:
        self.output().save(self.data)

    def output(self) -> InMemoryTarget[str]:
        return InMemoryTarget(key=self.id)


class LoadsStrTaskSubclass(LoadsStrTask):
    """Subclass of LoadsStrTask - should be compatible with TaskLoads[str]."""

    extra_field: str = "extra"


class LoadsIntTask(Task[LoadableTarget[int]]):
    """Basic task that loads int."""

    number: int = 42

    def run(self) -> None:
        self.output().save(self.number)

    def output(self) -> InMemoryTarget[int]:
        return InMemoryTarget(key=self.id)


class LoadsStrTaskWithAnnotation(Task[LoadableTarget[str]]):
    """Task with annotated fields - should be compatible with TaskLoads[str]."""

    data: Annotated[str, StardagField(hash_exclude=True)] = "annotated"

    def run(self) -> None:
        self.output().save(self.data)

    def output(self) -> InMemoryTarget[str]:
        return InMemoryTarget(key=self.id)


class LoadsSaveableStrTask(Task[LoadableSaveableTarget[str]]):
    """Task using LoadableSaveableTarget[str] - subtype of LoadableTarget[str]."""

    data: str = "saveable"

    def run(self) -> None:
        self.output().save(self.data)

    def output(self) -> InMemoryTarget[str]:
        return InMemoryTarget(key=self.id)


class LoadsListStrTask(Task[LoadableTarget[list[str]]]):
    """Task that loads list[str] - tests nested generics."""

    items: list[str] = ["a", "b", "c"]

    def run(self) -> None:
        self.output().save(self.items)

    def output(self) -> InMemoryTarget[list[str]]:
        return InMemoryTarget(key=self.id)


class LoadsListIntTask(Task[LoadableTarget[list[int]]]):
    """Task that loads list[int] - tests nested generics mismatch."""

    items: list[int] = [1, 2, 3]

    def run(self) -> None:
        self.output().save(self.items)

    def output(self) -> InMemoryTarget[list[int]]:
        return InMemoryTarget(key=self.id)


# =============================================================================
# Container tasks with various field types
# =============================================================================


class ContainerTaskLoadsStr(BaseTask):
    """Container expecting TaskLoads[str]."""

    task: TaskLoads[str]

    def complete(self) -> bool:
        return True

    def run(self) -> None:
        pass


class ContainerTaskLoadsListStr(BaseTask):
    """Container expecting TaskLoads[list[str]]."""

    task: TaskLoads[list[str]]

    def complete(self) -> bool:
        return True

    def run(self) -> None:
        pass


class ContainerWithSubClass(BaseTask):
    """Container using SubClass directly instead of TaskLoads."""

    task: SubClass[Task[LoadableTarget[str]]]

    def complete(self) -> bool:
        return True

    def run(self) -> None:
        pass


class ContainerWithAnnotatedField(BaseTask):
    """Container with annotated TaskLoads field."""

    task: Annotated[TaskLoads[str], StardagField(hash_exclude=True)]

    def complete(self) -> bool:
        return True

    def run(self) -> None:
        pass


# =============================================================================
# Tests
# =============================================================================


def test_task_loads_basic():
    """Basic test: compatible type should work."""
    container = ContainerTaskLoadsStr(task=LoadsStrTask())
    assert_serialize_validate_roundtrip(ContainerTaskLoadsStr, container)


@pytest.mark.parametrize(
    "task_instance,description",
    [
        (LoadsStrTask(), "exact match: Task[LoadableTarget[str]]"),
        (LoadsStrTaskSubclass(), "subclass of compatible task"),
        (LoadsStrTaskWithAnnotation(), "task with annotated fields"),
        # NOTE: LoadableSaveableTarget[str] is a subtype of LoadableTarget[str]
        # but our best-effort check doesn't handle protocol subtyping,
        # so this would fail. If we want to support it, we'd need more
        # sophisticated type checking.
        # (LoadsSaveableStrTask(), "task with LoadableSaveableTarget subtype"),
    ],
)
def test_task_loads_compatible_types(task_instance, description):
    """Test that compatible task types are accepted."""
    container = ContainerTaskLoadsStr(task=task_instance)
    assert_serialize_validate_roundtrip(ContainerTaskLoadsStr, container)


@pytest.mark.parametrize(
    "container_cls,task_instance,expected_type,actual_type,description",
    [
        (
            ContainerTaskLoadsStr,
            LoadsIntTask(),
            "LoadableTarget[str]",
            "LoadableTarget[int]",
            "str vs int mismatch",
        ),
        (
            ContainerTaskLoadsListStr,
            LoadsListIntTask(),
            "LoadableTarget[list[str]]",
            "LoadableTarget[list[int]]",
            "list[str] vs list[int] - nested generic mismatch",
        ),
        (
            ContainerTaskLoadsStr,
            LoadsListStrTask(),
            "LoadableTarget[str]",
            "LoadableTarget[list[str]]",
            "str vs list[str] - different structure",
        ),
    ],
)
def test_task_loads_type_mismatch(
    container_cls, task_instance, expected_type, actual_type, description
):
    """Test that incompatible task types are rejected with clear error messages."""
    with pytest.raises(ValidationError) as exc_info:
        container_cls(task=task_instance)  # pyright: ignore[reportArgumentType]

    error_str = str(exc_info.value)
    assert type(task_instance).__name__ in error_str, f"Failed: {description}"
    assert expected_type in error_str, f"Expected type not in error: {description}"
    assert actual_type in error_str, f"Actual type not in error: {description}"


def test_subclass_annotation_compatible():
    """SubClass[Task[...]] should work the same as TaskLoads[...]."""
    container = ContainerWithSubClass(task=LoadsStrTask())
    assert_serialize_validate_roundtrip(ContainerWithSubClass, container)


def test_subclass_annotation_mismatch():
    """SubClass[Task[...]] should also catch type mismatches."""
    with pytest.raises(ValidationError) as exc_info:
        ContainerWithSubClass(task=LoadsIntTask())  # pyright: ignore[reportArgumentType]

    error_str = str(exc_info.value)
    assert "LoadsIntTask" in error_str


def test_annotated_field_compatible():
    """Annotated[TaskLoads[...], ...] should work correctly."""
    container = ContainerWithAnnotatedField(task=LoadsStrTask())
    assert_serialize_validate_roundtrip(ContainerWithAnnotatedField, container)


def test_annotated_field_mismatch():
    """Annotated[TaskLoads[...], ...] should also catch type mismatches."""
    with pytest.raises(ValidationError) as exc_info:
        ContainerWithAnnotatedField(task=LoadsIntTask())  # pyright: ignore[reportArgumentType]

    error_str = str(exc_info.value)
    assert "LoadsIntTask" in error_str
    assert "LoadableTarget[str]" in error_str
    assert "LoadableTarget[int]" in error_str


def test_nested_generic_compatible():
    """Tasks with nested generics should work when types match."""
    container = ContainerTaskLoadsListStr(task=LoadsListStrTask())
    assert_serialize_validate_roundtrip(ContainerTaskLoadsListStr, container)


# =============================================================================
# Tests for on_type_mismatch parameter
# =============================================================================


class ContainerWithWarnOnMismatch(BaseTask):
    """Container that warns on type mismatch instead of raising."""

    task: Annotated[
        Task[LoadableTarget[str]], Polymorphic(on_generic_type_mismatch="warn")
    ]

    def complete(self) -> bool:
        return True

    def run(self) -> None:
        pass


class ContainerWithIgnoreOnMismatch(BaseTask):
    """Container that ignores type mismatches."""

    task: Annotated[
        Task[LoadableTarget[str]], Polymorphic(on_generic_type_mismatch="ignore")
    ]

    def complete(self) -> bool:
        return True

    def run(self) -> None:
        pass


def test_on_type_mismatch_warn():
    """on_type_mismatch='warn' should emit warning but accept value."""
    with pytest.warns(UserWarning, match="LoadsIntTask.*not compatible"):
        container = ContainerWithWarnOnMismatch(task=LoadsIntTask())  # pyright: ignore[reportArgumentType]

    # Value should be accepted
    assert isinstance(container.task, LoadsIntTask)


def test_on_type_mismatch_ignore():
    """on_type_mismatch='ignore' should silently accept mismatched value."""
    # Should not raise or warn
    container = ContainerWithIgnoreOnMismatch(task=LoadsIntTask())  # pyright: ignore[reportArgumentType]

    # Value should be accepted
    assert isinstance(container.task, LoadsIntTask)


def test_on_type_mismatch_raise_is_default():
    """on_type_mismatch='raise' is the default behavior."""
    # ContainerTaskLoadsStr uses TaskLoads which uses Polymorphic() with default
    with pytest.raises(ValidationError):
        ContainerTaskLoadsStr(task=LoadsIntTask())  # pyright: ignore[reportArgumentType]


# =============================================================================
# Tests for AutoTask compatibility with TaskLoads
# =============================================================================


class AutoTaskStr(AutoTask[str]):
    """AutoTask that produces str - should be compatible with TaskLoads[str]."""

    data: str = "auto task string"

    def run(self) -> None:
        self.output().save(self.data)


class AutoTaskInt(AutoTask[int]):
    """AutoTask that produces int - not compatible with TaskLoads[str]."""

    number: int = 42

    def run(self) -> None:
        self.output().save(self.number)


class AutoTaskListStr(AutoTask[list[str]]):
    """AutoTask that produces list[str] - not compatible with TaskLoads[str]."""

    items: list[str] = ["a", "b", "c"]

    def run(self) -> None:
        self.output().save(self.items)


def test_autotask_compatible_with_task_loads():
    """AutoTask[str] should be compatible with TaskLoads[str].

    This tests the __map_generic_args_to_ancestor__ hook that maps
    AutoTask[str] -> Task[LoadableSaveableFileSystemTarget[str]]
    which is compatible with TaskLoads[str] (= Task[LoadableTarget[str]])
    because LoadableSaveableFileSystemTarget is a subtype of LoadableTarget.
    """
    container = ContainerTaskLoadsStr(task=AutoTaskStr())
    assert_serialize_validate_roundtrip(ContainerTaskLoadsStr, container)


def test_autotask_type_mismatch_detected():
    """AutoTask[int] should NOT be compatible with TaskLoads[str].

    The __map_generic_args_to_ancestor__ hook should enable proper type checking
    even when origins differ (AutoTask vs Task).
    """
    with pytest.raises(ValidationError) as exc_info:
        ContainerTaskLoadsStr(task=AutoTaskInt())  # pyright: ignore[reportArgumentType]

    error_str = str(exc_info.value)
    assert "AutoTaskInt" in error_str


def test_autotask_nested_generic_mismatch():
    """AutoTask[list[str]] should NOT be compatible with TaskLoads[str]."""
    with pytest.raises(ValidationError) as exc_info:
        ContainerTaskLoadsStr(task=AutoTaskListStr())  # pyright: ignore[reportArgumentType]

    error_str = str(exc_info.value)
    assert "AutoTaskListStr" in error_str


class ContainerTaskLoadsInt(BaseTask):
    """Container expecting TaskLoads[int]."""

    task: TaskLoads[int]

    def complete(self) -> bool:
        return True

    def run(self) -> None:
        pass


def test_autotask_int_compatible_with_task_loads_int():
    """AutoTask[int] should be compatible with TaskLoads[int]."""
    container = ContainerTaskLoadsInt(task=AutoTaskInt())
    assert_serialize_validate_roundtrip(ContainerTaskLoadsInt, container)
