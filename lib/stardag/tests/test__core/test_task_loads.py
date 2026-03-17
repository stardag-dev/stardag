from typing import Annotated

import pytest
from pydantic import ValidationError

from stardag import BaseTask, LoadableTask, TargetTask, Task, auto_namespace
from stardag._core.task_loads import TaskLoads
from stardag.base_model import StardagField
from stardag.polymorphic import Polymorphic, SubClass
from stardag.target import InMemoryTarget, LoadableSaveableTarget, LoadableTarget
from stardag.utils.testing.generic import assert_serialize_validate_roundtrip

auto_namespace(__name__)  # Avoid collisions in task registry

# =============================================================================
# TargetTask classes for testing SubClass[TargetTask[...]] directly
# =============================================================================


class LoadsStrTask(TargetTask[LoadableTarget[str]]):
    """Basic task that loads str (TargetTask-level)."""

    data: str = "hello world"

    def run(self) -> None:
        self.target().save(self.data)

    def target(self) -> InMemoryTarget[str]:
        return InMemoryTarget(key=self.id)


class LoadsStrTaskSubclass(LoadsStrTask):
    """Subclass of LoadsStrTask - should be compatible with SubClass[TargetTask[...]]."""

    extra_field: str = "extra"


class LoadsIntTask(TargetTask[LoadableTarget[int]]):
    """Basic task that loads int (TargetTask-level)."""

    number: int = 42

    def run(self) -> None:
        self.target().save(self.number)

    def target(self) -> InMemoryTarget[int]:
        return InMemoryTarget(key=self.id)


class LoadsStrTaskWithAnnotation(TargetTask[LoadableTarget[str]]):
    """Task with annotated fields - should be compatible with SubClass[TargetTask[...]]."""

    data: Annotated[str, StardagField(hash_exclude=True)] = "annotated"

    def run(self) -> None:
        self.target().save(self.data)

    def target(self) -> InMemoryTarget[str]:
        return InMemoryTarget(key=self.id)


class LoadsSaveableStrTask(TargetTask[LoadableSaveableTarget[str]]):
    """Task using LoadableSaveableTarget[str] - subtype of LoadableTarget[str]."""

    data: str = "saveable"

    def run(self) -> None:
        self.target().save(self.data)

    def target(self) -> InMemoryTarget[str]:
        return InMemoryTarget(key=self.id)


class LoadsListStrTask(TargetTask[LoadableTarget[list[str]]]):
    """Task that loads list[str] (TargetTask-level)."""

    items: list[str] = ["a", "b", "c"]

    def run(self) -> None:
        self.target().save(self.items)

    def target(self) -> InMemoryTarget[list[str]]:
        return InMemoryTarget(key=self.id)


class LoadsListIntTask(TargetTask[LoadableTarget[list[int]]]):
    """Task that loads list[int] (TargetTask-level)."""

    items: list[int] = [1, 2, 3]

    def run(self) -> None:
        self.target().save(self.items)

    def target(self) -> InMemoryTarget[list[int]]:
        return InMemoryTarget(key=self.id)


# =============================================================================
# Task classes for testing TaskLoads
# =============================================================================


class TaskStr(Task[str]):
    """Task that produces str - compatible with TaskLoads[str]."""

    data: str = "auto task string"

    def run(self) -> None:
        self._save(self.data)


class TaskStrSubclass(TaskStr):
    """Subclass of TaskStr - should be compatible with TaskLoads[str]."""

    extra_field: str = "extra"


class TaskInt(Task[int]):
    """Task that produces int - not compatible with TaskLoads[str]."""

    number: int = 42

    def run(self) -> None:
        self._save(self.number)


class TaskListStr(Task[list[str]]):
    """Task that produces list[str] - not compatible with TaskLoads[str]."""

    items: list[str] = ["a", "b", "c"]

    def run(self) -> None:
        self._save(self.items)


class TaskListInt(Task[list[int]]):
    """Task that produces list[int]."""

    items: list[int] = [1, 2, 3]

    def run(self) -> None:
        self._save(self.items)


# =============================================================================
# Bare LoadableTask classes (no target, just load())
# =============================================================================


class BareLoadableStr(LoadableTask[str]):
    """Bare LoadableTask that loads str - compatible with TaskLoads[str]."""

    data: str = "bare loadable"

    def complete(self) -> bool:
        return True

    def run(self) -> None:
        pass

    def load(self) -> str:
        return self.data


class BareLoadableInt(LoadableTask[int]):
    """Bare LoadableTask that loads int - not compatible with TaskLoads[str]."""

    number: int = 99

    def complete(self) -> bool:
        return True

    def run(self) -> None:
        pass

    def load(self) -> int:
        return self.number


# =============================================================================
# Container tasks with TaskLoads fields
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


class ContainerTaskLoadsInt(BaseTask):
    """Container expecting TaskLoads[int]."""

    task: TaskLoads[int]

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
# Container tasks with SubClass[TargetTask[...]] (lower-level API)
# =============================================================================


class ContainerWithSubClass(BaseTask):
    """Container using SubClass[TargetTask[...]] directly."""

    task: SubClass[TargetTask[LoadableTarget[str]]]

    def complete(self) -> bool:
        return True

    def run(self) -> None:
        pass


# =============================================================================
# Tests for TaskLoads with Task subclasses
# =============================================================================


def test_task_loads_basic():
    """Basic test: Task[str] should work with TaskLoads[str]."""
    container = ContainerTaskLoadsStr(task=TaskStr())
    assert_serialize_validate_roundtrip(ContainerTaskLoadsStr, container)


@pytest.mark.parametrize(
    "task_instance,description",
    [
        (TaskStr(), "exact match: Task[str]"),
        (TaskStrSubclass(), "subclass of compatible Task"),
    ],
)
def test_task_loads_compatible_types(task_instance, description):
    """Test that compatible Task types are accepted by TaskLoads."""
    container = ContainerTaskLoadsStr(task=task_instance)
    assert_serialize_validate_roundtrip(ContainerTaskLoadsStr, container)


@pytest.mark.parametrize(
    "container_cls,task_instance,description",
    [
        (
            ContainerTaskLoadsStr,
            TaskInt(),
            "str vs int mismatch",
        ),
        (
            ContainerTaskLoadsListStr,
            TaskListInt(),
            "list[str] vs list[int] - nested generic mismatch",
        ),
        (
            ContainerTaskLoadsStr,
            TaskListStr(),
            "str vs list[str] - different structure",
        ),
    ],
)
def test_task_loads_type_mismatch(container_cls, task_instance, description):
    """Test that incompatible Task types are rejected by TaskLoads."""
    with pytest.raises(ValidationError):
        container_cls(task=task_instance)  # pyright: ignore[reportArgumentType]


def test_task_loads_accepts_bare_loadable_task():
    """TaskLoads[str] should accept bare LoadableTask[str] subclasses."""
    container = ContainerTaskLoadsStr(task=BareLoadableStr())
    assert_serialize_validate_roundtrip(ContainerTaskLoadsStr, container)


def test_task_loads_rejects_bare_loadable_task_type_mismatch():
    """TaskLoads[str] should reject LoadableTask[int] (type mismatch)."""
    with pytest.raises(ValidationError):
        ContainerTaskLoadsStr(task=BareLoadableInt())  # pyright: ignore[reportArgumentType]


def test_task_loads_rejects_target_base_task():
    """TaskLoads[str] should NOT accept raw TargetTask subclasses (not LoadableTask)."""
    with pytest.raises(ValidationError):
        ContainerTaskLoadsStr(task=LoadsStrTask())  # pyright: ignore[reportArgumentType]


def test_annotated_field_compatible():
    """Annotated[TaskLoads[...], ...] should work correctly."""
    container = ContainerWithAnnotatedField(task=TaskStr())
    assert_serialize_validate_roundtrip(ContainerWithAnnotatedField, container)


def test_annotated_field_mismatch():
    """Annotated[TaskLoads[...], ...] should also catch type mismatches."""
    with pytest.raises(ValidationError):
        ContainerWithAnnotatedField(task=TaskInt())  # pyright: ignore[reportArgumentType]


def test_nested_generic_compatible():
    """Tasks with nested generics should work when types match."""
    container = ContainerTaskLoadsListStr(task=TaskListStr())
    assert_serialize_validate_roundtrip(ContainerTaskLoadsListStr, container)


def test_task_int_compatible_with_task_loads_int():
    """Task[int] should be compatible with TaskLoads[int]."""
    container = ContainerTaskLoadsInt(task=TaskInt())
    assert_serialize_validate_roundtrip(ContainerTaskLoadsInt, container)


# =============================================================================
# Tests for SubClass[TargetTask[...]] (lower-level API)
# =============================================================================


def test_subclass_annotation_compatible():
    """SubClass[TargetTask[...]] should accept TargetTask subclasses."""
    container = ContainerWithSubClass(task=LoadsStrTask())
    assert_serialize_validate_roundtrip(ContainerWithSubClass, container)


def test_subclass_annotation_mismatch():
    """SubClass[TargetTask[...]] should catch type mismatches."""
    with pytest.raises(ValidationError) as exc_info:
        ContainerWithSubClass(task=LoadsIntTask())  # pyright: ignore[reportArgumentType]

    error_str = str(exc_info.value)
    assert "LoadsIntTask" in error_str


def test_task_compatible_with_subclass_target_base_task():
    """Task[str] should also be compatible with SubClass[TargetTask[LoadableTarget[str]]].

    This tests the __map_generic_args_to_ancestor__ hook that maps
    Task[str] -> TargetTask[LoadableSaveableFileSystemTarget[str]]
    which is compatible with SubClass[TargetTask[LoadableTarget[str]]]
    because LoadableSaveableFileSystemTarget is a subtype of LoadableTarget.
    """
    container = ContainerWithSubClass(task=TaskStr())
    assert_serialize_validate_roundtrip(ContainerWithSubClass, container)


# =============================================================================
# Tests for on_type_mismatch parameter (uses SubClass[TargetTask[...]])
# =============================================================================


class ContainerWithWarnOnMismatch(BaseTask):
    """Container that warns on type mismatch instead of raising."""

    task: Annotated[
        TargetTask[LoadableTarget[str]],
        Polymorphic(on_generic_type_mismatch="warn"),
    ]

    def complete(self) -> bool:
        return True

    def run(self) -> None:
        pass


class ContainerWithIgnoreOnMismatch(BaseTask):
    """Container that ignores type mismatches."""

    task: Annotated[
        TargetTask[LoadableTarget[str]],
        Polymorphic(on_generic_type_mismatch="ignore"),
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
    """on_type_mismatch='raise' is the default behavior for SubClass[TargetTask[...]]."""
    with pytest.raises(ValidationError):
        ContainerWithSubClass(task=LoadsIntTask())  # pyright: ignore[reportArgumentType]


# =============================================================================
# Tests for TaskLoads with Annotated loaded types
# =============================================================================


class StrMetadataTag:
    """Dummy annotation metadata for testing."""

    pass


AnnotatedStr = Annotated[str, StrMetadataTag()]


class ContainerTaskLoadsAnnotatedStr(BaseTask):
    """Container expecting TaskLoads[Annotated[str, ...]] — metadata should not constrain."""

    task: TaskLoads[AnnotatedStr]  # type: ignore[type-arg]

    def complete(self) -> bool:
        return True

    def run(self) -> None:
        pass


class GenericWrapperTask(LoadableTask[str]):
    """Multi-level LoadableTask subclass without __map_generic_args_to_ancestor__.

    This mimics the WrapperTask[T] → ConcreteWrapper pattern from the bug report,
    using a concrete subclass of LoadableTask with no generic intermediary.
    """

    data: str = "wrapped"

    def complete(self) -> bool:
        return True

    def run(self) -> None:
        pass

    def load(self) -> str:
        return self.data


def test_task_loads_annotated_accepts_task():
    """Task[str] should be accepted by TaskLoads[Annotated[str, ...]] — metadata is not a type constraint."""
    container = ContainerTaskLoadsAnnotatedStr(task=TaskStr())
    assert isinstance(container.task, TaskStr)


def test_task_loads_annotated_accepts_bare_loadable():
    """LoadableTask[str] should be accepted by TaskLoads[Annotated[str, ...]] — metadata is not a type constraint."""
    container = ContainerTaskLoadsAnnotatedStr(task=BareLoadableStr())
    assert isinstance(container.task, BareLoadableStr)


def test_task_loads_annotated_accepts_generic_wrapper():
    """LoadableTask[str] subclass (no __map_generic_args_to_ancestor__) should be accepted by TaskLoads[Annotated[str, ...]].

    This is the 'WrapperTask' pattern from the bug report — a concrete LoadableTask
    subclass whose origin differs from LoadableTask and has no mapper, previously
    accepted via an early bail-out. After the fix both paths (with/without mapper)
    consistently accept a compatible loaded type.
    """
    container = ContainerTaskLoadsAnnotatedStr(task=GenericWrapperTask())
    assert isinstance(container.task, GenericWrapperTask)


def test_task_loads_annotated_rejects_type_mismatch():
    """Task[int] should still be rejected by TaskLoads[Annotated[str, ...]]."""
    with pytest.raises(ValidationError):
        ContainerTaskLoadsAnnotatedStr(task=TaskInt())  # pyright: ignore[reportArgumentType]


def test_task_loads_annotated_task_and_loadable_consistent():
    """Task[str] and LoadableTask[str] should behave consistently with TaskLoads[Annotated[str, ...]]."""
    # Use explicit assertions rather than broad exception catching so that
    # unrelated errors still fail loudly.
    ContainerTaskLoadsAnnotatedStr(task=TaskStr())
    ContainerTaskLoadsAnnotatedStr(task=BareLoadableStr())
    ContainerTaskLoadsAnnotatedStr(task=GenericWrapperTask())
