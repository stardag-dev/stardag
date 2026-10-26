"""The rehydration pre-flight's runtime-state policy (STA-126).

A reactive tick runs each task as rebuilt from its instance body, which
carries neither private attributes nor fields declared ``exclude=True``. The
pre-flight compares each task with its rebuild and splits by what it finds:

- an excluded field rebuilt with a *decidably* different value (both plain
  data, unequal) refuses the build;
- an excluded field whose difference cannot be decided warns;
- a private attribute never refuses: a decidable difference warns, an
  undecidable one (a lock, a ``uuid4``) is silent.
"""

from __future__ import annotations

import collections
import dataclasses
import datetime
import logging
import math
import threading
import typing
import uuid

import pytest
from pydantic import Field, PrivateAttr

import stardag as sd
from stardag.base_model import StardagField
from stardag.build import _task_modules
from stardag.build._task_modules import (
    _reset_import_state_for_tests,
    plan_rehydration,
)

sd.auto_namespace(__name__)

PATTERNS = [__name__]


@pytest.fixture(autouse=True)
def _fresh_warnings():
    """Warnings are once per class and attribute per process."""
    _reset_import_state_for_tests()
    yield
    _reset_import_state_for_tests()


def _state_warnings(caplog) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == _task_modules.__name__ and r.levelno == logging.WARNING
    ]


def _refusal(plan) -> str:
    assert plan.reconstructable == ()
    ((_, reason),) = plan.unreconstructable
    return reason


# =============================================================================
# Task and model classes (module level: a tick imports them by name)
# =============================================================================


class StatefulTask(sd.Task[int]):
    x: int = 0
    handle: typing.Any = Field(default=None, exclude=True)
    _state: str | None = PrivateAttr(default=None)

    def run(self) -> None:
        self._save(self.x)


class ExcludedParams(sd.StardagBaseModel):
    a: int = 1
    note: str | None = Field(default=None, exclude=True)


class PrivateParams(sd.StardagBaseModel):
    a: int = 1
    _cache: dict | None = PrivateAttr(default=None)


class LockParams(sd.StardagBaseModel):
    a: int = 1
    _lock: typing.Any = PrivateAttr(default_factory=threading.Lock)


class ParamsTask(sd.Task[int]):
    excluded: ExcludedParams = ExcludedParams()
    private: PrivateParams = PrivateParams()
    locked: LockParams = LockParams()

    def run(self) -> None:
        self._save(1)


class DequeTask(sd.Task[int]):
    params: collections.deque[ExcludedParams] = Field(
        default_factory=lambda: collections.deque([ExcludedParams()])
    )

    def run(self) -> None:
        self._save(len(self.params))


class AnyFieldTask(sd.Task[int]):
    """A model in an ``Any`` field is rebuilt as a plain dict."""

    blob: typing.Any = None

    def run(self) -> None:
        self._save(1)


class ParentTask(sd.Task[int]):
    upstream: sd.TaskLoads[int]

    def run(self) -> None:
        self._save(self.upstream.load() + 1)


class NormalisedExcludedTask(sd.Task[int]):
    """``validate_default`` (on for every stardag model) turns ``"3"`` into
    ``3``, in the original and in the rebuild alike."""

    count: int = Field(default="3", exclude=True)  # pyright: ignore[reportAssignmentType]

    def run(self) -> None:
        self._save(self.count)


class CompatExcludedTask(sd.Task[int]):
    """Rehydration fills the missing key with the ``compat_default``."""

    level: typing.Annotated[int, StardagField(compat_default=0)] = Field(
        default=5, exclude=True
    )

    def run(self) -> None:
        self._save(self.level)


class DerivedPrivateTask(sd.Task[int]):
    x: int = 0
    _double: int = PrivateAttr(default=0)

    def model_post_init(self, context: typing.Any) -> None:
        super().model_post_init(context)
        self._double = 2 * self.x

    def run(self) -> None:
        self._save(self._double)


class MemoisingTask(sd.Task[int]):
    """Caches a path in ``requires()``: discovery fills it on the very object
    the pre-flight checks, and the tick derives it again from ``x``."""

    x: int = 0
    _out: str | None = PrivateAttr(default=None)

    def requires(self):
        self._out = f"/data/{self.x}"
        return None

    def run(self) -> None:
        self._save(self.x)


class _RaisingEq:
    """A value whose ``==`` raises, standing in for a numpy array."""

    def __eq__(self, other: object) -> bool:
        raise ValueError("ambiguous truth value")

    __hash__ = object.__hash__


class FactoryDefaultsTask(sd.Task[int]):
    """Untouched private attributes whose defaults have no value equality,
    or raise on comparison, or are NaN. All ran fine reactively before."""

    _lock: typing.Any = PrivateAttr(default_factory=threading.Lock)
    _sentinel: typing.Any = PrivateAttr(default_factory=object)
    _uid: uuid.UUID = PrivateAttr(default_factory=uuid.uuid4)
    _at: datetime.datetime = PrivateAttr(default_factory=datetime.datetime.now)
    _raising: typing.Any = PrivateAttr(default_factory=_RaisingEq)
    _nan: float = PrivateAttr(default=math.nan)

    def run(self) -> None:
        self._save(1)


class NumpyPrivateTask(sd.Task[int]):
    _arr: typing.Any = PrivateAttr(default=None)

    def model_post_init(self, context: typing.Any) -> None:
        super().model_post_init(context)
        import numpy as np

        self._arr = np.zeros(3)

    def run(self) -> None:
        self._save(1)


class ExcludedOpaqueTask(sd.Task[int]):
    """Excluded fields whose defaults have no value equality, or are NaN."""

    client: typing.Any = Field(default_factory=object, exclude=True)
    lock: typing.Any = Field(default_factory=threading.Lock, exclude=True)
    ratio: float = Field(default=math.nan, exclude=True)

    def run(self) -> None:
        self._save(1)


@dataclasses.dataclass
class Holder:
    """Equality ignores ``params``."""

    params: PrivateParams = dataclasses.field(
        default_factory=PrivateParams, compare=False
    )


class ExcludedHolderTask(sd.Task[int]):
    holder: Holder = Field(default_factory=Holder, exclude=True)

    def run(self) -> None:
        self._save(1)


# =============================================================================
# Refused: a decidable difference in an excluded field
# =============================================================================


class TestRefused:
    def test_a_set_plain_excluded_field_is_refused(self):
        task = StatefulTask(handle="live-handle")

        reason = _refusal(plan_rehydration([task], PATTERNS))

        assert "StatefulTask.handle is excluded from the body" in reason
        assert "make it a serialized field" in reason
        assert "compat_default" not in reason
        error = plan_rehydration([task], PATTERNS).error(PATTERNS)
        assert error is not None and f"{__name__}.StatefulTask" in error
        assert "Add [" not in error

    def test_in_a_nested_task(self):
        parent = ParentTask(upstream=StatefulTask(x=1, handle="h"))

        plan = plan_rehydration([parent], PATTERNS)

        assert "StatefulTask.handle is excluded" in _refusal(plan)
        assert plan.unreconstructable[0][0] is parent

    def test_in_a_nested_parameter_model(self):
        task = ParamsTask(excluded=ExcludedParams(note="set"))
        assert "ExcludedParams.note is excluded" in _refusal(
            plan_rehydration([task], PATTERNS)
        )

    def test_in_a_model_inside_a_deque(self):
        task = DequeTask(params=collections.deque([ExcludedParams(note="set")]))
        assert "ExcludedParams.note is excluded" in _refusal(
            plan_rehydration([task], PATTERNS)
        )

    def test_in_a_model_rebuilt_as_a_dict(self):
        """No model on the rebuilt side: the declared default stands in."""
        task = AnyFieldTask(blob=ExcludedParams(note="set"))
        assert "ExcludedParams.note is excluded" in _refusal(
            plan_rehydration([task], PATTERNS)
        )

    def test_an_untouched_excluded_field_with_a_compat_default(self):
        """The tick really does get the compat_default (0), not 5."""
        reason = _refusal(plan_rehydration([CompatExcludedTask()], PATTERNS))
        assert "CompatExcludedTask.level is excluded" in reason
        assert "drop the compat_default" in reason


# =============================================================================
# Not refused
# =============================================================================


class TestNotRefused:
    def test_state_at_its_defaults(self, caplog):
        task = StatefulTask(x=3)
        assert plan_rehydration([task], PATTERNS).reconstructable == (task,)
        assert _state_warnings(caplog) == []

    def test_an_excluded_default_normalised_by_validation(self, caplog):
        task = NormalisedExcludedTask()
        assert task.count == 3
        assert plan_rehydration([task], PATTERNS).reconstructable == (task,)
        assert _state_warnings(caplog) == []

    def test_a_private_attribute_derived_from_fields(self, caplog):
        task = DerivedPrivateTask(x=4)
        assert plan_rehydration([task], PATTERNS).reconstructable == (task,)
        assert _state_warnings(caplog) == []

    def test_a_plain_private_attribute_warns_once_per_class_and_attribute(self, caplog):
        first, second = StatefulTask(x=1), StatefulTask(x=2)
        first._state = "a"
        second._state = "b"

        plan = plan_rehydration([first, second], PATTERNS)

        assert plan.reconstructable == (first, second)
        (warning,) = _state_warnings(caplog)
        assert f"{__name__}.StatefulTask._state" in warning
        assert "requires(), output() or run()" in warning

    @pytest.mark.parametrize("where", ["nested_task", "param_model", "any_dict"])
    def test_a_nested_plain_private_attribute_warns(self, caplog, where):
        params = PrivateParams(a=2)
        params._cache = {"k": 1}
        if where == "nested_task":
            inner = StatefulTask(x=1)
            inner._state = "set"
            task: sd.Task[int] = ParentTask(upstream=inner)
            attr = "StatefulTask._state"
        elif where == "param_model":
            task = ParamsTask(private=params)
            attr = "PrivateParams._cache"
        else:
            task = AnyFieldTask(blob=params)
            attr = "PrivateParams._cache"

        assert plan_rehydration([task], PATTERNS).reconstructable == (task,)
        (warning,) = _state_warnings(caplog)
        assert attr in warning

    def test_a_value_memoised_in_requires(self, caplog):
        task = MemoisingTask(x=7)
        task.requires()  # what discovery does before the pre-flight
        assert task._out == "/data/7"

        assert plan_rehydration([task], PATTERNS).reconstructable == (task,)
        (warning,) = _state_warnings(caplog)
        assert "MemoisingTask._out" in warning

    def test_untouched_factory_defaults_are_silent(self, caplog):
        """A lock, object(), uuid4, datetime.now, a value whose == raises,
        and NaN: no value equality to decide on, so nothing is said."""
        task = FactoryDefaultsTask()
        assert plan_rehydration([task], PATTERNS).reconstructable == (task,)
        assert _state_warnings(caplog) == []

    def test_a_numpy_array_private_attribute_is_silent(self, caplog):
        pytest.importorskip("numpy")
        task = NumpyPrivateTask()
        assert plan_rehydration([task], PATTERNS).reconstructable == (task,)
        assert _state_warnings(caplog) == []

    def test_a_nested_parameter_model_holding_a_lock_is_silent(self, caplog):
        task = ParamsTask(locked=LockParams(a=3))
        assert plan_rehydration([task], PATTERNS).reconstructable == (task,)
        assert _state_warnings(caplog) == []

    def test_opaque_excluded_fields_warn_and_nan_passes(self, caplog):
        task = ExcludedOpaqueTask()

        assert plan_rehydration([task], PATTERNS).reconstructable == (task,)

        warnings = _state_warnings(caplog)
        assert len(warnings) == 2
        assert any("ExcludedOpaqueTask.client" in w for w in warnings)
        assert any("ExcludedOpaqueTask.lock" in w for w in warnings)
        assert not any("ratio" in w for w in warnings)

    def test_a_cyclic_excluded_value_warns_rather_than_crashing(self, caplog):
        cyclic: list[typing.Any] = []
        cyclic.append(cyclic)
        task = StatefulTask(handle=cyclic)

        assert plan_rehydration([task], PATTERNS).reconstructable == (task,)
        (warning,) = _state_warnings(caplog)
        assert "StatefulTask.handle" in warning

    def test_state_nested_in_an_excluded_field_equal_to_its_default(self, caplog):
        """The walk enters excluded fields after comparing them."""
        task = ExcludedHolderTask()
        task.holder.params._cache = {"k": 1}

        assert plan_rehydration([task], PATTERNS).reconstructable == (task,)
        (warning,) = _state_warnings(caplog)
        assert "PrivateParams._cache" in warning

    def test_a_failing_detector_warns_and_passes(self, caplog, monkeypatch):
        def broken(original, rebuilt):
            raise RecursionError("too deep")

        monkeypatch.setattr(_task_modules, "state_outside_body", broken)
        task = StatefulTask(handle="would-be-refused")

        assert plan_rehydration([task], PATTERNS).reconstructable == (task,)
        (warning,) = _state_warnings(caplog)
        assert "Could not check" in warning and "too deep" in warning


class TestOverhead:
    def test_a_class_without_state_or_model_fields_is_not_walked(self):
        """The common task pays a cached lookup, nothing more."""
        from stardag._core.instance import _class_plan

        class Plain(sd.Task[int]):
            name: str
            values: frozenset[str] = frozenset()
            ratio: float | None = None

            def run(self) -> None:
                self._save(1)

        # Task._load_validators is stardag's own, not task state.
        assert _class_plan(Plain).inert
        assert _class_plan(StatefulTask).private == ("_state",)
        assert _class_plan(StatefulTask).excluded == ("handle",)
        # A field that could hold a model is walked.
        assert "upstream" in _class_plan(ParentTask).walked
