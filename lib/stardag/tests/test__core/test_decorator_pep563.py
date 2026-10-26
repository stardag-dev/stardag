"""``@task`` in a module using PEP 563 deferred annotations.

The future import below is the point of this module: every annotation in it is
a string at runtime, which is what the decorator has to resolve.
"""

from __future__ import annotations

import warnings

import pytest
from pydantic import ValidationError

import stardag as sd
from stardag._core.base_task import LoadableTask


@sd.task(name="Pep563Range")
def get_range(limit: int, salt: str) -> list[int]:
    del salt
    return list(range(limit))


@sd.task(name="Pep563Square")
def square(values: sd.Depends[list[int]], offset: int) -> list[int]:
    return [(v + offset) ** 2 for v in values]


@sd.task(name="Pep563Upper")
def upper(value: sd.Depends[str]) -> str:
    return value.upper()


class SuspendingParent(sd.Task[list[int]]):
    salt: str

    def requires(self):
        return get_range(limit=2, salt=self.salt)

    def run(self):
        self._save(self.requires().load())


@pytest.fixture(autouse=True)
def _warn_on_generic_mismatch(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("STARDAG_POLYMORPHIC_ON_GENERIC_TYPE_MISMATCH", raising=False)


def test_depends_annotation_is_resolved():
    assert square.model_fields["values"].annotation is LoadableTask[list[int]]


def test_matching_class_task_into_depends_does_not_warn():
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        task = square(values=SuspendingParent(salt="abc"), offset=5)
    assert isinstance(getattr(task, "values"), SuspendingParent)


def test_genuine_mismatch_still_warns():
    with pytest.warns(UserWarning, match="Generic type mismatch at position 0"):
        upper(value=SuspendingParent(salt="abc"))


def test_depends_rejects_a_raw_value():
    with pytest.raises(ValidationError):
        square(values=[1, 2], offset=5)


def test_unresolvable_local_annotation_still_decorates():
    class Local:
        pass

    @sd.task(name="Pep563LocalAnnotation")
    def uses_local(x: int) -> Local:
        return Local()

    assert uses_local.__name__ == "Pep563LocalAnnotation"
