import asyncio
import typing

import pytest

from stardag import LoadValidator, Task, auto_namespace

auto_namespace(__name__)


# ---------------------------------------------------------------------------
# Example validators
# ---------------------------------------------------------------------------


class HasPrefix(LoadValidator[str]):
    """Validates that a string starts with a given prefix."""

    def __init__(self, prefix: str) -> None:
        self.prefix = prefix

    def validate(self, value: str) -> str:
        if not value.startswith(self.prefix):
            raise ValueError(
                f"Expected value to start with {self.prefix!r}, got {value!r}"
            )
        return value


class StripWhitespace(LoadValidator[str]):
    """Transforms a string by stripping leading/trailing whitespace."""

    def validate(self, value: str) -> str:
        return value.strip()


class PositiveInt(LoadValidator[int]):
    """Validates that an integer is positive."""

    def validate(self, value: int) -> int:
        if value <= 0:
            raise ValueError(f"Expected positive integer, got {value}")
        return value


class ClampMax(LoadValidator[int]):
    """Clamps an integer to a maximum value."""

    def __init__(self, max_value: int) -> None:
        self.max_value = max_value

    def validate(self, value: int) -> int:
        return min(value, self.max_value)


class NonEmptyList(LoadValidator[list]):
    """Validates that a list is not empty."""

    def validate(self, value: list) -> list:
        if not value:
            raise ValueError("List must not be empty")
        return value


# ---------------------------------------------------------------------------
# Task definitions using validators
# ---------------------------------------------------------------------------


class PrefixTask(Task[typing.Annotated[str, HasPrefix("x")]]):
    value: str

    def run(self):
        self._save(self.value)


class StripAndPrefixTask(
    Task[typing.Annotated[str, StripWhitespace(), HasPrefix("x")]]
):
    value: str

    def run(self):
        self._save(self.value)


class PositiveIntTask(Task[typing.Annotated[int, PositiveInt()]]):
    value: int

    def run(self):
        self._save(self.value)


class ClampedIntTask(Task[typing.Annotated[int, ClampMax(100)]]):
    value: int

    def run(self):
        self._save(self.value)


class NoValidatorTask(Task[str]):
    value: str

    def run(self):
        self._save(self.value)


class NonEmptyListTask(Task[typing.Annotated[list[int], NonEmptyList()]]):
    items: list[int]

    def run(self):
        self._save(self.items)


# ---------------------------------------------------------------------------
# Tests: Validator discovery
# ---------------------------------------------------------------------------


class TestValidatorDiscovery:
    """Tests that validators are correctly extracted from Annotated types."""

    def test_single_validator_discovered(self):
        assert len(PrefixTask._load_validators) == 1
        assert isinstance(PrefixTask._load_validators[0], HasPrefix)

    def test_multiple_validators_discovered(self):
        assert len(StripAndPrefixTask._load_validators) == 2
        assert isinstance(StripAndPrefixTask._load_validators[0], StripWhitespace)
        assert isinstance(StripAndPrefixTask._load_validators[1], HasPrefix)

    def test_no_validators_when_not_annotated(self):
        assert NoValidatorTask._load_validators == ()

    def test_validator_preserves_init_args(self):
        assert PrefixTask._load_validators[0].prefix == "x"  # type: ignore[attr-defined]

    def test_clamp_validator_discovered(self):
        assert len(ClampedIntTask._load_validators) == 1
        assert isinstance(ClampedIntTask._load_validators[0], ClampMax)


# ---------------------------------------------------------------------------
# Tests: Validation on _save
# ---------------------------------------------------------------------------


class TestSaveValidation:
    """Tests that validators run during _save."""

    def test_save_passes_when_valid(self, default_in_memory_fs_target):
        task = PrefixTask(value="x_hello")
        task.run()
        assert task.target().load() == "x_hello"

    def test_save_raises_when_invalid(self, default_in_memory_fs_target):
        task = PrefixTask(value="no_prefix")
        with pytest.raises(ValueError, match="Expected value to start with 'x'"):
            task.run()

    def test_save_transforms_value(self, default_in_memory_fs_target):
        task = ClampedIntTask(value=200)
        task.run()
        assert task.target().load() == 100

    def test_save_with_positive_int_valid(self, default_in_memory_fs_target):
        task = PositiveIntTask(value=42)
        task.run()
        assert task.target().load() == 42

    def test_save_with_positive_int_invalid(self, default_in_memory_fs_target):
        task = PositiveIntTask(value=-1)
        with pytest.raises(ValueError, match="Expected positive integer"):
            task.run()

    def test_save_chained_validators_strip_then_prefix(
        self, default_in_memory_fs_target
    ):
        task = StripAndPrefixTask(value="  x_hello  ")
        task.run()
        assert task.target().load() == "x_hello"

    def test_save_chained_validators_strip_then_prefix_invalid(
        self, default_in_memory_fs_target
    ):
        task = StripAndPrefixTask(value="  no_prefix  ")
        with pytest.raises(ValueError, match="Expected value to start with 'x'"):
            task.run()

    def test_save_no_validators_passes_through(self, default_in_memory_fs_target):
        task = NoValidatorTask(value="anything")
        task.run()
        assert task.target().load() == "anything"

    def test_save_non_empty_list_valid(self, default_in_memory_fs_target):
        task = NonEmptyListTask(items=[1, 2, 3])
        task.run()
        assert task.target().load() == [1, 2, 3]

    def test_save_non_empty_list_invalid(self, default_in_memory_fs_target):
        task = NonEmptyListTask(items=[])
        with pytest.raises(ValueError, match="List must not be empty"):
            task.run()


# ---------------------------------------------------------------------------
# Tests: Validation on load
# ---------------------------------------------------------------------------


class TestLoadValidation:
    """Tests that validators run during load."""

    def test_load_validates_stored_data(self, default_in_memory_fs_target):
        task = PrefixTask(value="x_valid")
        # Save raw via target to bypass _save validation
        task.target().save("x_valid")
        result = task.load()
        assert result == "x_valid"

    def test_load_raises_on_invalid_stored_data(self, default_in_memory_fs_target):
        task = PrefixTask(value="x_valid")
        # Save invalid data directly to target, bypassing validation
        task.target().save("no_prefix")
        with pytest.raises(ValueError, match="Expected value to start with 'x'"):
            task.load()

    def test_load_transforms_stored_data(self, default_in_memory_fs_target):
        task = ClampedIntTask(value=50)
        # Save a value exceeding the clamp directly
        task.target().save(200)
        result = task.load()
        assert result == 100

    def test_load_chained_validators(self, default_in_memory_fs_target):
        task = StripAndPrefixTask(value="x_dummy")
        # Save value with whitespace directly
        task.target().save("  x_hello  ")
        result = task.load()
        assert result == "x_hello"


# ---------------------------------------------------------------------------
# Tests: Async validation
# ---------------------------------------------------------------------------


class TestAsyncValidation:
    """Tests that validators run during async save/load."""

    def test_save_aio_validates(self, default_in_memory_fs_target):
        task = PrefixTask(value="x_async")
        asyncio.run(task._save_aio("x_async"))
        assert task.target().load() == "x_async"

    def test_save_aio_raises_on_invalid(self, default_in_memory_fs_target):
        task = PrefixTask(value="x_dummy")
        with pytest.raises(ValueError, match="Expected value to start with 'x'"):
            asyncio.run(task._save_aio("no_prefix"))

    def test_save_aio_transforms(self, default_in_memory_fs_target):
        task = ClampedIntTask(value=50)
        asyncio.run(task._save_aio(200))
        assert task.target().load() == 100

    def test_load_aio_validates(self, default_in_memory_fs_target):
        task = PrefixTask(value="x_valid")
        task.target().save("x_valid")
        result = asyncio.run(task.load_aio())
        assert result == "x_valid"

    def test_load_aio_raises_on_invalid(self, default_in_memory_fs_target):
        task = PrefixTask(value="x_dummy")
        task.target().save("no_prefix")
        with pytest.raises(ValueError, match="Expected value to start with 'x'"):
            asyncio.run(task.load_aio())

    def test_load_aio_transforms(self, default_in_memory_fs_target):
        task = ClampedIntTask(value=50)
        task.target().save(200)
        result = asyncio.run(task.load_aio())
        assert result == 100


# ---------------------------------------------------------------------------
# Tests: Generic task subclasses with validators
# ---------------------------------------------------------------------------

T = typing.TypeVar("T")


class TestGenericSubclassValidators:
    """Tests that validators work with generic Task subclasses."""

    def test_concrete_subclass_inherits_validators(self):
        class GenericValidated(Task[T], typing.Generic[T]):
            value: T  # type: ignore

            def run(self):
                self._save(self.value)

        class ConcreteValidated(GenericValidated[typing.Annotated[int, PositiveInt()]]):
            pass

        assert len(ConcreteValidated._load_validators) == 1
        assert isinstance(ConcreteValidated._load_validators[0], PositiveInt)

    def test_concrete_subclass_validates_on_save(self, default_in_memory_fs_target):
        class GenericValidated(Task[T], typing.Generic[T]):
            value: T  # type: ignore

            def run(self):
                self._save(self.value)

        class ConcreteValidated(GenericValidated[typing.Annotated[int, PositiveInt()]]):
            pass

        task = ConcreteValidated(value=-5)
        with pytest.raises(ValueError, match="Expected positive integer"):
            task.run()


# ---------------------------------------------------------------------------
# Tests: Decorator API with validators
# ---------------------------------------------------------------------------


class TestDecoratorApiValidation:
    """Tests that validators work with the @task decorator API."""

    def test_decorator_task_validates_on_run(self, default_in_memory_fs_target):
        from stardag import task

        @task
        def validated_task(
            value: str,
        ) -> typing.Annotated[str, HasPrefix("x")]:
            return value

        t = validated_task(value="x_hello")
        t.run()
        assert t.load() == "x_hello"

    def test_decorator_task_rejects_invalid(self, default_in_memory_fs_target):
        from stardag import task

        @task
        def validated_task_reject(
            value: str,
        ) -> typing.Annotated[str, HasPrefix("x")]:
            return value

        t = validated_task_reject(value="no_prefix")
        with pytest.raises(ValueError, match="Expected value to start with 'x'"):
            t.run()

    def test_decorator_task_transforms(self, default_in_memory_fs_target):
        from stardag import task

        @task
        def strip_task(value: str) -> typing.Annotated[str, StripWhitespace()]:
            return value

        t = strip_task(value="  hello  ")
        t.run()
        assert t.load() == "hello"

    def test_decorator_task_chained_validators(self, default_in_memory_fs_target):
        from stardag import task

        @task
        def chained_task(
            value: str,
        ) -> typing.Annotated[str, StripWhitespace(), HasPrefix("x")]:
            return value

        t = chained_task(value="  x_hello  ")
        t.run()
        assert t.load() == "x_hello"

    def test_decorator_task_chained_validators_rejects(
        self, default_in_memory_fs_target
    ):
        from stardag import task

        @task
        def chained_task_reject(
            value: str,
        ) -> typing.Annotated[str, StripWhitespace(), HasPrefix("x")]:
            return value

        t = chained_task_reject(value="  no_prefix  ")
        with pytest.raises(ValueError, match="Expected value to start with 'x'"):
            t.run()

    def test_decorator_task_load_validates(self, default_in_memory_fs_target):
        """Validates that load() also runs validators for @task functions."""
        from stardag import task

        @task
        def load_validated_task(
            value: int,
        ) -> typing.Annotated[int, ClampMax(100)]:
            return value

        t = load_validated_task(value=50)
        # Write a value exceeding the clamp directly to the target
        t.target().save(200)
        assert t.load() == 100

    def test_decorator_async_task_validates(self, default_in_memory_fs_target):
        from stardag import task

        @task
        async def async_validated(
            value: str,
        ) -> typing.Annotated[str, HasPrefix("x")]:
            return value

        t = async_validated(value="x_async")
        t.run()
        assert t.load() == "x_async"

    def test_decorator_async_task_rejects(self, default_in_memory_fs_target):
        from stardag import task

        @task
        async def async_validated_reject(
            value: str,
        ) -> typing.Annotated[str, HasPrefix("x")]:
            return value

        t = async_validated_reject(value="no_prefix")
        with pytest.raises(ValueError, match="Expected value to start with 'x'"):
            t.run()

    def test_decorator_task_with_dependency(self, default_in_memory_fs_target):
        """Validates that validators work when a @task has upstream dependencies."""
        from stardag import Depends, task

        @task
        def upstream(value: str) -> str:
            return value

        @task
        def downstream(
            data: Depends[str],
        ) -> typing.Annotated[str, HasPrefix("x")]:
            return data

        up = upstream(value="x_from_upstream")
        down = downstream(data=up)
        up.run()
        down.run()
        assert down.load() == "x_from_upstream"

    def test_decorator_task_with_dependency_rejects(self, default_in_memory_fs_target):
        from stardag import Depends, task

        @task
        def upstream_bad(value: str) -> str:
            return value

        @task
        def downstream_bad(
            data: Depends[str],
        ) -> typing.Annotated[str, HasPrefix("x")]:
            return data

        up = upstream_bad(value="no_prefix")
        down = downstream_bad(data=up)
        up.run()
        with pytest.raises(ValueError, match="Expected value to start with 'x'"):
            down.run()


# ---------------------------------------------------------------------------
# Tests: Unit tests for get_validators and run_validators
# ---------------------------------------------------------------------------


class TestGetValidators:
    """Unit tests for the get_validators helper."""

    def test_returns_empty_for_plain_type(self):
        from stardag._core.validate import get_validators

        assert get_validators(str) == ()

    def test_returns_empty_for_annotated_without_validators(self):
        from stardag._core.validate import get_validators

        assert get_validators(typing.Annotated[str, "some_metadata"]) == ()

    def test_returns_validators_in_order(self):
        from stardag._core.validate import get_validators

        validators = get_validators(
            typing.Annotated[str, StripWhitespace(), HasPrefix("x")]
        )
        assert len(validators) == 2
        assert isinstance(validators[0], StripWhitespace)
        assert isinstance(validators[1], HasPrefix)

    def test_skips_non_validator_metadata(self):
        from stardag._core.validate import get_validators

        validators = get_validators(
            typing.Annotated[str, "metadata", HasPrefix("x"), 42]
        )
        assert len(validators) == 1
        assert isinstance(validators[0], HasPrefix)


class TestRunValidators:
    """Unit tests for the run_validators helper."""

    def test_empty_validators_returns_value(self):
        from stardag._core.validate import run_validators

        assert run_validators((), "hello") == "hello"

    def test_single_validator(self):
        from stardag._core.validate import run_validators

        result = run_validators((StripWhitespace(),), "  hello  ")
        assert result == "hello"

    def test_chained_validators(self):
        from stardag._core.validate import run_validators

        result = run_validators(
            (StripWhitespace(), HasPrefix("h")),
            "  hello  ",
        )
        assert result == "hello"

    def test_validator_raises(self):
        from stardag._core.validate import run_validators

        with pytest.raises(ValueError, match="Expected value to start with 'x'"):
            run_validators((HasPrefix("x"),), "no_prefix")

    def test_transform_then_validate(self):
        from stardag._core.validate import run_validators

        result = run_validators(
            (ClampMax(10), PositiveInt()),
            15,
        )
        assert result == 10
