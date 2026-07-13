"""Unit tests for Modal executor metadata resolution and propagation.

Covers the executor side (metadata on detached handles + the env-override
channel to workers) and the worker side (self-reported starts carrying the
same dict, dropping it for registries predating the kwarg). Workspace and
environment values are pinned by the ``hermetic_modal_executor_metadata``
conftest fixture; the resolution helpers themselves are tested with
explicit monkeypatching below.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest

try:
    import modal  # noqa: F401
except ImportError:
    pytest.skip("Skipping modal tests (import not available)", allow_module_level=True)

from stardag import BaseTask
from stardag.build._base import current_build_id_var
from stardag.integration.modal._app import (
    MODAL_EXECUTOR_NAME,
    STARDAG_BUILD_ID_ENV,
    STARDAG_MODAL_APP_NAME_ENV,
    STARDAG_MODAL_ENVIRONMENT_ENV,
    STARDAG_MODAL_FUNCTION_NAME_ENV,
    STARDAG_MODAL_WORKSPACE_ENV,
    ModalTaskExecutor,
    _WorkerLifecycleReporter,
)
from stardag.registry import NoOpRegistry

EXPECTED_BASE_METADATA = {
    "kind": "modal",
    "app_name": "test-app",
    "workspace": "test-workspace",
    "environment": "test-env",
}


class FakeFunctionCall:
    def __init__(self, object_id: str = "fc-meta-1"):
        self.object_id = object_id
        self.get = SimpleNamespace(aio=self._get_aio)

    async def _get_aio(self, timeout: float | None = None):
        if timeout == 0:
            raise TimeoutError("still running")
        return None


class FakeWorkerFunction:
    def __init__(self, function_call: FakeFunctionCall):
        self._function_call = function_call
        self.spawn_calls: list[tuple] = []
        self.spawn = SimpleNamespace(aio=self._spawn_aio)

    async def _spawn_aio(self, task, env_overrides=None):
        self.spawn_calls.append((task, env_overrides))
        return self._function_call


def _make_executor(worker, **kwargs) -> ModalTaskExecutor:
    executor = ModalTaskExecutor(
        modal_app_name="test-app",
        worker_selector=lambda task: "default",
        **kwargs,
    )
    executor._worker_functions["default"] = worker  # bypass from_name
    return executor


def _make_task() -> BaseTask:
    task = MagicMock(spec=BaseTask)
    task.id = uuid4()
    return task


class TestDetachedHandleMetadata:
    async def test_handle_carries_resolved_metadata(self):
        worker = FakeWorkerFunction(FakeFunctionCall())
        executor = _make_executor(worker)

        handle = await executor.submit_detached(_make_task())

        assert handle.executor_metadata == {
            **EXPECTED_BASE_METADATA,
            "function_name": "worker_default",
        }

    async def test_explicit_workspace_override_wins(self):
        worker = FakeWorkerFunction(FakeFunctionCall())
        executor = _make_executor(worker, modal_workspace="explicit-ws")

        handle = await executor.submit_detached(_make_task())

        assert handle.executor_metadata is not None
        assert handle.executor_metadata["workspace"] == "explicit-ws"

    async def test_resolution_failure_never_fails_the_spawn(self, monkeypatch):
        """A broken workspace lookup degrades to identity-only metadata."""
        from stardag.integration.modal import _app as modal_app_module

        async def _boom():
            raise ConnectionError("no network")

        monkeypatch.setattr(modal_app_module, "_get_modal_workspace_aio", _boom)
        monkeypatch.setattr(
            modal_app_module,
            "_get_modal_environment",
            lambda: (_ for _ in ()).throw(ConnectionError("also broken")),
        )
        worker = FakeWorkerFunction(FakeFunctionCall())
        executor = _make_executor(worker)

        handle = await executor.submit_detached(_make_task())

        assert handle.ref == "fc-meta-1"
        assert handle.executor_metadata == {
            "kind": "modal",
            "app_name": "test-app",
            "function_name": "worker_default",
        }

    async def test_failed_workspace_lookup_cached(
        self, monkeypatch, hermetic_modal_executor_metadata
    ):
        """A raising token lookup is cached as None: no raise reaches the
        caller and the lookup (and its timeout) is not re-paid on every
        task start."""
        from stardag.integration.modal import _app as modal_app_module

        real_get = hermetic_modal_executor_metadata["get_modal_workspace_aio"]
        calls = {"n": 0}

        async def _raising_lookup():
            calls["n"] += 1
            raise TimeoutError("modal api unreachable")

        monkeypatch.setattr(
            modal_app_module, "_lookup_modal_workspace_aio", _raising_lookup
        )
        monkeypatch.setattr(
            modal_app_module,
            "_modal_workspace_cache",
            modal_app_module._MODAL_WORKSPACE_UNRESOLVED,
        )

        assert await real_get() is None
        assert await real_get() is None
        assert calls["n"] == 1  # failure cached, not retried

    async def test_base_metadata_resolved_once(self, monkeypatch):
        from stardag.integration.modal import _app as modal_app_module

        calls = {"n": 0}

        async def _counting():
            calls["n"] += 1
            return "counted-ws"

        monkeypatch.setattr(modal_app_module, "_get_modal_workspace_aio", _counting)
        worker = FakeWorkerFunction(FakeFunctionCall())
        executor = _make_executor(worker)

        await executor.submit_detached(_make_task())
        await executor.submit_detached(_make_task())

        assert calls["n"] == 1

    async def test_reattach_handle_carries_base_metadata(self, monkeypatch):
        """Re-attached handles get base metadata only — the worker function
        behind a bare ref isn't known."""
        function_call = FakeFunctionCall(object_id="fc-live")
        monkeypatch.setattr(
            modal.FunctionCall, "from_id", staticmethod(lambda ref: function_call)
        )
        executor = _make_executor(FakeWorkerFunction(function_call))

        handle = await executor.reattach(_make_task(), MODAL_EXECUTOR_NAME, "fc-live")

        assert handle is not None
        assert handle.executor_metadata == EXPECTED_BASE_METADATA


class TestWorkerEnvForwarding:
    async def test_metadata_env_vars_forwarded_in_build_context(self):
        build_id = uuid4()
        worker = FakeWorkerFunction(FakeFunctionCall())
        executor = _make_executor(worker)

        token = current_build_id_var.set(build_id)
        try:
            await executor.submit_detached(_make_task())
        finally:
            current_build_id_var.reset(token)

        _, env_overrides = worker.spawn_calls[0]
        assert env_overrides[STARDAG_BUILD_ID_ENV] == str(build_id)
        assert env_overrides[STARDAG_MODAL_APP_NAME_ENV] == "test-app"
        assert env_overrides[STARDAG_MODAL_WORKSPACE_ENV] == "test-workspace"
        assert env_overrides[STARDAG_MODAL_ENVIRONMENT_ENV] == "test-env"
        assert env_overrides[STARDAG_MODAL_FUNCTION_NAME_ENV] == "worker_default"

    async def test_not_forwarded_outside_build_context(self):
        worker = FakeWorkerFunction(FakeFunctionCall())
        executor = _make_executor(worker)

        await executor.submit_detached(_make_task())

        _, env_overrides = worker.spawn_calls[0]
        assert env_overrides is None or STARDAG_MODAL_WORKSPACE_ENV not in env_overrides


class MetadataAwareRegistry(NoOpRegistry):
    """Registry whose task_start accepts the executor_metadata kwarg."""

    def __init__(self) -> None:
        super().__init__()
        self.starts: list[dict] = []

    def task_start(
        self,
        build_id,
        task,
        executor=None,
        executor_ref=None,
        executor_metadata=None,
    ) -> None:
        self.starts.append(
            {
                "executor": executor,
                "executor_ref": executor_ref,
                "executor_metadata": executor_metadata,
            }
        )


class LegacyRegistry(NoOpRegistry):
    """Registry with the pre-metadata task_start signature."""

    def __init__(self) -> None:
        super().__init__()
        self.starts: list[dict] = []

    def task_start(self, build_id, task, executor=None, executor_ref=None) -> None:  # pyright: ignore[reportIncompatibleMethodOverride]  # pre-metadata signature (deliberate)
        self.starts.append({"executor": executor, "executor_ref": executor_ref})


def _reporter_env(build_id) -> dict[str, str]:
    return {
        STARDAG_BUILD_ID_ENV: str(build_id),
        STARDAG_MODAL_APP_NAME_ENV: "test-app",
        STARDAG_MODAL_WORKSPACE_ENV: "test-workspace",
        STARDAG_MODAL_ENVIRONMENT_ENV: "test-env",
        STARDAG_MODAL_FUNCTION_NAME_ENV: "worker_default",
    }


class TestWorkerReporterMetadata:
    def _create_reporter(self, registry, env) -> _WorkerLifecycleReporter:
        from stardag.registry import registry_provider

        with registry_provider.override(registry):
            reporter = _WorkerLifecycleReporter.create(_make_task(), env)
        assert reporter is not None
        return reporter

    def test_create_builds_metadata_from_env(self):
        reporter = self._create_reporter(
            MetadataAwareRegistry(), _reporter_env(uuid4())
        )
        assert reporter.executor_metadata == {
            **EXPECTED_BASE_METADATA,
            "function_name": "worker_default",
        }

    def test_started_passes_metadata_to_aware_registry(self, monkeypatch):
        monkeypatch.setattr(modal, "current_function_call_id", lambda: "fc-worker-1")
        registry = MetadataAwareRegistry()
        reporter = self._create_reporter(registry, _reporter_env(uuid4()))

        reporter.started()

        assert registry.starts == [
            {
                "executor": MODAL_EXECUTOR_NAME,
                "executor_ref": "fc-worker-1",
                "executor_metadata": {
                    **EXPECTED_BASE_METADATA,
                    "function_name": "worker_default",
                },
            }
        ]

    def test_started_drops_metadata_for_legacy_registry(self, monkeypatch):
        """No TypeError against a pre-metadata registry — the start (with
        its ref) is still recorded."""
        monkeypatch.setattr(modal, "current_function_call_id", lambda: "fc-worker-2")
        registry = LegacyRegistry()
        reporter = self._create_reporter(registry, _reporter_env(uuid4()))

        reporter.started()

        assert registry.starts == [
            {"executor": MODAL_EXECUTOR_NAME, "executor_ref": "fc-worker-2"}
        ]

    def test_metadata_minimal_without_forwarded_env(self):
        """Older orchestrators forward only the build id — the worker still
        reports what it knows (executor kind)."""
        build_id = uuid4()
        reporter = self._create_reporter(
            MetadataAwareRegistry(), {STARDAG_BUILD_ID_ENV: str(build_id)}
        )
        assert reporter.build_id == UUID(str(build_id))
        assert reporter.executor_metadata == {"kind": "modal"}


class TestGetExecutorMetadata:
    """`get_executor_metadata` resolves the same dict as a spawn would
    record, without starting anything — used by reactive ticks to stamp
    the slot-acquiring TASK_STARTED before the spawn."""

    async def test_resolves_without_spawning(self):
        worker = FakeWorkerFunction(FakeFunctionCall())
        executor = _make_executor(worker)

        metadata = await executor.get_executor_metadata(_make_task())

        assert metadata == {
            **EXPECTED_BASE_METADATA,
            "function_name": "worker_default",
        }
        assert worker.spawn_calls == []

    async def test_selector_failure_returns_none(self):
        def _boom_selector(task):
            raise RuntimeError("selector blew up")

        executor = ModalTaskExecutor(
            modal_app_name="test-app", worker_selector=_boom_selector
        )

        assert await executor.get_executor_metadata(_make_task()) is None


class TestWorkspaceLookupColdBurst:
    async def test_parallel_cold_start_performs_one_lookup(
        self, monkeypatch, hermetic_modal_executor_metadata
    ):
        """Concurrent cold-start resolutions serialize on the module lock:
        one network lookup, everyone gets the cached result."""
        import asyncio

        from stardag.integration.modal import _app as modal_app_module

        real_get = hermetic_modal_executor_metadata["get_modal_workspace_aio"]
        calls = {"n": 0}

        async def _slow_lookup():
            calls["n"] += 1
            await asyncio.sleep(0.01)
            return "burst-ws"

        monkeypatch.setattr(
            modal_app_module, "_lookup_modal_workspace_aio", _slow_lookup
        )
        monkeypatch.setattr(
            modal_app_module,
            "_modal_workspace_cache",
            modal_app_module._MODAL_WORKSPACE_UNRESOLVED,
        )

        results = await asyncio.gather(*[real_get() for _ in range(10)])

        assert results == ["burst-ws"] * 10
        assert calls["n"] == 1
