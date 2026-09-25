"""A resident build's registry session: its build, plan, claims and reports.

Both resident engines (``build_aio`` and ``build_sequential_aio``) drive the
registry through one :class:`ResidentSession`, so they register, claim,
yield and report in one order (design.md: "Both engines use this one route
in this one order — v1 had them in different orders"):

1. :meth:`open` resolves the deployment the build plans under (see
   :mod:`stardag.build._deployment`) and creates the build — or resumes it.
2. :meth:`register` runs the static phase for the engine's walk: roots
   first, members in post-order chunks, ``/seal``. A resume reuses the plan
   of the same scope and re-sends the observations, and resets members that
   failed before.
3. Per execution: :meth:`claim` (every execution claims, D11 — with a TTL,
   renewed by :class:`ClaimRenewal` while an in-process execution runs),
   then the reports naming that execution: :meth:`start_ref`,
   :meth:`complete`, :meth:`fail`, :meth:`send_yield`.
4. :meth:`finish` completes or fails the build; the registry releases the
   claims its plans hold.

A build the registry has stopped — an operator's ``cancel``, or any other
terminal status — refuses the next claiming start ``build_not_running``
(:meth:`claim` answers ``build_stopped``), and refuses a later lifecycle
report ``build_terminal`` (:meth:`finish` returns the status it found): a
terminal build status is sticky, and the engine stops without writing one.

Without a registry (:class:`~stardag.registry.NoOpRegistry`) the session is
disabled: no build, no plan, no claims, and every method is a no-op (D11).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import typing
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from uuid import UUID

from stardag._core.base_task import BaseTask
from stardag.build._base import (
    ClaimConfig,
    DetachedHandle,
    ExecutorDetails,
    OnRegistryFailure,
    describe_task,
    handle_registry_error,
)
from stardag.build._deployment import resolve_deployment_id_aio
from stardag.build._registration import (
    Walk,
    YieldBatch,
    new_id,
    register_plan_aio,
    send_yield_aio,
)
from stardag.exceptions import EXECUTION_OVER_CODES, APIError
from stardag.registry import RegistryABC, is_noop_registry

logger = logging.getLogger(__name__)

LimitKeySelector = Callable[[BaseTask], Sequence[str]]

#: Refusals of a report that say the execution is already over — its claim
#: moved on (to another execution, or to another plan: ``not_claim_holder``),
#: the ledger has no such execution, or its end was already recorded.
#: Nothing for the engine to do; a detached ref refused this way is an orphan.
_LATE_REPORT_CODES = EXECUTION_OVER_CODES | {"execution_already_ended"}

#: Claim refusals that mean "someone else, wait": another execution holds
#: the claim, an upstream is not COMPLETED in the registry yet, or a
#: concurrency limit on the claim's keys is full.
_WAIT_CODES = frozenset(
    {"task_already_running", "upstream_incomplete", "concurrency_limit_reached"}
)


@dataclass(frozen=True)
class ClaimOutcome:
    """What :meth:`ResidentSession.claim` decided.

    ``kind``: ``granted`` (``execution_id`` holds the claim),
    ``completed`` (the task is complete — nothing to run), ``timeout`` (the
    claim stayed held elsewhere past ``ClaimConfig.wait_timeout_seconds``),
    ``build_stopped`` (the build is no longer RUNNING: it hands out no more
    work, and the engine stops) or ``refused`` (a refusal that cannot be
    waited out, ``message`` says which).
    """

    kind: typing.Literal["granted", "completed", "timeout", "build_stopped", "refused"]
    execution_id: UUID | None = None
    message: str | None = None


class ResidentSession:
    """See the module docstring."""

    def __init__(
        self,
        registry: RegistryABC,
        *,
        on_registry_failure: OnRegistryFailure = "raise",
        claim_config: ClaimConfig | None = None,
        settings: Mapping[str, str] | None = None,
        limit_key_selector: LimitKeySelector | None = None,
    ) -> None:
        self.registry = registry
        self.enabled = not is_noop_registry(registry)
        self.on_registry_failure: OnRegistryFailure = on_registry_failure
        self.claim_config = claim_config or ClaimConfig()
        self.settings = dict(settings or {})
        self.limit_key_selector = limit_key_selector
        self.build_id: UUID | None = None
        self.plan_id: UUID | None = None
        self.deployment_id: UUID | None = None
        self.resumed = False

    # -- the build ----------------------------------------------------------------

    async def open(
        self,
        roots: Sequence[BaseTask],
        *,
        app_name: str | None = None,
        resume_build_id: UUID | None = None,
        description: str | None = None,
    ) -> None:
        """Resolve the deployment and create (or resume) the build.

        ``app_name`` is the deployed app the build's tasks run on, if any
        (``TaskExecutorABC.deployment_app_name``): the build then plans
        under that app's current deployment (D13).
        """
        if not self.enabled:
            return
        try:
            self.deployment_id = await resolve_deployment_id_aio(
                self.registry, app_name=app_name
            )
            if resume_build_id is not None:
                result = await self.registry.build_resume_aio(
                    resume_build_id,
                    deployment_id=self.deployment_id,
                    settings=self.settings,
                )
                self.build_id = result.build.id
                self.resumed = True
                return
            build = await self.registry.build_create_aio(
                root_task_ids=[str(r.id) for r in roots],
                build_id=new_id(),
                description=description,
            )
            self.build_id = build.id
        except Exception as e:
            self._degrade(e, "Failed to create the build in the registry")

    def _degrade(self, error: Exception, message: str) -> None:
        """An outage in ``warn`` mode: carry on without the registry — no
        plan, no claims, no reports (a refusal, or ``raise`` mode,
        propagates)."""
        handle_registry_error(error, message, self.on_registry_failure)
        logger.warning("Continuing the build without the registry.")
        self.enabled = False

    async def register(self, walk: Walk) -> None:
        """The static phase for ``walk`` (see
        :func:`~stardag.build._registration.register_plan_aio`)."""
        if not self.enabled:
            return
        assert self.build_id is not None and self.deployment_id is not None
        try:
            plan = await register_plan_aio(
                self.registry,
                self.build_id,
                walk,
                deployment_id=self.deployment_id,
                settings=self.settings,
                retry_failed=self.resumed,
            )
        except Exception as e:
            ids = ", ".join(str(t.id) for t in walk.order[:5])
            self._degrade(
                e,
                f"Failed to register the plan of build {self.build_id} "
                f"({len(walk.order)} tasks: {ids}{', ...' if len(walk.order) > 5 else ''})",
            )
            return
        self.plan_id = plan.id

    async def finish(
        self, error: BaseException | None, message: str | None = None
    ) -> str | None:
        """Complete the build, or fail it with ``message`` (default: the
        error's type and text). The registry releases the claims of the
        build's plans either way.

        Returns None, or — when the registry refused because the build is
        already terminal (``build_terminal``: cancelled by an operator, or
        ended by another hand) — the status it found, which stands."""
        if not self.enabled or self.build_id is None:
            return None
        try:
            if error is None:
                await self.registry.build_complete_aio(self.build_id)
            else:
                await self.registry.build_fail_aio(
                    self.build_id, message or f"{type(error).__name__}: {error}"
                )
        except APIError as e:
            if e.code != "build_terminal":
                raise
            status = str((e.payload or {}).get("build_status") or "terminal")
            logger.warning(
                f"Build {self.build_id} is already {status}; its status stands "
                "(the registry recorded this build's own report, not applied)."
            )
            return status
        return None

    async def skip_blocked(self) -> list[str] | None:
        """Mark members blocked by a failure SKIPPED (best-effort: the build
        is already failing). Returns the task ids the registry skipped —
        the plan's own count of what the failure blocks — or None when
        there is no registry or the call failed."""
        if not self.enabled or self.build_id is None:
            return None
        try:
            return list(await self.registry.build_skip_blocked_aio(self.build_id))
        except Exception as e:
            logger.warning(f"Could not mark blocked members skipped: {e}")
            return None

    # -- claims -----------------------------------------------------------------

    def limit_keys(self, task: BaseTask) -> list[str]:
        if self.limit_key_selector is None:
            return []
        return list(self.limit_key_selector(task))

    async def claim(
        self,
        task: BaseTask,
        *,
        claim_ttl_seconds: int | None,
        executor: ExecutorDetails | None = None,
    ) -> ClaimOutcome:
        """Claim ``task`` for a new execution, waiting out another holder.

        One execution id is minted per call and re-sent on every attempt:
        a retried granted claim is the same execution asking again and is
        granted, which is what makes a lost response survivable. While the
        claim is held elsewhere (or an upstream is not yet COMPLETED in the
        registry, or a limit is full) the target is polled and the claim
        re-asked with backoff; a lapsed claim is taken over server-side.
        """
        if not self.enabled:
            # Nothing to arbitrate against; the id still names the
            # execution locally (and to a detached worker, which then has
            # no plan to report to).
            return ClaimOutcome("granted", execution_id=new_id())
        assert self.plan_id is not None
        config = self.claim_config
        details = executor or ExecutorDetails()
        execution_id = new_id()
        limit_keys = self.limit_keys(task)
        loop = asyncio.get_running_loop()
        started = loop.time()
        interval = config.wait_initial_interval_seconds
        waited_reason: str | None = None
        while True:
            try:
                await self.registry.member_start_aio(
                    self.plan_id,
                    str(task.id),
                    execution_id=execution_id,
                    claim=True,
                    claim_ttl_seconds=claim_ttl_seconds,
                    executor=details.executor,
                    executor_ref=details.executor_ref,
                    executor_metadata=details.executor_metadata,
                    limit_keys=limit_keys,
                )
                return ClaimOutcome("granted", execution_id=execution_id)
            except APIError as e:
                if not 400 <= (e.status_code or 500) < 500:
                    # An outage, not a refusal: ``warn`` carries on
                    # unclaimed (its reports are then recorded as late).
                    handle_registry_error(
                        e, f"Claim failed for task {task.id}", self.on_registry_failure
                    )
                    return ClaimOutcome("granted", execution_id=execution_id)
                code = e.code
                if code == "task_already_completed":
                    return ClaimOutcome("completed")
                if code == "execution_superseded":
                    execution_id = new_id()
                    continue
                if code == "build_not_running":
                    status = (e.payload or {}).get("build_status") or "gone"
                    return ClaimOutcome(
                        "build_stopped",
                        message=(
                            f"Build {self.build_id} is no longer running "
                            f"({status}); it hands out no more work."
                        ),
                    )
                if code not in _WAIT_CODES:
                    return ClaimOutcome("refused", message=str(e))
                waited_reason = code
            except Exception as e:
                handle_registry_error(
                    e, f"Claim failed for task {task.id}", self.on_registry_failure
                )
                return ClaimOutcome("granted", execution_id=execution_id)
            timeout = config.wait_timeout_seconds
            if timeout is not None and loop.time() - started >= timeout:
                return ClaimOutcome(
                    "timeout",
                    message=(
                        f"Claim wait timed out after {timeout}s ({waited_reason}): "
                        "the task is still held by another execution, or waits "
                        "on an upstream or a concurrency limit."
                    ),
                )
            if await task.complete_aio():
                return ClaimOutcome("completed")
            await asyncio.sleep(interval)
            interval = min(
                interval * config.wait_backoff_factor, config.wait_max_interval_seconds
            )

    def renewal(self, task: BaseTask, execution_id: UUID | None) -> "ClaimRenewal":
        """A renewal loop for an in-process execution's claim (D11)."""
        return ClaimRenewal(self, task, execution_id)

    # -- reports ------------------------------------------------------------------

    async def _report(self, what: str, call: typing.Awaitable[typing.Any]) -> None:
        try:
            await call
        except APIError as e:
            if e.code in _LATE_REPORT_CODES:
                logger.info(f"{what}: the registry recorded a late report ({e.code}).")
                return
            handle_registry_error(e, what, self.on_registry_failure)
        except Exception as e:
            handle_registry_error(e, what, self.on_registry_failure)

    async def start_ref(
        self, task: BaseTask, execution_id: UUID | None, handle: DetachedHandle
    ) -> bool:
        """Record a detached execution's ref (a non-claiming start).

        Returns False when the registry refused because the claim has moved
        on from this execution — the spawned container is then an orphan,
        and the caller stops it.
        """
        if not self.enabled or execution_id is None:
            return True
        assert self.plan_id is not None
        try:
            await self.registry.member_start_aio(
                self.plan_id,
                str(task.id),
                execution_id=execution_id,
                claim=False,
                executor=handle.executor,
                executor_ref=handle.ref,
                executor_metadata=handle.executor_metadata,
            )
        except APIError as e:
            if e.code in _LATE_REPORT_CODES:
                return False
            handle_registry_error(
                e,
                f"Failed to record the ref of task {task.id}",
                self.on_registry_failure,
            )
        except Exception as e:
            handle_registry_error(
                e,
                f"Failed to record the ref of task {task.id}",
                self.on_registry_failure,
            )
        return True

    async def complete(self, task: BaseTask, execution_id: UUID | None) -> None:
        if not self.enabled or execution_id is None:
            return
        assert self.plan_id is not None
        await self._report(
            f"Failed to report completion of task {task.id}",
            self.registry.member_complete_aio(
                self.plan_id, str(task.id), execution_id=execution_id
            ),
        )
        try:
            artifacts = await task.artifacts_aio()
            if artifacts:
                await self.registry.task_upload_artifacts_aio(
                    self.plan_id, str(task.id), artifacts, execution_id=execution_id
                )
        except Exception as e:
            handle_registry_error(
                e,
                f"Failed to collect/upload artifacts for task {task.id}",
                "warn",
            )

    async def fail(
        self, task: BaseTask, execution_id: UUID | None, error_message: str
    ) -> None:
        if not self.enabled or execution_id is None:
            return
        assert self.plan_id is not None
        await self._report(
            f"Failed to report failure of task {task.id}",
            self.registry.member_fail_aio(
                self.plan_id,
                str(task.id),
                execution_id=execution_id,
                error_message=error_message,
            ),
        )

    async def send_yield(
        self,
        task: BaseTask,
        execution_id: UUID | None,
        batches: Sequence[YieldBatch],
    ) -> None:
        """Register a yield (children with their closure, then the dynamic
        edges). Not swallowed: a failure to register fails the task rather
        than leaving a parent waiting on children the registry never saw."""
        if not self.enabled or execution_id is None:
            return
        assert self.plan_id is not None and self.deployment_id is not None
        await send_yield_aio(
            self.registry,
            batches,
            plan_id=self.plan_id,
            task_id=str(task.id),
            execution_id=execution_id,
            deployment_id=self.deployment_id,
        )


def failure_message(
    failed: typing.Sequence[tuple[BaseTask, BaseException]],
    blocked: int | None,
) -> str:
    """The one-line reason a build writes on ``/fail`` for task failures:
    the first failed task (name and id) and its error, how many other tasks
    failed, and how many members the registry found blocked by them."""
    task, error = failed[0]
    text = " ".join(str(error).split()) or type(error).__name__
    message = f"Task {describe_task(task)} failed: {type(error).__name__}: {text}"
    if len(failed) > 1:
        message += f" (and {len(failed) - 1} more failed task(s))"
    if blocked is not None:
        message += f"; {blocked} downstream member(s) blocked"
    return message


def _lost_claim_reason(error: APIError) -> str:
    """Why a renewal was refused, from the claim's recorded outcome."""
    outcome = (error.payload or {}).get("claim_outcome")
    if outcome == "released":
        return "the build is no longer running, and released its claims"
    if outcome == "taken_over":
        return "it lapsed and another execution took it over"
    if outcome:
        return f"the claim ended ({outcome})"
    return "it lapsed"


class ClaimRenewal:
    """Renews an in-process execution's claim while it runs (D11).

    An async context manager: the loop runs for the block and is cancelled
    on exit. A refused renewal means the claim is no longer this
    execution's — its build released it, or it lapsed (and another execution
    may have taken it over): logged with the reason the registry recorded,
    and the execution's own reports will be recorded as late.
    """

    def __init__(
        self, session: ResidentSession, task: BaseTask, execution_id: UUID | None
    ) -> None:
        self._session = session
        self._task = task
        self._execution_id = execution_id
        self._loop_task: asyncio.Task | None = None

    async def _renew_forever(self) -> None:
        session = self._session
        config = session.claim_config
        assert self._execution_id is not None
        while True:
            await asyncio.sleep(config.renew_interval_seconds)
            try:
                await session.registry.claim_renew_aio(
                    str(self._task.id),
                    execution_id=self._execution_id,
                    claim_ttl_seconds=config.in_process_ttl_seconds,
                )
            except asyncio.CancelledError:
                raise
            except APIError as e:
                if e.code == "claim_not_held":
                    logger.warning(
                        f"The claim on task {self._task.id} is no longer this "
                        f"execution's: {_lost_claim_reason(e)}; its reports "
                        "will be recorded as late."
                    )
                    return
                logger.warning(f"Could not renew the claim on {self._task.id}: {e}")
            except Exception as e:
                logger.warning(f"Could not renew the claim on {self._task.id}: {e}")

    def start(self) -> None:
        if (
            self._session.enabled
            and self._execution_id is not None
            and self._loop_task is None
        ):
            self._loop_task = asyncio.create_task(
                self._renew_forever(), name=f"claim-renewal-{self._task.id}"
            )

    async def stop(self) -> None:
        loop_task, self._loop_task = self._loop_task, None
        if loop_task is not None:
            loop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await loop_task

    async def __aenter__(self) -> "ClaimRenewal":
        self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()
