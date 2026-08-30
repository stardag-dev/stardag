"""Unit tests for per-task environment overrides and worker-function caching.

These tests do NOT require a real Modal account:

- ``Runner.__call__`` is exercised against local in-memory targets to verify
  that ``env_overrides`` are applied around the task ``run`` and restored
  afterwards.
- ``ModalTaskExecutor`` is exercised with a fake ``modal.Function.from_name``
  to verify the worker-function handle is memoized and that the selected
  ``env_overrides`` are forwarded to the remote call.

For the end-to-end Modal round-trip see ``test__app.py``.
"""

from __future__ import annotations

import os

import pytest

try:
    import modal  # noqa: F401
except ImportError:
    pytest.skip("Skipping modal tests (import not available)", allow_module_level=True)

import stardag as sd
from stardag.integration.modal._executor import ModalTaskExecutor
from stardag.integration.modal._protocols import _callable_accepts_env_overrides
from stardag.integration.modal._runner import Runner
from stardag.integration.modal._selector import _normalize_worker_selection
from stardag.testing.modal._tasks import make_range

_UNSET = "<unset>"


@sd.task
def env_capture(var_name: str) -> str:
    """Return the current value of ``var_name`` (``"<unset>"`` if absent)."""
    return os.environ.get(var_name, _UNSET)


@pytest.fixture
def runner() -> Runner:
    return Runner()


# --- _normalize_worker_selection ---------------------------------------------


class TestNormalizeWorkerSelection:
    def test_bare_string(self):
        assert _normalize_worker_selection("gpu") == ("gpu", None)

    def test_tuple_with_overrides(self):
        assert _normalize_worker_selection(("gpu", {"A": "1"})) == ("gpu", {"A": "1"})


# --- _callable_accepts_env_overrides (backward-compat shim) ------------------


class TestCallableAcceptsEnvOverrides:
    def test_bare_task_signature_rejected(self):
        def legacy_run(task):
            return None

        assert _callable_accepts_env_overrides(legacy_run) is False

    def test_explicit_env_overrides_param(self):
        def new_run(task, env_overrides=None):
            return None

        assert _callable_accepts_env_overrides(new_run) is True

    def test_var_keyword_accepted(self):
        def kwargs_run(task, **kwargs):
            return None

        assert _callable_accepts_env_overrides(kwargs_run) is True

    def test_runner_instance_accepted(self):
        assert _callable_accepts_env_overrides(Runner()) is True


# --- Runner env overrides -----------------------------------------------------


class TestRunnerEnvOverrides:
    def test_overrides_applied_during_run(
        self, runner: Runner, default_in_memory_fs_target
    ):
        var = "STARDAG_TEST_ENV_OVERRIDE_APPLIED"
        assert var not in os.environ
        task = env_capture(var_name=var)

        result = runner(task, env_overrides={var: "applied"})

        assert result is None
        assert task.target().load() == "applied"
        # Restored to "unset" after the run.
        assert var not in os.environ

    def test_overrides_restored_to_previous_value(
        self, runner: Runner, default_in_memory_fs_target, monkeypatch
    ):
        var = "STARDAG_TEST_ENV_OVERRIDE_RESTORE"
        monkeypatch.setenv(var, "original")
        task = env_capture(var_name=var)

        runner(task, env_overrides={var: "temporary"})

        assert task.target().load() == "temporary"
        # Pre-existing value is restored after the run.
        assert os.environ[var] == "original"

    def test_no_overrides_is_noop(self, runner: Runner, default_in_memory_fs_target):
        task = make_range(limit=3)
        assert runner(task) is None
        assert task.target().load() == [0, 1, 2]

    def test_env_overrides_is_keyword_only(
        self, runner: Runner, default_in_memory_fs_target
    ):
        # ``env_overrides`` must be passed by keyword (the framework always
        # forwards it that way); a positional second arg is a TypeError.
        with pytest.raises(TypeError):
            runner(make_range(limit=1), {"X": "1"})  # type: ignore[call-arg]

    def test_overrides_restored_even_on_exception(
        self, runner: Runner, default_in_memory_fs_target, monkeypatch
    ):
        var = "STARDAG_TEST_ENV_OVERRIDE_EXC"
        monkeypatch.setenv(var, "original")

        class _Boom(sd.Task[int]):
            def run(self) -> None:
                raise RuntimeError("boom")

        with pytest.raises(RuntimeError, match="boom"):
            runner(_Boom(), env_overrides={var: "temporary"})

        assert os.environ[var] == "original"


# --- ModalTaskExecutor: caching + env-override forwarding ---------------------


class _FakeRemote:
    def __init__(self, fn: "_FakeFunction", recorder: "_Recorder"):
        self._fn = fn
        self._recorder = recorder

    async def aio(self, task, env_overrides=None):
        self._recorder.remote_calls.append((self._fn.name, env_overrides))
        return None


class _FakeFunction:
    def __init__(self, name: str, recorder: "_Recorder"):
        self.name = name
        self.remote = _FakeRemote(self, recorder)


class _Recorder:
    def __init__(self):
        self.from_name_calls: list[tuple[str, str]] = []
        self.remote_calls: list[tuple[str, dict | None]] = []


@pytest.fixture
def fake_modal(monkeypatch) -> _Recorder:
    recorder = _Recorder()

    def fake_from_name(app_name: str, name: str):
        recorder.from_name_calls.append((app_name, name))
        return _FakeFunction(name, recorder)

    monkeypatch.setattr(modal.Function, "from_name", fake_from_name)
    return recorder


class TestModalTaskExecutorEnvOverrides:
    @pytest.mark.asyncio
    async def test_tuple_selection_forwards_env_overrides(self, fake_modal: _Recorder):
        executor = ModalTaskExecutor(
            modal_app_name="my-app",
            worker_selector=lambda t: ("gpu", {"FOO": "bar"}),
        )
        result = await executor.submit(make_range(limit=2))

        assert result is None
        assert fake_modal.from_name_calls == [("my-app", "worker_gpu")]
        assert fake_modal.remote_calls == [("worker_gpu", {"FOO": "bar"})]

    @pytest.mark.asyncio
    async def test_bare_string_selection_forwards_none(self, fake_modal: _Recorder):
        executor = ModalTaskExecutor(
            modal_app_name="my-app",
            worker_selector=lambda t: "default",
        )
        await executor.submit(make_range(limit=2))

        assert fake_modal.remote_calls == [("worker_default", None)]


class TestFinalizeWrapperBackwardCompat:
    """The ``_modal_run`` wrapper applies env overrides even for a legacy
    custom run function with the old ``(task)``-only signature."""

    def test_legacy_run_function_still_gets_env_overrides(self, monkeypatch):
        from unittest.mock import MagicMock

        from stardag.integration.modal import FunctionSettings, StardagApp

        monkeypatch.setattr(
            "stardag.integration.modal._app.get_target_roots_volumes",
            lambda *a, **k: MagicMock(by_volume_name={}, by_root_key={}),
        )

        var = "STARDAG_TEST_ENV_OVERRIDE_LEGACY"
        assert var not in os.environ
        seen: list[str] = []

        def legacy_run(task):  # old signature, no ``env_overrides``
            seen.append(os.environ.get(var, _UNSET))

        app = StardagApp(
            "test-app",
            run_function=legacy_run,
            builder_settings=FunctionSettings(image=modal.Image.debian_slim()),
            worker_settings={
                "default": FunctionSettings(image=modal.Image.debian_slim())
            },
            # Keeps this unit test hermetic. Without it, finalize() asks Modal
            # whether a `stardag-api-key` secret exists, so the test's outcome
            # depends on the developer's ambient Modal profile: it passes with
            # no credentials (the check is best-effort and skips) and with a
            # profile whose environment happens to hold that secret, and fails
            # with any other authenticated profile.
            stardag_api_key_secret=None,
        )

        registered: dict = {}

        def capture_function(**kwargs):
            def decorator(fn):
                registered[kwargs.get("name")] = fn
                return fn

            return decorator

        app.modal_app.function = capture_function  # type: ignore[assignment]
        app.finalize()

        registered["worker_default"]("a-task", env_overrides={var: "from-selector"})

        assert seen == ["from-selector"]
        # Restored afterwards.
        assert var not in os.environ


class TestModalTaskExecutorCaching:
    @pytest.mark.asyncio
    async def test_worker_function_memoized_across_submits(self, fake_modal: _Recorder):
        executor = ModalTaskExecutor(
            modal_app_name="my-app",
            worker_selector=lambda t: "default",
        )
        await executor.submit(make_range(limit=1))
        await executor.submit(make_range(limit=2))

        # ``from_name`` is called once even though two tasks were submitted.
        assert fake_modal.from_name_calls == [("my-app", "worker_default")]
        assert len(fake_modal.remote_calls) == 2

    @pytest.mark.asyncio
    async def test_distinct_workers_each_looked_up_once(self, fake_modal: _Recorder):
        workers = iter(["a", "b", "a", "b"])
        executor = ModalTaskExecutor(
            modal_app_name="my-app",
            worker_selector=lambda t: next(workers),
        )
        for limit in range(4):
            await executor.submit(make_range(limit=limit))

        # Two distinct workers -> two lookups, regardless of submit count.
        assert fake_modal.from_name_calls == [
            ("my-app", "worker_a"),
            ("my-app", "worker_b"),
        ]
        assert len(fake_modal.remote_calls) == 4
