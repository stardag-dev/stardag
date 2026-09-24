"""The sequential engine: one task at a time, for debugging and testing.

Tasks run one after the other in dependency order; a task's dynamic
dependencies are built inline, depth-first, while its generator waits. The
registry side is the resident engines' one
:class:`~stardag.build._session.ResidentSession`: the walk is registered
as a plan (roots first, post-order, sealed), every execution claims — with
a TTL renewed while it runs (D11) — and a yield is sent with ``suspend:
false``, since the generator stays alive.

The scheduling algorithm exists once, as a coroutine
(:class:`_SequentialEngine`). ``build_sequential_aio`` runs it on the
caller's loop. The sync ``build_sequential`` runs it on a private loop in a
helper thread and executes every piece of *task* code — ``run()``,
``run_aio()`` via ``asyncio.run``, and each generator step — back on the
calling thread, so a debugger sees task code where it was called from and
the claim renewals keep running while a task blocks.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import inspect
import logging
import queue
import threading
import typing
from collections.abc import Callable, Sequence
from typing import Any, Literal
from uuid import UUID

from stardag import BaseTask, TaskStruct, flatten_task_struct
from stardag._core.base_task import _has_custom_run, _has_custom_run_aio
from stardag._core.instance import extend_path
from stardag.build._base import (
    BuildContext,
    BuildExitStatus,
    BuildSummary,
    ClaimConfig,
    FailMode,
    OnRegistryFailure,
    TaskCount,
    current_build_context_var,
    handle_registry_error,
)
from stardag.build._registration import Walk, walk_aio, yield_batches
from stardag.build._session import LimitKeySelector, ResidentSession
from stardag.build._settings import resident_settings, validate_settings
from stardag.registry import RegistryABC, registry_provider

logger = logging.getLogger(__name__)

_DONE = object()


class _Runner(typing.Protocol):
    """How the engine executes task code: start a task, step its generator."""

    async def start(self, task: BaseTask) -> Any: ...

    async def step(self, result: Any) -> Any:
        """The next yielded TaskStruct of ``result``, or ``_DONE``."""
        ...


class _AsyncRunner:
    """Task code on the engine's own loop (``build_sequential_aio``)."""

    def __init__(self, sync_run_default: Literal["thread", "blocking"]) -> None:
        self.sync_run_default = sync_run_default

    async def start(self, task: BaseTask) -> Any:
        if _has_custom_run_aio(task):
            if inspect.isasyncgenfunction(type(task).run_aio):
                return task.run_aio()
            return await task.run_aio()
        if _has_custom_run(task):
            if self.sync_run_default == "thread":
                return await asyncio.to_thread(task.run)
            return task.run()
        raise ValueError(f"Task {task} has no run method")

    async def step(self, result: Any) -> Any:
        if hasattr(result, "__anext__"):
            try:
                return await result.__anext__()
            except StopAsyncIteration:
                return _DONE
        if hasattr(result, "__next__"):
            try:
                return next(result)
            except StopIteration:
                return _DONE
        return _DONE


class _CallerThread:
    """Executes calls on the thread that called ``build_sequential``, on
    behalf of the engine coroutine running in the helper loop."""

    def __init__(self) -> None:
        self._calls: queue.Queue[
            tuple[Callable[[], Any], concurrent.futures.Future[Any]]
        ] = queue.Queue()

    async def call(self, fn: Callable[[], Any]) -> Any:
        future: concurrent.futures.Future[Any] = concurrent.futures.Future()
        self._calls.put((fn, future))
        return await asyncio.wrap_future(future)

    def serve_until(self, done: concurrent.futures.Future[Any]) -> None:
        while not done.done():
            try:
                fn, future = self._calls.get(timeout=0.02)
            except queue.Empty:
                continue
            try:
                future.set_result(fn())
            except BaseException as e:  # delivered to the engine, not raised here
                future.set_exception(e)


class _SyncRunner:
    """Task code on the calling thread (``build_sequential``)."""

    def __init__(
        self, caller: _CallerThread, dual_run_default: Literal["sync", "async"]
    ) -> None:
        self.caller = caller
        self.dual_run_default = dual_run_default

    async def start(self, task: BaseTask) -> Any:
        has_run, has_run_aio = _has_custom_run(task), _has_custom_run_aio(task)
        use_async = (has_run_aio and not has_run) or (
            has_run and has_run_aio and self.dual_run_default == "async"
        )
        if use_async:
            return await self.caller.call(lambda: asyncio.run(task.run_aio()))
        if not has_run:
            raise ValueError(f"Task {task} has no run method")
        return await self.caller.call(task.run)

    async def step(self, result: Any) -> Any:
        if not hasattr(result, "__next__"):
            return _DONE

        def _next() -> Any:
            try:
                return next(result)
            except StopIteration:
                return _DONE

        return await self.caller.call(_next)


class _SequentialEngine:
    """The one sequential algorithm; see the module docstring."""

    def __init__(
        self,
        roots: list[BaseTask],
        *,
        session: ResidentSession,
        runner: _Runner,
        fail_mode: FailMode,
        register_all: bool,
        max_concurrent_discover: int,
    ) -> None:
        self.roots = roots
        self.session = session
        self.runner = runner
        self.fail_mode = fail_mode
        self.register_all = register_all
        self.max_concurrent_discover = max_concurrent_discover
        self.count = TaskCount()
        self.walk = Walk(roots=roots)
        self.tasks: dict[UUID, BaseTask] = {}
        self.completed: set[UUID] = set()
        self.failed: set[UUID] = set()

    def _add(self, walk: Walk) -> None:
        self.walk.complete.update(walk.complete)
        self.walk.deps.update(walk.deps)
        self.walk.observed_at.update(walk.observed_at)
        for task in walk.order:
            if task.id in self.tasks:
                continue
            self.walk.order.append(task)
            self.tasks[task.id] = task
            self.count.discovered += 1
            if walk.complete[task.id]:
                self.completed.add(task.id)
                self.count.previously_completed += 1

    def _deps(self, task: BaseTask) -> list[BaseTask]:
        return self.walk.deps.get(task.id, [])

    def _find_ready(self) -> BaseTask | None:
        for task in self.tasks.values():
            if task.id in self.completed or task.id in self.failed:
                continue
            deps = self._deps(task)
            if any(d.id in self.failed for d in deps):
                continue
            if all(d.id in self.completed for d in deps):
                return task
        return None

    def _check_deadlock(self) -> None:
        stuck = [
            t
            for t in self.tasks.values()
            if t.id not in self.completed
            and t.id not in self.failed
            and not any(d.id in self.failed for d in self._deps(t))
        ]
        if stuck:
            raise RuntimeError(
                f"Deadlock: {len(stuck)} tasks cannot proceed. "
                f"Tasks: {[str(t.id) for t in stuck[:5]]}"
            )

    async def _walk_more(self, parent: BaseTask, deps: list[BaseTask]) -> list:
        parent_path = self.walk.seen.path_of(parent.id) or extend_path(None, parent)
        known = set(self.tasks)
        walk = await walk_aio(
            deps,
            max_concurrent_discover=self.max_concurrent_discover,
            # The round trip guards registry rehydration; nothing is
            # registered without a registry.
            check_stability=self.session.enabled,
            seen=self.walk.seen,
            prior=self.walk,
            root_path=parent_path,
        )
        self._add(walk)
        return yield_batches(walk, deps, suspend=False, known=known)

    async def _execute(self, task: BaseTask) -> None:
        """Run ``task`` (its not-yet-built upstreams first), under a claim."""
        for dep in self._deps(task):
            if dep.id not in self.completed:
                await self._execute(dep)
        outcome = await self.session.claim(
            task,
            claim_ttl_seconds=self.session.claim_config.in_process_ttl_seconds,
            executor_metadata=None,
        )
        if outcome.kind == "completed":
            self.completed.add(task.id)
            self.count.previously_completed += 1
            return
        if outcome.kind != "granted":
            raise RuntimeError(outcome.message)
        execution_id = outcome.execution_id
        try:
            async with self.session.renewal(task, execution_id):
                result = await self.runner.start(task)
                while (yielded := await self.runner.step(result)) is not _DONE:
                    deps = flatten_task_struct(typing.cast(TaskStruct, yielded))
                    batches = await self._walk_more(task, deps)
                    await self.session.send_yield(task, execution_id, batches)
                    for dep in deps:
                        if dep.id not in self.completed:
                            await self._execute(dep)
        except BaseException as e:
            await self.session.fail(task, execution_id, f"{type(e).__name__}: {e}")
            raise
        await self.session.complete(task, execution_id)
        self.completed.add(task.id)
        self.count.succeeded += 1

    async def run(
        self, *, resume_build_id: UUID | None, description: str | None
    ) -> BuildSummary:
        session = self.session
        token = None
        error: BaseException | None = None
        try:
            walk = await walk_aio(
                self.roots,
                max_concurrent_discover=self.max_concurrent_discover,
                check_stability=session.enabled,
                register_all=self.register_all,
            )
            self.walk.seen = walk.seen
            self._add(walk)
            await session.open(
                self.roots, resume_build_id=resume_build_id, description=description
            )
            await session.register(walk)
            if session.build_id is not None:
                token = current_build_context_var.set(
                    BuildContext(
                        build_id=session.build_id,
                        plan_id=session.plan_id,
                        deployment_id=session.deployment_id,
                        settings=session.settings,
                    )
                )
            while (task := self._find_ready()) is not None:
                try:
                    await self._execute(task)
                except Exception as e:
                    self.failed.add(task.id)
                    self.count.failed += 1
                    error = e
                    if self.fail_mode == FailMode.FAIL_FAST:
                        raise
            self._check_deadlock()
            # Completion is verified by the registry; a refusal fails the
            # build below.
            await self._finish(error)
        except Exception as e:
            await self._fail_best_effort(e)
            if self.fail_mode == FailMode.FAIL_FAST:
                raise
            return self._summary(BuildExitStatus.FAILURE, e)
        finally:
            if token is not None:
                current_build_context_var.reset(token)
        return self._summary(
            BuildExitStatus.SUCCESS if error is None else BuildExitStatus.FAILURE,
            error,
        )

    async def _finish(self, error: BaseException | None) -> None:
        if error is not None:
            await self.session.skip_blocked()
        await self.session.finish(error)

    async def _fail_best_effort(self, error: BaseException) -> None:
        try:
            await self._finish(error)
        except Exception as e:
            handle_registry_error(
                e,
                "Failed to record the build's failure",
                self.session.on_registry_failure,
            )

    def _summary(
        self, status: BuildExitStatus, error: BaseException | None
    ) -> BuildSummary:
        return BuildSummary(
            status=status,
            task_count=self.count,
            build_id=self.session.build_id,
            error=error,
        )


def _roots(tasks: Sequence[BaseTask] | BaseTask) -> list[BaseTask]:
    if isinstance(tasks, BaseTask):
        return [tasks]
    roots = list(tasks)
    for index, task in enumerate(roots):
        if not isinstance(task, BaseTask):
            raise ValueError(
                f"Invalid task at index {index}: {task} (must be BaseTask)"
            )
    return roots


def _session(
    registry: RegistryABC | None,
    *,
    on_registry_failure: OnRegistryFailure,
    claim_config: ClaimConfig | None,
    settings: dict[str, str],
    limit_key_selector: LimitKeySelector | None,
) -> ResidentSession:
    return ResidentSession(
        registry if registry is not None else registry_provider.get(),
        on_registry_failure=on_registry_failure,
        claim_config=claim_config,
        settings=settings,
        limit_key_selector=limit_key_selector,
    )


def build_sequential(
    tasks: Sequence[BaseTask] | BaseTask,
    registry: RegistryABC | None = None,
    fail_mode: FailMode = FailMode.FAIL_FAST,
    dual_run_default: Literal["sync", "async"] = "sync",
    resume_build_id: UUID | None = None,
    register_all: bool = False,
    on_registry_failure: OnRegistryFailure = "raise",
    claim_config: ClaimConfig | None = None,
    settings: dict[str, str] | None = None,
    limit_key_selector: LimitKeySelector | None = None,
    description: str | None = None,
    max_concurrent_discover: int = 16,
) -> BuildSummary:
    """Build tasks sequentially, from synchronous code (for debugging).

    Task code runs on the calling thread: sync tasks via ``run()``,
    async-only tasks via ``asyncio.run(run_aio())`` (so not from inside a
    running event loop), dual tasks by ``dual_run_default``. See
    :func:`build_sequential_aio` for the other arguments.
    """
    roots = _roots(tasks)
    checked = validate_settings(settings)
    caller = _CallerThread()
    engine = _SequentialEngine(
        roots,
        session=_session(
            registry,
            on_registry_failure=on_registry_failure,
            claim_config=claim_config,
            settings=checked,
            limit_key_selector=limit_key_selector,
        ),
        runner=_SyncRunner(caller, dual_run_default),
        fail_mode=fail_mode,
        register_all=register_all,
        max_concurrent_discover=max_concurrent_discover,
    )
    with resident_settings(checked):
        loop = asyncio.new_event_loop()
        thread = threading.Thread(
            target=loop.run_forever, name="stardag-sequential", daemon=True
        )
        thread.start()
        try:
            done = asyncio.run_coroutine_threadsafe(
                engine.run(resume_build_id=resume_build_id, description=description),
                loop,
            )
            try:
                caller.serve_until(done)
            except BaseException:
                done.cancel()
                raise
            return done.result()
        finally:
            loop.call_soon_threadsafe(loop.stop)
            thread.join()
            loop.close()


async def build_sequential_aio(
    tasks: Sequence[BaseTask] | BaseTask,
    registry: RegistryABC | None = None,
    fail_mode: FailMode = FailMode.FAIL_FAST,
    sync_run_default: Literal["thread", "blocking"] = "blocking",
    resume_build_id: UUID | None = None,
    register_all: bool = False,
    on_registry_failure: OnRegistryFailure = "raise",
    claim_config: ClaimConfig | None = None,
    settings: dict[str, str] | None = None,
    limit_key_selector: LimitKeySelector | None = None,
    description: str | None = None,
    max_concurrent_discover: int = 16,
) -> BuildSummary:
    """Build tasks sequentially from async code (for debugging).

    Args:
        tasks: The root task(s).
        registry: Default: the configured registry (none: no plan, no
            claims).
        fail_mode: ``FAIL_FAST`` (default) or ``CONTINUE``.
        sync_run_default: How a sync-only task runs: ``"blocking"`` on the
            loop, or ``"thread"``.
        resume_build_id: Resume this build (its plan for this scope is
            reused, observations re-sent, failed members reset).
        register_all: Expand complete tasks too.
        on_registry_failure: ``"raise"`` or ``"warn"`` (outages only).
        claim_config: Claim waiting and renewal.
        settings: Environment variables applied for the build's duration.
        limit_key_selector: Registry concurrency-limit keys per task, sent
            with its claim.
        description: A description for a new build.
        max_concurrent_discover: Completion checks in flight while walking.
    """
    roots = _roots(tasks)
    checked = validate_settings(settings)
    engine = _SequentialEngine(
        roots,
        session=_session(
            registry,
            on_registry_failure=on_registry_failure,
            claim_config=claim_config,
            settings=checked,
            limit_key_selector=limit_key_selector,
        ),
        runner=_AsyncRunner(sync_run_default),
        fail_mode=fail_mode,
        register_all=register_all,
        max_concurrent_discover=max_concurrent_discover,
    )
    with resident_settings(checked):
        return await engine.run(
            resume_build_id=resume_build_id, description=description
        )


__all__ = [
    "build_sequential",
    "build_sequential_aio",
]
