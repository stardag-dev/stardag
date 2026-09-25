"""Unit tests for Modal executor metadata resolution and propagation.

Covers the executor side (metadata on detached handles + the env-override
channel to workers) and the worker side (the holder's non-claiming start
carrying the same dict). Workspace and
environment values are pinned by the ``hermetic_modal_executor_metadata``
conftest fixture; the resolution helpers themselves are tested with
explicit monkeypatching below.
"""

import contextlib
from collections.abc import Iterator
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest

try:
    import modal  # noqa: F401
except ImportError:
    pytest.skip("Skipping modal tests (import not available)", allow_module_level=True)

from stardag import BaseTask
from stardag.build._base import BuildContext, current_build_context_var
from stardag.build._claims import CLAIM_TTL_GRACE_SECONDS
from stardag.integration.modal._executor import ModalTaskExecutor
from stardag.integration.modal._metadata import (
    MODAL_EXECUTOR_NAME,
    STARDAG_BUILD_ID_ENV,
    STARDAG_CLAIM_TTL_SECONDS_ENV,
    STARDAG_EXECUTION_ID_ENV,
    STARDAG_MODAL_APP_ID_ENV,
    STARDAG_MODAL_APP_NAME_ENV,
    STARDAG_MODAL_ENVIRONMENT_ENV,
    STARDAG_MODAL_FUNCTION_ID_ENV,
    STARDAG_MODAL_FUNCTION_NAME_ENV,
    STARDAG_MODAL_WORKSPACE_ENV,
    STARDAG_PLAN_ID_ENV,
)
from stardag.integration.modal._reporter import _WorkerLifecycleReporter
from stardag.testing import InMemoryRegistry

EXPECTED_BASE_METADATA = {
    "kind": "modal",
    "app_name": "test-app",
    "workspace": "test-workspace",
    "environment": "test-env",
    "app_id": "ap-test-app",
}

# Base metadata + the per-worker fields (function name/id) — the dict a
# worker-routed start records and forwards.
EXPECTED_WORKER_METADATA = {
    **EXPECTED_BASE_METADATA,
    "function_name": "worker_default",
    "function_id": "fu-test-fn",
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


@contextlib.contextmanager
def _in_build(build_id: UUID | None = None) -> Iterator[BuildContext]:
    context = BuildContext(build_id=build_id or uuid4(), plan_id=uuid4())
    token = current_build_context_var.set(context)
    try:
        yield context
    finally:
        current_build_context_var.reset(token)


class TestDetachedHandleMetadata:
    async def test_handle_carries_resolved_metadata(self):
        worker = FakeWorkerFunction(FakeFunctionCall())
        executor = _make_executor(worker)

        handle = await executor.submit_detached(_make_task(), execution_id=uuid4())

        assert handle.executor_metadata == EXPECTED_WORKER_METADATA

    async def test_explicit_workspace_override_wins(self):
        worker = FakeWorkerFunction(FakeFunctionCall())
        executor = _make_executor(worker, modal_workspace="explicit-ws")

        handle = await executor.submit_detached(_make_task(), execution_id=uuid4())

        assert handle.executor_metadata is not None
        assert handle.executor_metadata["workspace"] == "explicit-ws"

    async def test_resolution_failure_never_fails_the_spawn(self, monkeypatch):
        """A broken metadata resolution degrades to identity-only metadata.

        All four best-effort fields (workspace, environment, app id,
        function id) fail here; the spawn still succeeds and the handle
        carries only the identity fields the executor always knows.
        """
        from stardag.integration.modal import _executor as executor_module

        async def _boom():
            raise ConnectionError("no network")

        # The id resolvers swallow their own failures and return None (the
        # key is then omitted) — that is their contract, so simulate it.
        async def _no_app_id(app_name, environment_name=None):
            return None

        async def _no_function_id(function):
            return None

        monkeypatch.setattr(executor_module, "_get_modal_workspace_aio", _boom)
        monkeypatch.setattr(
            executor_module,
            "_get_modal_environment",
            lambda: (_ for _ in ()).throw(ConnectionError("also broken")),
        )
        monkeypatch.setattr(executor_module, "_get_modal_app_id_aio", _no_app_id)
        monkeypatch.setattr(
            executor_module, "_get_modal_function_id_aio", _no_function_id
        )
        worker = FakeWorkerFunction(FakeFunctionCall())
        executor = _make_executor(worker)

        handle = await executor.submit_detached(_make_task(), execution_id=uuid4())

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
        from stardag.integration.modal import _metadata as metadata_module

        real_get = hermetic_modal_executor_metadata["get_modal_workspace_aio"]
        calls = {"n": 0}

        async def _raising_lookup():
            calls["n"] += 1
            raise TimeoutError("modal api unreachable")

        monkeypatch.setattr(
            metadata_module, "_lookup_modal_workspace_aio", _raising_lookup
        )
        monkeypatch.setattr(
            metadata_module,
            "_modal_workspace_cache",
            metadata_module._MODAL_WORKSPACE_UNRESOLVED,
        )

        assert await real_get() is None
        assert await real_get() is None
        assert calls["n"] == 1  # failure cached, not retried

    async def test_env_workspace_preferred_over_token_lookup(
        self, monkeypatch, hermetic_modal_executor_metadata
    ):
        """Inside a Modal container there is no token, so the workspace is
        baked into STARDAG_MODAL_WORKSPACE at deploy; the resolver must read
        that env var instead of attempting (and failing) a token lookup."""
        from stardag.integration.modal import _metadata as metadata_module

        real_get = hermetic_modal_executor_metadata["get_modal_workspace_aio"]

        async def _must_not_be_called():
            raise AssertionError("token lookup must not run when env is set")

        monkeypatch.setattr(
            metadata_module, "_lookup_modal_workspace_aio", _must_not_be_called
        )
        monkeypatch.setattr(
            metadata_module,
            "_modal_workspace_cache",
            metadata_module._MODAL_WORKSPACE_UNRESOLVED,
        )
        monkeypatch.setenv(
            metadata_module.STARDAG_MODAL_WORKSPACE_ENV, "baked-workspace"
        )

        assert await real_get() == "baked-workspace"

    async def test_workspace_lookup_falls_back_to_username(self, monkeypatch):
        """The Modal workspace lookup response leaves `workspace_name` empty
        for a personal workspace; the slug lives in `username` (what
        `modal token info` prints). The resolver must fall back to it —
        otherwise the (common) personal-workspace case resolves to nothing
        and UI deep links break."""
        import types

        from stardag.integration.modal import _metadata as metadata_module

        monkeypatch.setattr(
            metadata_module.modal.config,
            "config",
            types.SimpleNamespace(
                get=lambda k: {
                    "server_url": "https://api.modal.com",
                    "token_id": "tok",
                    "token_secret": "sec",
                }.get(k)
            ),
            raising=False,
        )

        async def _fake_lookup(server_url, token_id, token_secret):
            return types.SimpleNamespace(workspace_name="", username="andhus")

        monkeypatch.setattr(
            metadata_module.modal.config,
            "_lookup_workspace",
            _fake_lookup,
            raising=False,
        )
        assert await metadata_module._lookup_modal_workspace_aio() == "andhus"

        async def _fake_lookup_named(server_url, token_id, token_secret):
            return types.SimpleNamespace(workspace_name="my-org", username="u")

        monkeypatch.setattr(
            metadata_module.modal.config,
            "_lookup_workspace",
            _fake_lookup_named,
            raising=False,
        )
        assert await metadata_module._lookup_modal_workspace_aio() == "my-org"

    async def test_base_metadata_resolved_once(self, monkeypatch):
        from stardag.integration.modal import _executor as executor_module

        calls = {"n": 0}

        async def _counting():
            calls["n"] += 1
            return "counted-ws"

        monkeypatch.setattr(executor_module, "_get_modal_workspace_aio", _counting)
        worker = FakeWorkerFunction(FakeFunctionCall())
        executor = _make_executor(worker)

        await executor.submit_detached(_make_task(), execution_id=uuid4())
        await executor.submit_detached(_make_task(), execution_id=uuid4())

        assert calls["n"] == 1


class TestWorkerEnvForwarding:
    async def test_metadata_env_vars_forwarded_in_build_context(self):
        build_id = uuid4()
        worker = FakeWorkerFunction(FakeFunctionCall())
        executor = _make_executor(worker)

        with _in_build(build_id):
            await executor.submit_detached(_make_task(), execution_id=uuid4())

        _, env_overrides = worker.spawn_calls[0]
        assert env_overrides[STARDAG_BUILD_ID_ENV] == str(build_id)
        assert env_overrides[STARDAG_MODAL_APP_NAME_ENV] == "test-app"
        assert env_overrides[STARDAG_MODAL_WORKSPACE_ENV] == "test-workspace"
        assert env_overrides[STARDAG_MODAL_ENVIRONMENT_ENV] == "test-env"
        assert env_overrides[STARDAG_MODAL_FUNCTION_NAME_ENV] == "worker_default"
        assert env_overrides[STARDAG_MODAL_APP_ID_ENV] == "ap-test-app"
        assert env_overrides[STARDAG_MODAL_FUNCTION_ID_ENV] == "fu-test-fn"

    async def test_not_forwarded_outside_build_context(self):
        worker = FakeWorkerFunction(FakeFunctionCall())
        executor = _make_executor(worker)

        await executor.submit_detached(_make_task(), execution_id=uuid4())

        _, env_overrides = worker.spawn_calls[0]
        assert env_overrides is None or STARDAG_MODAL_WORKSPACE_ENV not in env_overrides


class TestClaimTtlForwarding:
    """The claim TTL derived from the worker's Modal timeout reaches the
    worker, whose non-claiming start after a preemption restores the
    claim's TTL (the registry's default otherwise)."""

    async def test_ttl_derived_from_the_workers_timeout_is_forwarded(self):
        worker = FakeWorkerFunction(FakeFunctionCall())
        executor = _make_executor(worker, worker_timeouts={"default": 3600})

        with _in_build():
            await executor.submit_detached(_make_task(), execution_id=uuid4())

        _, env_overrides = worker.spawn_calls[0]
        assert env_overrides[STARDAG_CLAIM_TTL_SECONDS_ENV] == str(
            int(3600 + CLAIM_TTL_GRACE_SECONDS)
        )

    async def test_no_declared_timeout_forwards_nothing(self):
        """Without the deployed settings there is no timeout to derive from,
        and the registry's default is the honest fallback."""
        worker = FakeWorkerFunction(FakeFunctionCall())
        executor = _make_executor(worker)

        with _in_build():
            await executor.submit_detached(_make_task(), execution_id=uuid4())

        _, env_overrides = worker.spawn_calls[0]
        assert STARDAG_CLAIM_TTL_SECONDS_ENV not in env_overrides

    def test_execution_timeout_comes_from_the_selected_worker(self):
        executor = _make_executor(
            FakeWorkerFunction(FakeFunctionCall()),
            worker_timeouts={"default": 60, "gpu": 7200},
        )
        executor.worker_selector = lambda task: "gpu"

        assert executor.execution_timeout_seconds(_make_task()) == 7200.0

    def test_unknown_worker_has_no_timeout(self):
        executor = _make_executor(
            FakeWorkerFunction(FakeFunctionCall()),
            worker_timeouts={"default": 60},
        )
        executor.worker_selector = lambda task: "unlisted"

        assert executor.execution_timeout_seconds(_make_task()) is None


def _reporter_env(build_id, plan_id, execution_id) -> dict[str, str]:
    return {
        STARDAG_BUILD_ID_ENV: str(build_id),
        STARDAG_PLAN_ID_ENV: str(plan_id),
        STARDAG_EXECUTION_ID_ENV: str(execution_id),
        STARDAG_MODAL_APP_NAME_ENV: "test-app",
        STARDAG_MODAL_WORKSPACE_ENV: "test-workspace",
        STARDAG_MODAL_ENVIRONMENT_ENV: "test-env",
        STARDAG_MODAL_FUNCTION_NAME_ENV: "worker_default",
        STARDAG_MODAL_APP_ID_ENV: "ap-test-app",
        STARDAG_MODAL_FUNCTION_ID_ENV: "fu-test-fn",
    }


class TestWorkerReporterMetadata:
    """The worker's non-claiming start records the same executor metadata
    the executor resolved, rebuilt from the forwarded env."""

    def _create_reporter(self, registry, env) -> _WorkerLifecycleReporter:
        from stardag.registry import registry_provider

        with registry_provider.override(registry):
            reporter = _WorkerLifecycleReporter.create(_make_task(), env)
        assert reporter is not None
        return reporter

    def test_create_builds_metadata_from_env(self):
        reporter = self._create_reporter(
            InMemoryRegistry(), _reporter_env(uuid4(), uuid4(), uuid4())
        )
        assert reporter.executor_metadata == EXPECTED_WORKER_METADATA

    def test_started_records_the_metadata_under_the_execution(self, monkeypatch):
        monkeypatch.setattr(modal, "current_function_call_id", lambda: "fc-worker-1")
        registry = InMemoryRegistry()
        build_id, plan_id, execution_id = uuid4(), uuid4(), uuid4()
        reporter = self._create_reporter(
            registry, _reporter_env(build_id, plan_id, execution_id)
        )
        # The member is unknown to the fake: the start is refused, but the
        # call it made is what is under test (reporting is best-effort).
        reporter.started()
        (start,) = registry.calls_to("member_start")
        assert start["claim"] is False
        assert start["execution_id"] == execution_id
        assert start["plan_id"] == plan_id
        assert start["executor"] == MODAL_EXECUTOR_NAME
        assert start["executor_ref"] == "fc-worker-1"
        assert start["executor_metadata"] == EXPECTED_WORKER_METADATA

    def test_without_the_forwarded_ids_there_is_nothing_to_report(self):
        """A build id alone names no execution: no reporter."""
        from stardag.registry import registry_provider

        with registry_provider.override(InMemoryRegistry()):
            assert (
                _WorkerLifecycleReporter.create(
                    _make_task(), {STARDAG_BUILD_ID_ENV: str(uuid4())}
                )
                is None
            )

    def test_metadata_minimal_without_forwarded_modal_env(self):
        build_id = uuid4()
        env = {
            STARDAG_BUILD_ID_ENV: str(build_id),
            STARDAG_PLAN_ID_ENV: str(uuid4()),
            STARDAG_EXECUTION_ID_ENV: str(uuid4()),
        }
        reporter = self._create_reporter(InMemoryRegistry(), env)
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

        assert metadata == EXPECTED_WORKER_METADATA
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

        from stardag.integration.modal import _metadata as metadata_module

        real_get = hermetic_modal_executor_metadata["get_modal_workspace_aio"]
        calls = {"n": 0}

        async def _slow_lookup():
            calls["n"] += 1
            await asyncio.sleep(0.01)
            return "burst-ws"

        monkeypatch.setattr(
            metadata_module, "_lookup_modal_workspace_aio", _slow_lookup
        )
        monkeypatch.setattr(
            metadata_module,
            "_modal_workspace_cache",
            metadata_module._MODAL_WORKSPACE_UNRESOLVED,
        )

        results = await asyncio.gather(*[real_get() for _ in range(10)])

        assert results == ["burst-ws"] * 10
        assert calls["n"] == 1


class TestAppAndFunctionIdResolution:
    """The real ``_get_modal_app_id_aio`` / ``_get_modal_function_id_aio``
    helpers (the conftest fixture normally pins them; here we reach past the
    pin via the ``originals`` it exposes and stub the underlying Modal API).
    """

    async def test_app_id_resolved_from_lookup(
        self, monkeypatch, hermetic_modal_executor_metadata
    ):
        real = hermetic_modal_executor_metadata["get_modal_app_id_aio"]

        async def _fake_lookup(name, environment_name=None):
            assert name == "some-app"
            assert environment_name == "some-env"
            return SimpleNamespace(app_id="ap-live-123")

        monkeypatch.setattr(
            modal.App, "lookup", SimpleNamespace(aio=_fake_lookup), raising=False
        )

        assert await real("some-app", "some-env") == "ap-live-123"

    async def test_app_id_omitted_on_failure(
        self, monkeypatch, hermetic_modal_executor_metadata
    ):
        real = hermetic_modal_executor_metadata["get_modal_app_id_aio"]

        async def _boom_lookup(name, environment_name=None):
            raise ConnectionError("modal api unreachable")

        monkeypatch.setattr(
            modal.App, "lookup", SimpleNamespace(aio=_boom_lookup), raising=False
        )

        assert await real("some-app", None) is None

    async def test_function_id_resolved_after_hydrate(
        self, hermetic_modal_executor_metadata
    ):
        real = hermetic_modal_executor_metadata["get_modal_function_id_aio"]

        class _Fn:
            def __init__(self):
                self.object_id = "fu-live-456"
                self.hydrated = False
                self.hydrate = SimpleNamespace(aio=self._hydrate)

            async def _hydrate(self):
                self.hydrated = True

        fn = _Fn()
        assert await real(fn) == "fu-live-456"
        assert fn.hydrated

    async def test_function_id_omitted_on_failure(
        self, hermetic_modal_executor_metadata
    ):
        real = hermetic_modal_executor_metadata["get_modal_function_id_aio"]

        class _Fn:
            def __init__(self):
                self.hydrate = SimpleNamespace(aio=self._hydrate)

            async def _hydrate(self):
                raise RuntimeError("cannot hydrate")

        assert await real(_Fn()) is None

    async def test_app_id_omitted_on_timeout(
        self, monkeypatch, hermetic_modal_executor_metadata
    ):
        """A hung ``modal.App.lookup`` is bounded by the short id-lookup
        timeout — it must not stall the caller; the id is omitted."""
        import asyncio

        from stardag.integration.modal import _metadata as metadata_module

        real = hermetic_modal_executor_metadata["get_modal_app_id_aio"]
        monkeypatch.setattr(metadata_module, "_MODAL_ID_LOOKUP_TIMEOUT_SECONDS", 0.01)

        async def _hang(name, environment_name=None):
            await asyncio.sleep(10)
            return SimpleNamespace(app_id="ap-never")

        monkeypatch.setattr(
            modal.App, "lookup", SimpleNamespace(aio=_hang), raising=False
        )

        assert await real("some-app", None) is None

    async def test_function_id_omitted_on_timeout(
        self, monkeypatch, hermetic_modal_executor_metadata
    ):
        """A hung ``Function.hydrate`` is bounded by the short id-lookup
        timeout; the id is omitted rather than stalling the start."""
        import asyncio

        from stardag.integration.modal import _metadata as metadata_module

        real = hermetic_modal_executor_metadata["get_modal_function_id_aio"]
        monkeypatch.setattr(metadata_module, "_MODAL_ID_LOOKUP_TIMEOUT_SECONDS", 0.01)

        class _Fn:
            def __init__(self):
                self.object_id = "fu-never"
                self.hydrate = SimpleNamespace(aio=self._hydrate)

            async def _hydrate(self):
                await asyncio.sleep(10)

        assert await real(_Fn()) is None


class TestFunctionIdCaching:
    """Function-id resolution is cached per worker name — success *and*
    failure — so a persistently broken/hung hydration is not re-paid on
    every task start (the per-start re-pay the workspace-lookup fix
    eliminated, now also for function ids)."""

    async def test_resolved_function_id_cached_per_worker(self, monkeypatch):
        from stardag.integration.modal import _executor as executor_module

        calls = {"n": 0}

        async def _counting(function):
            calls["n"] += 1
            return "fu-counted"

        monkeypatch.setattr(executor_module, "_get_modal_function_id_aio", _counting)
        worker = FakeWorkerFunction(FakeFunctionCall())
        executor = _make_executor(worker)

        await executor.submit_detached(_make_task(), execution_id=uuid4())
        await executor.submit_detached(_make_task(), execution_id=uuid4())

        assert calls["n"] == 1

    async def test_failed_function_id_cached_per_worker(self, monkeypatch):
        """A resolved-but-``None`` (failed hydration) is a resolution: it is
        cached and not retried on the next start, and the key stays omitted."""
        from stardag.integration.modal import _executor as executor_module

        calls = {"n": 0}

        async def _failing(function):
            calls["n"] += 1
            return None

        monkeypatch.setattr(executor_module, "_get_modal_function_id_aio", _failing)
        worker = FakeWorkerFunction(FakeFunctionCall())
        executor = _make_executor(worker)

        handle = await executor.submit_detached(_make_task(), execution_id=uuid4())
        await executor.submit_detached(_make_task(), execution_id=uuid4())

        assert calls["n"] == 1  # negative result cached, not retried
        assert handle.executor_metadata is not None
        assert "function_id" not in handle.executor_metadata
