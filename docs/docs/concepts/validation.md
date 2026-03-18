# Validation

Stardag supports automatic validation of task data on save and load via **load validators**. Validators are attached to the `LoadedT` type parameter using `typing.Annotated` and run automatically whenever data passes through `Task._save()`, `Task._save_aio()`, `Task.load()`, or `Task.load_aio()`.

This follows the same pattern as Pydantic's `Annotated` validators — place validator instances in the type metadata, and they execute automatically.

## Defining a Validator

Subclass `LoadValidator[T]` and implement the `validate` method:

```python
import stardag as sd


class NonEmpty(sd.LoadValidator[str]):
    """Reject empty strings."""

    def validate(self, value: str) -> str:
        if not value:
            raise ValueError("Value must not be empty")
        return value
```

The `validate` method:

- **Receives** the value being saved or loaded
- **Returns** the (possibly transformed) value
- **Raises** an exception to reject invalid data

## Using Validators

Attach validator instances to your task's type parameter via `typing.Annotated`:

```python
import typing

import stardag as sd


class HasPrefix(sd.LoadValidator[str]):
    def __init__(self, prefix: str) -> None:
        self.prefix = prefix

    def validate(self, value: str) -> str:
        if not value.startswith(self.prefix):
            raise ValueError(
                f"Expected value to start with {self.prefix!r}, got {value!r}"
            )
        return value


class MyTask(sd.Task[typing.Annotated[str, HasPrefix("x")]]):
    value: str

    def run(self):
        self._save(self.value)
```

When `MyTask._save()` is called, the `HasPrefix` validator runs before the data is written. When `MyTask.load()` is called, the validator runs after the data is read.

```python
task = MyTask(value="x_hello")
task.run()  # validates, then saves

task.load()  # loads, then validates
```

## Transforming Validators

Validators can transform data, not just reject it. This is useful for normalization:

```python
class StripWhitespace(sd.LoadValidator[str]):
    def validate(self, value: str) -> str:
        return value.strip()


class ClampMax(sd.LoadValidator[int]):
    def __init__(self, max_value: int) -> None:
        self.max_value = max_value

    def validate(self, value: int) -> int:
        return min(value, self.max_value)
```

## Multiple Validators

You can chain multiple validators in a single `Annotated` type. They execute **in order**, left to right:

```python
class CleanTask(sd.Task[typing.Annotated[str, StripWhitespace(), HasPrefix("x")]]):
    value: str

    def run(self):
        self._save(self.value)
```

Here, `StripWhitespace` runs first (removing whitespace), then `HasPrefix` validates the stripped result.

## With the Decorator API

Validators work with the `@sd.task` decorator too — use `Annotated` in the return type:

```python
@sd.task
def clean_text(raw: str) -> typing.Annotated[str, StripWhitespace()]:
    return raw
```

## Attribute-Based Validators

If subclassing `LoadValidator` is not possible — for example, because your validator must inherit from another class that conflicts in the MRO — you can use the **attribute-based** discovery escape hatch. Set the class attribute `stardag_load_validator = True` and implement a `validate` method:

```python
class MyExternalBase:
    """Some third-party base class."""

    ...


class SuffixValidator(MyExternalBase):
    stardag_load_validator = True

    def __init__(self, suffix: str) -> None:
        self.suffix = suffix

    def validate(self, value: str) -> str:
        if not value.endswith(self.suffix):
            raise ValueError(f"Expected suffix {self.suffix!r}")
        return value


class MyTask(sd.Task[typing.Annotated[str, SuffixValidator(".json")]]):
    value: str

    def run(self):
        self._save(self.value)
```

Attribute-based validators can be freely mixed with `LoadValidator` subclasses in the same `Annotated` type — they chain in order just the same.

!!! note
Both `stardag_load_validator = True` **and** a callable `validate` method are required. Objects with only one of the two are silently skipped.

## When Validators Run

| Method            | Validation order              |
| ----------------- | ----------------------------- |
| `_save(data)`     | validate → serialize → write  |
| `_save_aio(data)` | validate → serialize → write  |
| `load()`          | read → deserialize → validate |
| `load_aio()`      | read → deserialize → validate |

Note that `target().save()` and `target().load()` **bypass** validators — they operate at the target/serializer level. Use `task._save()` and `task.load()` to get validation.

## API Reference

::: stardag.LoadValidator
options:
show_root_heading: true
show_source: true
