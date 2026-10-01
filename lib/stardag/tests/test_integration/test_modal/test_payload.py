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

import importlib
import json
from collections import UserList
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Annotated, Any
from unittest.mock import patch

import pytest
from pydantic import Field, PrivateAttr, field_serializer

try:
    import modal  # noqa: F401
except ImportError:
    pytest.skip("Skipping modal tests (import not available)", allow_module_level=True)

import stardag as sd
from stardag import TaskRehydrationError
from stardag.build import SettingsError
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


class StatefulTask(sd.Task[int]):
    """Carries state its instance body does not: a private attribute and a
    field excluded from the dump."""

    x: int = 0
    handle: Any = Field(default=None, exclude=True)
    _state: str | None = PrivateAttr(default=None)

    def run(self) -> None:
        self._save(self.x)


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

    def test_a_module_gone_from_the_receiver_is_not_fatal_by_itself(self):
        """A module the sender named may be gone from the new deployment (a
        field removed together with its type): rehydration decides whether
        anything still needed is missing."""
        payload = _payload_of(PayloadTask())
        payload["modules"].append("stardag_no_such_module_sta124")

        assert from_task_payload(payload) == PayloadTask()

    def test_a_class_from_a_module_that_failed_to_import_is_refused(self):
        """A module can raise after registering a class; resolving that class
        would run the task with the module half-initialized."""
        payload = _payload_of(PayloadTask())
        real_import = importlib.import_module

        def failing_import(name, *args, **kwargs):
            if name == __name__:
                raise RuntimeError("failed after registering its classes")
            return real_import(name, *args, **kwargs)

        with patch.object(_payload.importlib, "import_module", failing_import):
            with pytest.raises(TaskRehydrationError, match="failed to import"):
                from_task_payload(payload)

    def test_a_missing_class_names_the_failed_import(self):
        payload = _payload_of(PayloadTask())
        payload["modules"].append("stardag_no_such_module_sta124")
        payload["body"]["__name"] = "NoSuchTaskSta124"

        with pytest.raises(TaskRehydrationError, match="stardag_no_such_module_sta124"):
            from_task_payload(payload)

    def test_a_decorator_task_is_sent_as_a_payload(self):
        """The common case: a ``@sd.task`` class is reachable by import under
        the function's name, so it is protected like a class-defined task."""
        task = payload_range(limit=3)

        payload = _payload_of(task)

        assert payload["modules"] == [__name__]
        assert from_task_payload(payload) == task

    def test_a_task_nested_in_a_dataclass_names_its_module(self):
        """The upstream sits inside a dataclass, which a pydantic-only walk
        would not enter: its module must still be named, or a receiver that
        has not imported it cannot resolve it."""
        from stardag.utils.testing.payload_root import (
            DataclassRoot,
            Holder,
        )
        from stardag.utils.testing.payload_upstream import (
            PkgUpstream,
        )

        task = DataclassRoot(holder=Holder(upstream=PkgUpstream(x=2)))

        payload = _payload_of(task)

        assert "stardag.utils.testing.payload_upstream" in (payload["modules"])
        assert from_task_payload(payload) == task

    def test_a_fresh_receiver_rehydrates_from_the_named_modules_alone(self):
        """The receiver's actual situation: a process that has imported none
        of the task's modules. Whatever the payload names must be enough."""
        from stardag.utils.testing.payload_root import (
            DataclassRoot,
            Holder,
        )
        from stardag.utils.testing.payload_upstream import (
            PkgUpstream,
        )

        task = DataclassRoot(holder=Holder(upstream=PkgUpstream(x=2)))
        payload = _payload_of(task)
        receiver = (
            "import json, sys\n"
            "from stardag.integration.modal._payload import from_task_payload\n"
            "task = from_task_payload(json.loads(sys.stdin.read()))\n"
            "print(task.id)\n"
        )

        result = subprocess.run(
            [sys.executable, "-c", receiver],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            cwd=Path(__file__).resolve().parents[3],
            env={**os.environ, "STARDAG_NO_REGISTRY": "1"},
        )

        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == str(task.id)

    def test_roots_keep_their_shape(self):
        one, two = PayloadTask(p=PayloadParams(a=1)), PayloadTask(p=PayloadParams(a=2))

        single = to_task_payloads(one)
        many = to_task_payloads([one, two])

        assert is_task_payload(single)
        assert isinstance(many, list) and len(many) == 2
        assert from_task_payloads(single) == one
        assert from_task_payloads(many) == [one, two]

    def test_any_iterable_of_roots_is_sent_as_payloads(self):
        """Not only lists and tuples: a ``UserList`` (or a generator) of roots
        is roots, not one task to send by value."""
        roots = UserList([PayloadTask(p=PayloadParams(a=1)), PayloadTask()])

        sent = to_task_payloads(roots)

        assert isinstance(sent, list)
        assert all(is_task_payload(t) for t in sent)
        assert from_task_payloads(UserList(sent)) == list(roots)


class TestByValueFallback:
    def test_runtime_state_goes_by_value(self):
        """A pickle carried private attributes and excluded fields; a payload
        cannot, so a task holding either keeps the pickle."""
        with_private = StatefulTask()
        with_private._state = "runtime"
        with_excluded = StatefulTask(handle="live-handle")

        assert to_task_payload(with_private) is with_private
        assert to_task_payload(with_excluded) is with_excluded
        # At their defaults, there is nothing to lose.
        assert is_task_payload(to_task_payload(StatefulTask()))

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

    def test_a_refused_payload_is_reported_before_the_worker_raises(self):
        """A refusal happens before the run function (and its reporter), so
        the wrapper records the failure itself; otherwise the claim would
        just lapse, with nothing on record."""
        functions = _finalize_capturing_functions(_app(run_function=lambda t: None))
        payload = _payload_of(PayloadTask(p=PayloadParams(a=3)))
        payload["body"]["p"]["a"] = 5  # no longer hashes to the sent id
        env = {"STARDAG_BUILD_ID": "b"}

        with patch(
            "stardag.integration.modal._functions.report_unreadable_task"
        ) as report:
            with pytest.raises(TaskRehydrationError):
                functions["worker_default"](payload, env_overrides=env)

        ((task_id, env_overrides, error), _) = report.call_args
        assert task_id == payload["task_id"]
        assert env_overrides == env
        assert isinstance(error, TaskRehydrationError)

    def test_a_refused_root_fails_the_triggered_build(self):
        """``build_trigger`` created the build before spawning ``build``: a
        root refused here must not leave it RUNNING."""
        functions = _finalize_capturing_functions(
            _app(build_function=lambda *a, **k: None)
        )
        payload = _payload_of(PayloadTask(p=PayloadParams(a=3)))
        payload["body"]["p"]["a"] = 5

        with patch(
            "stardag.integration.modal._functions._fail_build_best_effort"
        ) as fail:
            with pytest.raises(TaskRehydrationError):
                functions["build"](
                    [payload],
                    lambda t: "default",
                    "test-payload-app",
                    build_kwargs={"resume_build_id": "the-build"},
                )

        assert fail.call_args.args[1] == "the-build"

    def test_build_settings_are_validated_before_they_are_applied(self):
        functions = _finalize_capturing_functions(
            _app(build_function=lambda *a, **k: None)
        )

        with pytest.raises(SettingsError):
            functions["build"](
                [_payload_of(PayloadTask())],
                lambda t: "default",
                "test-payload-app",
                build_kwargs={"settings": {"AAA_STA124_LEAK": "x", "ZZZ": 1}},
            )

        assert "AAA_STA124_LEAK" not in os.environ


class TestReceiverEdges:
    def test_a_bare_resume_rehydrates_under_the_stored_settings(self):
        """``settings`` omitted on a resume means the active plan's, and the
        build function is handed that same resolved value."""
        received = []

        def build_function(tasks, worker_selector, app_name, build_kwargs=None):
            received.append((dict(os.environ), build_kwargs))

        functions = _finalize_capturing_functions(_app(build_function=build_function))
        stored = {"STA124_STORED": "yes"}

        with patch(
            "stardag.integration.modal._functions.resolve_settings",
            return_value=stored,
        ) as resolve:
            functions["build"](
                [_payload_of(PayloadTask())],
                lambda t: "default",
                "test-payload-app",
                build_kwargs={"resume_build_id": "the-build"},
            )

        assert resolve.call_args.args[1:] == ("the-build", None)
        ((_, build_kwargs),) = received
        assert build_kwargs["settings"] == stored

    def test_a_reporter_that_cannot_be_created_does_not_mask_the_refusal(self):
        from stardag.integration.modal import _reporter

        with patch.object(
            _reporter._WorkerLifecycleReporter,
            "create",
            side_effect=RuntimeError("broken registry config"),
        ):
            # Returns quietly; the caller raises the original refusal.
            _reporter.report_unreadable_task("t", {}, TaskRehydrationError("refused"))
