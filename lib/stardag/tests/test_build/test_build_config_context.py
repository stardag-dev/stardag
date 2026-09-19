"""The build config is installed for exactly the duration of a build, and
reaches every place a task of that build is constructed.

Two things a ContextVar does not do on its own, and this engine has to:

- **Release it on early failure.** The config is installed before the
  roots are re-bound and the structure scope is computed, so a registry
  that refuses the build at ``build_start`` fails the call before its main
  ``try``/``finally``. A long-lived caller (a notebook, a service) must not
  keep that failed build's config for every task it constructs afterwards.
- **Reach the worker pools.** ``loop.run_in_executor`` does not copy the
  calling context into a pool thread, and a subprocess has no context at
  all. A sync ``run()`` that constructs tasks — its dynamic dependencies —
  would otherwise resolve their ``dependencies_only`` / ``execution_only``
  fields to the class defaults, while the build's scope was hashed from the
  config it never saw.
"""

from __future__ import annotations

import contextvars
from typing import Annotated
from uuid import UUID

import pytest

import stardag as sd
from stardag.base_model import StardagField
from stardag.build import (
    BuildExitStatus,
    DefaultExecutionModeSelector,
    HybridConcurrentTaskExecutor,
    build_aio,
    build_sequential,
    build_sequential_aio,
)
from stardag.build._concurrent import _run_task_in_process
from stardag.build_config import get_build_config, task_config_key
from stardag.registry import BuildInfo, NoOpRegistry
from stardag.target import InMemoryTarget

NAMESPACE = "cfg_ctx_tests"


class Generation(sd.Task[list[int]]):
    """The width lives here, on a task the parent constructs *inside* ``run``."""

    __namespace__ = NAMESPACE

    salt: str
    width: Annotated[int, StardagField(significance="dependencies_only")] = 1

    def run(self) -> None:
        self.target().save(list(range(self.width)))

    def target(self) -> InMemoryTarget[list[int]]:  # type: ignore[override]
        return InMemoryTarget(key=str(self.id))


class Child(sd.Task[int]):
    __namespace__ = NAMESPACE

    salt: str
    index: int

    def run(self) -> None:
        self.target().save(self.index)

    def target(self) -> InMemoryTarget[int]:  # type: ignore[override]
        return InMemoryTarget(key=str(self.id))


class Parent(sd.Task[int]):
    """Sync-only, so the default selector sends it to the thread pool."""

    __namespace__ = NAMESPACE

    salt: str

    def run(self):
        generation = Generation(salt=self.salt)  # constructed in the pool
        yield generation
        children = [Child(salt=self.salt, index=i) for i in generation.load()]
        yield children
        self.target().save(len(children))

    def target(self) -> InMemoryTarget[int]:  # type: ignore[override]
        return InMemoryTarget(key=str(self.id))


class Probe(sd.Task[int]):
    """A plain sync ``run`` — no yield — that constructs a configured task.

    This is the decisive shape for the thread pool. A generator ``run``
    only *creates* the generator in the pool thread; its body is driven
    from the loop thread, where the config is installed anyway. A plain
    ``run`` executes entirely in the pool thread, so what it constructs
    there sees exactly the context the pool thread was given.
    """

    __namespace__ = NAMESPACE

    salt: str

    def run(self) -> None:
        self.target().save(Generation(salt=self.salt).width)

    def target(self) -> InMemoryTarget[int]:  # type: ignore[override]
        return InMemoryTarget(key=str(self.id))


GENERATION_KEY = task_config_key(NAMESPACE, "Generation")


class _RefusingRegistry(NoOpRegistry):
    """A registry that refuses every build before anything else happens."""

    async def build_start_aio(self, *args, **kwargs) -> UUID:
        raise RuntimeError("registry says no")

    def build_start(self, *args, **kwargs) -> UUID:
        raise RuntimeError("registry says no")


class TestConfigIsReleasedOnEarlyFailure:
    @pytest.mark.asyncio
    async def test_build_aio(self, default_in_memory_fs_target):
        assert get_build_config() is None
        with pytest.raises(RuntimeError, match="registry says no"):
            await build_aio(
                [Child(salt="a", index=0)],
                registry=_RefusingRegistry(),
                build_config={GENERATION_KEY: {"width": 3}},
            )
        assert get_build_config() is None

    @pytest.mark.asyncio
    async def test_build_sequential_aio(self, default_in_memory_fs_target):
        with pytest.raises(RuntimeError, match="registry says no"):
            await build_sequential_aio(
                [Child(salt="b", index=0)],
                registry=_RefusingRegistry(),
                build_config={GENERATION_KEY: {"width": 3}},
            )
        assert get_build_config() is None

    def test_build_sequential(self, default_in_memory_fs_target):
        with pytest.raises(RuntimeError, match="registry says no"):
            build_sequential(
                [Child(salt="c", index=0)],
                registry=_RefusingRegistry(),
                build_config={GENERATION_KEY: {"width": 3}},
            )
        assert get_build_config() is None

    def test_build_sequential_with_the_config_passed_positionally(
        self, default_in_memory_fs_target
    ):
        """The decorator reads the argument by binding, not by name in kwargs."""
        from stardag.build import FailMode

        with pytest.raises(RuntimeError, match="registry says no"):
            build_sequential(
                [Child(salt="d", index=0)],
                _RefusingRegistry(),
                FailMode.FAIL_FAST,
                "sync",
                None,
                None,
                None,
                False,
                "raise",
                {GENERATION_KEY: {"width": 3}},
            )
        assert get_build_config() is None


class TestConfigReachesTheWorkerPools:
    @pytest.mark.asyncio
    async def test_a_task_constructed_in_the_thread_pool_reads_the_config(
        self, default_in_memory_fs_target
    ):
        """Default executor, default selector: ``Probe.run`` executes in a
        pool thread and constructs a ``Generation`` there. With the config
        installed only in the loop's context that generation would be one
        wide; the build asked for three."""
        probe = Probe(salt="thread")
        summary = await build_aio(
            [probe],
            registry=NoOpRegistry(),
            build_config={GENERATION_KEY: {"width": 3}},
        )
        assert summary.status == BuildExitStatus.SUCCESS
        assert probe.target().load() == 3

    @pytest.mark.asyncio
    async def test_a_dynamic_generation_is_as_wide_as_the_config_says(
        self, default_in_memory_fs_target
    ):
        """The yielded shape: the parent's generator body is driven from the
        loop thread, but the generation it yields must still be built at the
        configured width and its children counted."""
        parent = Parent(salt="thread")
        summary = await build_aio(
            [parent],
            registry=NoOpRegistry(),
            build_config={GENERATION_KEY: {"width": 3}},
        )
        assert summary.status == BuildExitStatus.SUCCESS
        assert parent.target().load() == 3
        assert Generation(salt="thread").target().load() == [0, 1, 2]

    @pytest.mark.asyncio
    async def test_the_thread_pool_is_really_where_it_ran(
        self, default_in_memory_fs_target
    ):
        """Guard against the test above passing for the wrong reason: the
        selector must send a sync-only task to SYNC_THREAD, and an explicit
        thread executor gives the same answer."""
        from stardag.build._concurrent import ExecutionMode

        selector = DefaultExecutionModeSelector(sync_run_default="thread")
        assert selector(Probe(salt="x")) == ExecutionMode.SYNC_THREAD

        probe = Probe(salt="thread-explicit")
        executor = HybridConcurrentTaskExecutor(
            execution_mode_selector=selector, max_thread_workers=2
        )
        summary = await build_aio(
            [probe],
            task_executor=executor,
            registry=NoOpRegistry(),
            build_config={GENERATION_KEY: {"width": 2}},
        )
        assert summary.status == BuildExitStatus.SUCCESS
        assert probe.target().load() == 2

    def test_the_process_runner_installs_the_config_it_is_handed(
        self, default_in_memory_fs_target
    ):
        """The subprocess entry point takes the config as an argument and
        installs it before ``run()``. Exercised in-process in a fresh
        context, which is what a spawned worker's context looks like: an
        in-memory target cannot cross a real process boundary, and the
        classes here are not importable from a spawned interpreter."""
        parent = Parent(salt="process")
        config = {GENERATION_KEY: {"width": 4}}

        def in_a_fresh_context():
            assert get_build_config() is None
            first = _run_task_in_process(parent, config)
            # The generation was yielded incomplete: it comes back to be built.
            assert first is not None
            (generation,) = first
            assert isinstance(generation, Generation)
            assert generation.width == 4
            return get_build_config()

        installed = contextvars.copy_context().run(in_a_fresh_context)
        assert installed == config
        # Nothing leaked into the caller's own context.
        assert get_build_config() is None


class _MarkedRegistry(NoOpRegistry):
    """Stores one build's config and records the resume. A *subclass* of
    NoOpRegistry: the engine skips the lookup only for the bare class."""

    def __init__(self, build_id: UUID, build_config: dict) -> None:
        self.build_id = build_id
        self.stored = build_config
        self.resumed_with: list[dict | None] = []

    def build_get(self, build_id: UUID) -> BuildInfo:
        assert build_id == self.build_id
        return BuildInfo(id=build_id, build_config=self.stored)

    async def build_get_aio(self, build_id: UUID) -> BuildInfo:
        return self.build_get(build_id)

    def build_resume(self, build_id: UUID, executor_metadata=None, **kwargs) -> None:
        self.resumed_with.append(kwargs.get("build_config"))

    async def build_resume_aio(
        self, build_id: UUID, executor_metadata=None, **kwargs
    ) -> None:
        self.build_resume(build_id, executor_metadata, **kwargs)


class TestABareResumeAdoptsTheStoredConfig:
    """``resume_build_id`` without a config means the build's own config: an
    ``execution_only`` override shares the scope hash, so hashing the bare
    scope would silently run the build at the class defaults."""

    def test_build_sequential(self, default_in_memory_fs_target):
        build_id = UUID(int=1)
        registry = _MarkedRegistry(build_id, {GENERATION_KEY: {"width": 3}})
        probe = Probe(salt="resume-seq")

        summary = build_sequential([probe], registry=registry, resume_build_id=build_id)

        assert summary.status == BuildExitStatus.SUCCESS
        assert probe.target().load() == 3
        assert registry.resumed_with == [{GENERATION_KEY: {"width": 3}}]
        assert get_build_config() is None  # released with the build

    @pytest.mark.asyncio
    async def test_build_aio(self, default_in_memory_fs_target):
        build_id = UUID(int=2)
        registry = _MarkedRegistry(build_id, {GENERATION_KEY: {"width": 3}})
        probe = Probe(salt="resume-aio")

        summary = await build_aio([probe], registry=registry, resume_build_id=build_id)

        assert summary.status == BuildExitStatus.SUCCESS
        assert probe.target().load() == 3
        assert registry.resumed_with == [{GENERATION_KEY: {"width": 3}}]
        assert get_build_config() is None

    def test_a_given_config_wins_over_the_stored_one(self, default_in_memory_fs_target):
        """The lookup is for the bare case only; an explicit config is the
        caller's claim and the registry decides whether it matches."""
        build_id = UUID(int=3)
        registry = _MarkedRegistry(build_id, {GENERATION_KEY: {"width": 3}})
        probe = Probe(salt="resume-given")

        build_sequential(
            [probe],
            registry=registry,
            resume_build_id=build_id,
            build_config={GENERATION_KEY: {"width": 2}},
        )

        assert probe.target().load() == 2
        assert registry.resumed_with == [{GENERATION_KEY: {"width": 2}}]
