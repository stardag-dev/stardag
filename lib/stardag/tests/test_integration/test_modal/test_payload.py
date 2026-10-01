"""A task crosses a Modal call as its instance body (``_payload``).

The scenario being guarded: a task is serialized under one version of its
classes (deployment N, or a trigger's older local checkout) and run under
the next, which added a defaulted field to a nested parameter model. A
pickle restores the old ``__dict__`` without the field; the payload is
rehydrated in compat mode, so the field takes its default.

An older sender is simulated by dropping the new field from the body: that
is exactly the body a class without the field produces.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

import pytest
from pydantic import field_serializer

try:
    import modal  # noqa: F401
except ImportError:
    pytest.skip("Skipping modal tests (import not available)", allow_module_level=True)

import stardag as sd
from stardag import TaskRehydrationError
from stardag.integration.modal import FunctionSettings, StardagApp
from stardag.integration.modal import _payload
from stardag.integration.modal._payload import (
    PAYLOAD_KEY,
    from_task_payload,
    from_task_payloads,
    is_task_payload,
    to_task_payload,
    to_task_payloads,
)
from tests.test_integration.test_modal._app_helpers import (  # noqa: F401
    _finalize_capturing_functions,
    _make_image,
    _mock_secret_hydrate,
    _stub_modal_concurrent,
)


class PayloadParams(sd.StardagBaseModel):
    a: int = 1
    # Added "in deployment N+1": defaulted, with a compat default, so the
    # task id of every existing instance is unchanged.
    b: Annotated[int, sd.StardagField(0)] = 0


class PayloadTask(sd.Task[int]):
    p: PayloadParams = PayloadParams()

    def run(self) -> None:
        self._save(self.p.a + self.p.b)


class PayloadParent(sd.Task[int]):
    child: sd.TaskLoads[int]

    def requires(self):
        return self.child

    def run(self) -> None:
        self._save(0)


@sd.task(name="PayloadRange")
def payload_range(limit: int) -> list[int]:
    """A decorator task: its class is bound to ``payload_range``, not to its
    own name ``PayloadRange``."""
    return list(range(limit))


class DriftingNoteTask(sd.Task[int]):
    """A non-significant field whose serialization is not a fixed point:
    every round trip appends a "!". The task id survives; the value the
    worker would see does not."""

    note: Annotated[str, sd.StardagField(significant=False)] = ""

    @field_serializer("note")
    def _drift(self, value: str) -> str:
        return value + "!"

    def run(self) -> None:
        self._save(0)


def _payload_of(task: sd.BaseTask) -> dict[str, Any]:
    """``to_task_payload(task)``, asserted to be a payload (not by value)."""
    payload = to_task_payload(task)
    assert isinstance(payload, dict)
    return payload


def _as_older_sender(payload: dict[str, Any]) -> dict[str, Any]:
    """``payload`` as a sender without ``PayloadParams.b`` would build it."""
    body = dict(payload["body"])
    body["p"] = {k: v for k, v in body["p"].items() if k != "b"}
    return {**payload, "body": body}


@pytest.fixture(autouse=True)
def _reset_by_value_warnings():
    _payload._warned_by_value.clear()
    yield
    _payload._warned_by_value.clear()


class TestToAndFromPayload:
    def test_round_trip_equals_the_task(self):
        task = PayloadTask(p=PayloadParams(a=3, b=4))

        payload = _payload_of(task)

        assert is_task_payload(payload)
        assert payload[PAYLOAD_KEY] == 1
        assert payload["task_id"] == str(task.id)
        assert payload["modules"] == [__name__]
        assert from_task_payload(payload) == task

    def test_nested_task_classes_are_named(self):
        parent = PayloadParent(child=PayloadTask())

        payload = _payload_of(parent)

        assert payload["modules"] == [__name__]
        assert from_task_payload(payload) == parent

    def test_a_field_added_since_the_sender_takes_its_default(self):
        """The bug: under a pickle, ``p.b`` would be missing entirely."""
        task = PayloadTask(p=PayloadParams(a=3))
        older = _as_older_sender(_payload_of(task))
        assert "b" not in older["body"]["p"]

        received = from_task_payload(older)

        assert isinstance(received, PayloadTask)
        assert received.p.b == 0
        assert received.id == task.id

    def test_a_significant_change_fails_the_id_check(self):
        """A body that no longer hashes to the sent id is refused, never run
        as a different task."""
        payload = _payload_of(PayloadTask(p=PayloadParams(a=3)))
        payload["body"]["p"]["a"] = 5

        with pytest.raises(TaskRehydrationError, match="does not match"):
            from_task_payload(payload)

    def test_a_task_passes_through_unchanged(self):
        """An older sender (or this one's fallback) sends the object."""
        task = PayloadTask()
        assert from_task_payload(task) is task

    def test_an_unknown_version_is_refused(self):
        payload = _payload_of(PayloadTask())
        payload[PAYLOAD_KEY] = 2

        with pytest.raises(TaskRehydrationError, match="version 2"):
            from_task_payload(payload)

    def test_an_unimportable_module_is_refused(self):
        payload = _payload_of(PayloadTask())
        payload["modules"] = ["stardag_no_such_module_sta124"]

        with pytest.raises(TaskRehydrationError, match="Cannot import"):
            from_task_payload(payload)

    def test_a_decorator_task_is_sent_as_a_payload(self):
        """The common case: a ``@sd.task`` class is reachable by import under
        the function's name, so it is protected like a class-defined task."""
        task = payload_range(limit=3)

        payload = _payload_of(task)

        assert payload["modules"] == [__name__]
        assert from_task_payload(payload) == task

    def test_roots_keep_their_shape(self):
        one, two = PayloadTask(p=PayloadParams(a=1)), PayloadTask(p=PayloadParams(a=2))

        single = to_task_payloads(one)
        many = to_task_payloads([one, two])

        assert is_task_payload(single)
        assert isinstance(many, list) and len(many) == 2
        assert from_task_payloads(single) == one
        assert from_task_payloads(many) == [one, two]


class TestByValueFallback:
    def test_a_lossy_non_significant_field_goes_by_value(self, caplog):
        """The id check alone would pass this task; the full-body fixed
        point does not, so it keeps the pickle rather than arriving changed."""
        caplog.set_level(logging.WARNING, logger=_payload.__name__)
        task = DriftingNoteTask(note="a")

        sent = to_task_payload(task)

        assert sent is task
        (warning,) = [r for r in caplog.records if "by value" in r.getMessage()]
        assert "round trip is not stable" in warning.getMessage()

    def test_a_local_class_goes_by_value_with_one_warning(self, caplog):
        """A class no receiver can import by name cannot be rehydrated
        there, so it is pickled as before — and said so, once per class."""

        class LocalTask(sd.Task[int]):
            x: int = 0

            def run(self) -> None:
                self._save(self.x)

        caplog.set_level(logging.WARNING, logger=_payload.__name__)

        first = to_task_payload(LocalTask(x=1))
        second = to_task_payload(LocalTask(x=2))

        assert isinstance(first, LocalTask) and isinstance(second, LocalTask)
        warnings = [r for r in caplog.records if "by value" in r.getMessage()]
        assert len(warnings) == 1
        assert "not importable by reference" in warnings[0].getMessage()

    def test_a_decorator_task_defined_in_a_function_goes_by_value(self):
        """Not reachable by import: the receiver could never resolve it."""

        @sd.task(name="LocalRange")
        def local_range(limit: int) -> list[int]:
            return list(range(limit))

        task = local_range(limit=2)
        assert to_task_payload(task) is task


def _app(**kwargs) -> StardagApp:
    return StardagApp(
        "test-payload-app",
        builder_settings=FunctionSettings(image=_make_image()),
        worker_settings={"default": FunctionSettings(image=_make_image())},
        **kwargs,
    )


class TestDeployedReceivers:
    """The deployed wrappers rehydrate what they receive before user code
    sees it."""

    def test_worker_runs_the_rehydrated_task(self):
        received = []
        functions = _finalize_capturing_functions(
            _app(run_function=lambda task: received.append(task))
        )
        task = PayloadTask(p=PayloadParams(a=3))

        functions["worker_default"](_as_older_sender(_payload_of(task)))

        (got,) = received
        assert isinstance(got, PayloadTask)
        assert got.p.b == 0
        assert got == task

    def test_build_rehydrates_its_roots(self):
        received = []

        def build_function(tasks, worker_selector, app_name, build_kwargs=None):
            received.append(tasks)

        functions = _finalize_capturing_functions(_app(build_function=build_function))
        task = PayloadTask(p=PayloadParams(a=3))

        functions["build"](
            [_as_older_sender(_payload_of(task))],
            lambda t: "default",
            "test-payload-app",
        )

        assert received == [[task]]
        assert received[0][0].p.b == 0
