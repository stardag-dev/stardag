"""The resident concurrent engine: ``build_aio``.

One process drives the whole build: it walks the DAG, registers the plan,
and schedules every task whose dependencies are complete, concurrently, on
a :class:`~stardag.build.TaskExecutorABC`. With a registry, every execution
claims (D11) and its reports name that execution; the registry side lives
in :class:`~stardag.build._session.ResidentSession`, shared with the
sequential engine.

Dynamic dependencies: a task that yields incomplete dependencies is
suspended until they are built. In-process, its generator waits and its
claim is kept (renewed; the yield is sent with ``suspend: false``); a
detached execution's container exits, so its yield suspends the task and a
later submission claims it again as a new execution.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import traceback as tb_module
from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from stardag import BaseTask, TaskStruct, flatten_task_struct
from stardag._core.instance import extend_path
from stardag.build._base import (
    BuildContext,
    BuildExitStatus,
    BuildSummary,
    ClaimConfig,
    DetachedHandle,
    FailMode,
    OnRegistryFailure,
    TaskCount,
    TaskExecutionError,
    TaskExecutionState,
    TaskExecutorABC,
    current_build_context_var,
    handle_registry_error,
)
from stardag.build._claims import claim_ttl_seconds
from stardag.build._concurrency import (
    ConcurrencyConfig,
    ConcurrencyLimiter,
    build_concurrency_limiter,
)
from stardag.build._registration import Walk, walk_aio, yield_batches
from stardag.build._session import ClaimRenewal, LimitKeySelector, ResidentSession
from stardag.build._settings import resident_settings, validate_settings
from stardag.build._wakeups import drain_wake_candidates
from stardag.exceptions import ExecutionCancelled
from stardag.registry import RegistryABC, registry_provider

logger = logging.getLogger(__name__)

# Minimum spacing between a hybrid build's cross-build drains.
_RESIDENT_DRAIN_INTERVAL_SECONDS = 5.0


class _Skipped(Exception):
    """State marker for a task that never ran because an upstream failed.
    Never raised."""


@dataclass(frozen=True)
class _NotRun:
    """A submission that did not execute the task: the claim found it
    complete (``completed``), or could not be won (``error``)."""

    completed: bool
    error: BaseException | None = None


def _error(e: BaseException) -> TaskExecutionError:
    return TaskExecutionError(
        exception=e, traceback="".join(tb_module.format_exception(e))
    )


def _merge(into: Walk, walk: Walk) -> None:
    """Fold ``walk``'s results into the build's cumulative walk (reused as
    ``prior`` by later walks)."""
    into.complete.update(walk.complete)
    into.deps.update(walk.deps)
    into.observed_at.update(walk.observed_at)
    known = {t.id for t in into.order}
    into.order.extend(t for t in walk.order if t.id not in known)


async def build_aio(
    tasks: Sequence[BaseTask] | BaseTask,
    task_executor: TaskExecutorABC | None = None,
    fail_mode: FailMode = FailMode.FAIL_FAST,
    registry: RegistryABC | None = None,
    max_concurrent_discover: int = 50,
    resume_build_id: UUID | None = None,
    register_all: bool = False,
    on_registry_failure: OnRegistryFailure = "raise",
    concurrency_config: ConcurrencyConfig | None = None,
    concurrency_limiter: ConcurrencyLimiter | None = None,
    claim_config: ClaimConfig | None = None,
    settings: dict[str, str] | None = None,
    limit_key_selector: LimitKeySelector | None = None,
    description: str | None = None,
) -> BuildSummary:
    """Build tasks concurrently using hybrid async/thread/process execution.

    Walks the DAG from the roots (stopping at complete tasks), registers the
    plan with the registry — roots first, the rest in post-order, then
    sealed — and runs every task whose dependencies are met on
    ``task_executor``. With a registry every execution claims, so two
    builds (or a build and a reactive one) never run a task twice at once.

    Args:
        tasks: The root task(s).
        task_executor: Where tasks run (default:
            :class:`HybridConcurrentTaskExecutor`). An executor running tasks
            on a deployed Modal app makes the build plan under that app's
            current deployment (D13).
        fail_mode: Stop at the first failure (``FAIL_FAST``) or run
            everything whose dependencies are met (``CONTINUE``).
        registry: Default: the configured registry (none: no plan, no
            claims — the build runs purely locally).
        max_concurrent_discover: Completion checks in flight while walking.
        resume_build_id: Resume this build: its plan for this scope is
            reused, observations re-sent, and failed members reset.
        register_all: Expand complete tasks too, so every edge is recorded.
        on_registry_failure: ``"raise"`` (default) or ``"warn"`` — carry on
            through a registry *outage*; a refusal always raises.
        concurrency_config / concurrency_limiter: Build-local limits.
        claim_config: How claims are waited on and renewed.
        settings: Environment variables applied for the build's duration
            (the scope's second half; ``STARDAG_*`` / ``MODAL_*`` refused).
            Two concurrent builds in one process with different settings
            are refused.
        limit_key_selector: The registry concurrency-limit keys a task runs
            under, sent with its claim.
        description: A description for a new build.

    Returns:
        BuildSummary with status, task counts and build id.
    """
    roots = [tasks] if isinstance(tasks, BaseTask) else list(tasks)
    for index, task in enumerate(roots):
        if not isinstance(task, BaseTask):
            raise ValueError(
                f"Invalid task at index {index}: {task} (must be BaseTask)"
            )
    checked_settings = validate_settings(settings)
    with resident_settings(checked_settings):
        engine = _ResidentEngine(
            roots,
            task_executor=task_executor or _default_executor(),
            fail_mode=fail_mode,
            session=ResidentSession(
                registry if registry is not None else registry_provider.get(),
                on_registry_failure=on_registry_failure,
                claim_config=claim_config,
                settings=checked_settings,
                limit_key_selector=limit_key_selector,
            ),
            max_concurrent_discover=max_concurrent_discover,
            register_all=register_all,
            limiter=build_concurrency_limiter(concurrency_config, concurrency_limiter),
        )
        return await engine.run(
            resume_build_id=resume_build_id, description=description
        )


def _default_executor() -> TaskExecutorABC:
    from stardag.build._concurrent import HybridConcurrentTaskExecutor

    return HybridConcurrentTaskExecutor()


class _ResidentEngine:
    """The state and loop of one ``build_aio`` call."""

    def __init__(
        self,
        roots: list[BaseTask],
        *,
        task_executor: TaskExecutorABC,
        fail_mode: FailMode,
        session: ResidentSession,
        max_concurrent_discover: int,
        register_all: bool,
        limiter: ConcurrencyLimiter,
    ) -> None:
        self.roots = roots
        self.executor = task_executor
        self.fail_mode = fail_mode
        self.session = session
        self.max_concurrent_discover = max_concurrent_discover
        self.register_all = register_all
        self.limiter = limiter
        self.count = TaskCount()
        self.states: dict[UUID, TaskExecutionState] = {}
        self.walk: Walk | None = None
        self.executing: set[UUID] = set()
        self.pending: dict[UUID, asyncio.Task] = {}
        self.renewals: dict[UUID, ClaimRenewal] = {}
        self.error: BaseException | None = None
        self.fail_fast_triggered = False
        self._last_drain = float("-inf")

    # -- discovery ------------------------------------------------------------------

    def _add_walk(self, walk: Walk) -> None:
        for task in walk.order:
            if task.id in self.states:
                continue
            state = TaskExecutionState(
                task=task, static_deps=walk.deps.get(task.id, [])
            )
            if walk.complete[task.id]:
                state.completed = True
                self.count.previously_completed += 1
            self.states[task.id] = state
            self.count.discovered += 1

    async def discover(self) -> None:
        """Walk the roots' DAG. Before anything is sent: an instance conflict
        or an unstable body fails here, with no build created for it."""
        walk = await walk_aio(
            self.roots,
            max_concurrent_discover=self.max_concurrent_discover,
            # The round trip guards registry rehydration; nothing is
            # registered without a registry.
            check_stability=self.session.enabled,
            register_all=self.register_all,
        )
        self.walk = walk
        self._add_walk(walk)

    async def discover_dynamic(self, parent: BaseTask, deps: list[BaseTask]) -> list:
        """Walk a yield's dependencies under the build's walk; returns the
        yield's batches (what the registry is told)."""
        assert self.walk is not None
        parent_path = self.walk.seen.path_of(parent.id) or extend_path(None, parent)
        walk = await walk_aio(
            deps,
            max_concurrent_discover=self.max_concurrent_discover,
            check_stability=self.session.enabled,
            seen=self.walk.seen,
            prior=self.walk,
            root_path=parent_path,
        )
        known = {t.id for t in self.walk.order}
        _merge(self.walk, walk)
        self._add_walk(walk)
        return yield_batches(
            walk,
            deps,
            suspend=self.executor.supports_detached(parent),
            known=known,
        )

    # -- execution ----------------------------------------------------------------

    async def _metadata(self, task: BaseTask) -> dict | None:
        try:
            return await self.executor.get_executor_metadata(task)
        except Exception:
            logger.debug(f"Executor metadata failed for {task.id}", exc_info=True)
            return None

    async def submit(
        self, task: BaseTask
    ) -> TaskExecutionError | TaskStruct | _NotRun | None:
        """Claim (unless this execution already holds the claim across an
        in-process yield), then execute."""
        state = self.states[task.id]
        detached = self.executor.supports_detached(task)
        if state.execution_id is None:
            state.waiting_on_claim = True
            try:
                outcome = await self.session.claim(
                    task,
                    claim_ttl_seconds=(
                        claim_ttl_seconds(task, self.executor)
                        if detached
                        else self.session.claim_config.in_process_ttl_seconds
                    ),
                    executor_metadata=await self._metadata(task),
                )
            finally:
                state.waiting_on_claim = False
            if outcome.kind == "completed":
                return _NotRun(completed=True)
            if outcome.kind != "granted":
                return _NotRun(completed=False, error=RuntimeError(outcome.message))
            state.execution_id = outcome.execution_id
        async with contextlib.AsyncExitStack() as stack:
            try:
                await stack.enter_async_context(self.limiter.slot(task))
            except Exception as e:
                return _error(e)
            if detached:
                return await self._run_detached(task, state)
            renewal = self.renewals.get(task.id)
            if renewal is None:
                renewal = self.session.renewal(task, state.execution_id)
                renewal.start()
                self.renewals[task.id] = renewal
            return await self.executor.submit(task)

    async def _run_detached(
        self, task: BaseTask, state: TaskExecutionState
    ) -> TaskExecutionError | TaskStruct | _NotRun | None:
        assert state.execution_id is not None
        try:
            handle: DetachedHandle = await self.executor.submit_detached(
                task, execution_id=state.execution_id
            )
        except Exception as e:
            return _error(e)
        if not await self.session.start_ref(task, state.execution_id, handle):
            logger.warning(
                f"Task {task.id} stopped being this build's while its execution "
                f"was being started; stopping execution {handle.ref!r}."
            )
            try:
                await self.executor.cancel_detached(task, handle.executor, handle.ref)
            except Exception as e:
                logger.warning(f"Could not stop orphaned execution {handle.ref!r}: {e}")
            state.execution_id = None
            return _NotRun(
                completed=False,
                error=RuntimeError(
                    "The claim moved on while the execution was being started; "
                    "nothing was recorded against the task."
                ),
            )
        return await handle.wait()

    async def _stop_renewal(self, task_id: UUID) -> None:
        renewal = self.renewals.pop(task_id, None)
        if renewal is not None:
            await renewal.stop()

    def _record_failure(
        self, state: TaskExecutionState, failure: BaseException
    ) -> None:
        state.exception = failure
        self.count.failed += 1
        self.error = failure
        if self.fail_mode == FailMode.FAIL_FAST:
            self.fail_fast_triggered = True

    async def process_result(
        self,
        task: BaseTask,
        result: TaskExecutionError | BaseException | TaskStruct | _NotRun | None,
    ) -> None:
        state = self.states[task.id]
        reports = self.executor.reports_lifecycle(task)
        if isinstance(result, _NotRun):
            await self._stop_renewal(task.id)
            state.execution_id = None
            if result.completed:
                state.completed = True
                self.count.previously_completed += 1
            else:
                assert result.error is not None
                self._record_failure(state, result.error)
            return

        if isinstance(result, (TaskExecutionError, BaseException)):
            failure = (
                result.exception if isinstance(result, TaskExecutionError) else result
            )
            await self._stop_renewal(task.id)
            execution_id, state.execution_id = state.execution_id, None
            if isinstance(failure, ExecutionCancelled):
                # The worker stopped itself at a cooperative checkpoint: the
                # task is cancelled or somebody else's. Record nothing.
                logger.info(
                    f"Execution of task {task.id} stopped at a cooperative "
                    "cancellation checkpoint; recording nothing."
                )
            else:
                # A self-reporting worker reports its own failure; this one
                # is the fallback for a worker that died first (a second
                # report of one execution is recorded and refused).
                await self.session.fail(task, execution_id, str(result))
            self._record_failure(state, failure)
            return

        if result is None:
            await self._stop_renewal(task.id)
            execution_id, state.execution_id = state.execution_id, None
            if not reports:
                await self.session.complete(task, execution_id)
            state.completed = True
            self.count.succeeded += 1
            return

        # Dynamic dependencies.
        deps = flatten_task_struct(result)
        try:
            batches = await self.discover_dynamic(task, deps)
            if not reports:
                await self.session.send_yield(task, state.execution_id, batches)
        except Exception as e:
            # A failure to register a yield fails the task — never a parent
            # suspended on children the registry did not see (S13).
            await self._stop_renewal(task.id)
            execution_id, state.execution_id = state.execution_id, None
            await self.session.fail(task, execution_id, f"{type(e).__name__}: {e}")
            self._record_failure(state, e)
            return
        if self.executor.supports_detached(task):
            # The container exited; its yield suspended the task and released
            # the claim. The next submission claims it as a new execution.
            state.execution_id = None
        known = {d.id for d in state.dynamic_deps}
        state.dynamic_deps.extend(d for d in deps if d.id not in known)

    def find_ready(self) -> list[BaseTask]:
        ready: list[BaseTask] = []
        for state in self.states.values():
            if state.completed or state.exception is not None:
                continue
            if state.task.id in self.executing:
                continue
            if all(self.states[d.id].completed for d in state.all_deps):
                ready.append(state.task)
                self.executing.add(state.task.id)
        return ready

    # -- failure handling ------------------------------------------------------------

    async def cancel_in_flight(self) -> None:
        """Stop the still-running executions after a FAIL_FAST failure. The
        build's failure releases their claims server-side."""
        if not self.pending:
            return
        snapshot = dict(self.pending)
        self.pending.clear()
        for task_id, future in snapshot.items():
            if future.done():
                self.executing.discard(task_id)
                try:
                    done = future.result()
                except Exception as e:
                    done = _error(e)
                await self.process_result(self.states[task_id].task, done)
        running = [tid for tid, f in snapshot.items() if not f.done()]
        for task_id in running:
            snapshot[task_id].cancel()
        for task_id in running:
            try:
                await self.executor.cancel(self.states[task_id].task)
            except Exception as e:
                logger.warning(f"Executor cancel failed for {task_id}: {e}")
        await asyncio.gather(*(snapshot[t] for t in running), return_exceptions=True)
        for task_id in running:
            self.executing.discard(task_id)
            await self._stop_renewal(task_id)
            state = self.states[task_id]
            state.execution_id = None
            if state.exception is None:
                state.exception = asyncio.CancelledError(
                    "Cancelled by build engine in FAIL_FAST mode"
                )
            self.count.cancelled += 1

    def mark_skipped(self) -> None:
        """Tasks blocked by a failed or cancelled upstream, to a fixed point."""
        while True:
            skipped = 0
            for state in self.states.values():
                if state.completed or state.exception is not None:
                    continue
                if any(self.states[d.id].exception is not None for d in state.all_deps):
                    state.exception = _Skipped()
                    self.count.skipped += 1
                    skipped += 1
            if not skipped:
                return

    # -- cross-build wake-ups (hybrid builds) ----------------------------------------

    async def drain_neighbours(self, *, force: bool = False) -> None:
        if not self.session.enabled or not self.executor.can_spawn_scheduler_ticks():
            return
        now = asyncio.get_running_loop().time()
        if not force and now - self._last_drain < _RESIDENT_DRAIN_INTERVAL_SECONDS:
            return
        self._last_drain = now
        await drain_wake_candidates(
            self.session.registry,
            self.executor.spawn_scheduler_tick,
            build_id=self.session.build_id,
        )

    # -- the loop ---------------------------------------------------------------------

    async def run(
        self, *, resume_build_id: UUID | None, description: str | None
    ) -> BuildSummary:
        session = self.session
        token = None
        try:
            try:
                await self.discover()
                await session.open(
                    self.roots,
                    app_name=self.executor.deployment_app_name(),
                    resume_build_id=resume_build_id,
                    description=description,
                )
                assert self.walk is not None
                await session.register(self.walk)
                if session.build_id is not None:
                    token = current_build_context_var.set(
                        BuildContext(
                            build_id=session.build_id,
                            plan_id=session.plan_id,
                            deployment_id=session.deployment_id,
                            settings=session.settings,
                        )
                    )
                await self.executor.setup()
                try:
                    await self._loop()
                finally:
                    await self.executor.teardown()
                if self.error is not None and self.fail_mode == FailMode.FAIL_FAST:
                    raise self.error
                # Completion is verified by the registry (the plan sealed, every
                # member COMPLETED); a refusal fails the build below.
                await self._finish(self.error)
            except Exception as e:
                try:
                    await self.cancel_in_flight()
                    self.mark_skipped()
                except Exception as cleanup_err:
                    logger.warning(f"Error during build cleanup: {cleanup_err}")
                await self._fail_best_effort(e)
                if self.fail_mode == FailMode.FAIL_FAST:
                    raise
                return self._summary(BuildExitStatus.FAILURE, e)
            return self._summary(
                BuildExitStatus.SUCCESS
                if self.error is None
                else BuildExitStatus.FAILURE,
                self.error,
            )
        finally:
            for task_id in list(self.renewals):
                await self._stop_renewal(task_id)
            if token is not None:
                current_build_context_var.reset(token)

    async def _finish(self, error: BaseException | None) -> None:
        """Complete the build, or fail it with ``error`` (skipping the
        members a failure blocks first)."""
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

    async def _loop(self) -> None:
        while True:
            if all(self.states[r.id].completed for r in self.roots):
                break
            for task in self.find_ready():
                self.pending[task.id] = asyncio.create_task(self.submit(task))
            if not self.pending:
                blocked = [
                    s
                    for s in self.states.values()
                    if not s.completed and s.exception is None
                ]
                stuck = [
                    s
                    for s in blocked
                    if not any(
                        self.states[d.id].exception is not None for d in s.all_deps
                    )
                ]
                if stuck:
                    raise RuntimeError(
                        f"Deadlock: {len(stuck)} tasks cannot proceed. "
                        f"Tasks: {[s.task.id for s in stuck[:5]]}"
                    )
                break
            done, _ = await asyncio.wait(
                self.pending.values(), return_when=asyncio.FIRST_COMPLETED
            )
            for future in done:
                task_id = next(t for t, f in self.pending.items() if f is future)
                del self.pending[task_id]
                self.executing.discard(task_id)
                try:
                    result = future.result()
                except Exception as e:
                    result = _error(e)
                await self.process_result(self.states[task_id].task, result)
            await self.drain_neighbours()
            if self.fail_fast_triggered and self.fail_mode == FailMode.FAIL_FAST:
                break
        if self.fail_fast_triggered and self.fail_mode == FailMode.FAIL_FAST:
            await self.cancel_in_flight()
        self.mark_skipped()
        await self.drain_neighbours(force=True)
