"""Unit tests for reactive tick scheduling (stardag.build._reactive).

Uses an in-memory fake registry that mirrors the API's frontier semantics
(dependency gating on task statuses), driven entirely by the tick's own
event calls — plus a fake detached executor whose "workers" complete
instantly (simulating worker-side lifecycle reporting + wake-up).
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import typing
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4


import pytest

from stardag import (
    BaseTask,
    TaskStruct as TaskStructType,
    auto_namespace,
    flatten_task_struct,
)
from stardag.build import (
    BuildTaskStore,
    DetachedExecutionStatus,
    DetachedHandle,
    FailMode,
    TaskExecutorABC,
    TickConfig,
    discover_and_register_aio,
    run_tick_aio,
)
from stardag.build import _reactive as reactive_module
from stardag.build._reactive import (
    _RETRYABLE_STATUSES,
    TickSummary,
    _skip_blocked,
)
from stardag.exceptions import NotFoundError
from stardag.registry import (
    SchedulerLeaseResult,
    BuildFrontier,
    BuildNotifyResult,
    FrontierExternalBlocker,
    FrontierTaskRef,
    NoOpRegistry,
    RegisteredTaskInfo,
)
from stardag.target import InMemoryFileTarget
from stardag.utils.testing.helper_tasks import SyncOnlyTask

auto_namespace(__name__)

FAST_TICK = TickConfig(linger_seconds=0.3, poll_interval_seconds=0.01)


# =============================================================================
# Fakes
# =============================================================================


class FakeReactiveRegistry(NoOpRegistry):
    """In-memory registry with frontier semantics mirroring the API.

    State transitions are driven by the tick's own calls (and by tests
    setting up preconditions). ``auto_complete`` simulates instant workers:
    a task transitions straight to completed when its start is recorded,
    with the wake-up flag set (as a self-reporting worker would).
    """

    def __init__(
        self,
        *,
        root_task_ids: list[str],
        auto_complete: bool = False,
    ) -> None:
        super().__init__()
        self.root_task_ids = root_task_ids
        self.auto_complete = auto_complete
        # The reactive marker/owner, surfaced on the frontier. Presence is
        # the marker (defaults to a reactive build); tests set it to None to
        # simulate a non-reactive build.
        self.reactive_app_name: str | None = "test-app"
        self.reactive_tick_kwargs: dict | None = {}
        self.statuses: dict[str, str] = {}
        self.upstreams: dict[str, set[str]] = {}
        self.refs: dict[str, tuple[str | None, str | None]] = {}
        self.start_metadata: dict[str, dict | None] = {}
        self.needs_tick = False
        # Scheduler lease state (see build_acquire_scheduler_lease_aio).
        self.lease_acquired = True
        self.lease_owner: str | None = None
        # Our own lease expired between renewals, with nobody having taken
        # it: renew refuses, re-acquire succeeds. The SDK heals and carries
        # on.
        self.lease_lapsed = False
        # A successor holds the lease: renew refuses and so does the
        # re-acquire, so the tick must stop. Applies from the *second*
        # acquire onwards, since the tick has to get the lease before it
        # can lose it.
        self.lease_stolen = False
        self._lease_acquires = 0
        self.lease_on_release: typing.Callable[[], None] | None = None
        self.build_status = "running"
        self.build_error_message: str | None = None
        self.calls: list[tuple[str, str | None]] = []
        # Named concurrency limits: key -> cap; holders tracked per task.
        self.limits: dict[str, int] = {}
        self.task_limit_keys: dict[str, set[str]] = {}
        # Limit keys as sent on each claiming start: task_id -> keys.
        self.claim_limit_keys: dict[str, list[str]] = {}
        self.status_at: dict[str, datetime] = {}
        # task_id -> when its RUNNING execution claim lapses. Absent = the
        # server's "never lapses" (NULL), which is also what a server
        # predating claim expiry reports for everything.
        self.expires_at: dict[str, datetime] = {}
        # claim_ttl_seconds as sent on each start: task_id -> list of TTLs
        # (a spawn records two starts — the claim and the post-spawn ref).
        self.sent_claim_ttls: dict[str, list[int | None]] = {}
        # --- attempt counting per build ROUND (TickConfig.max_attempts) ---
        # Rather than keeping a number, keep the lifecycle events and derive
        # the count the way the server does (see ``attempt_count``). Each
        # entry is "start", "interrupt" or "other" — three values rather
        # than two because a start is collapsed by BOTH of the first two,
        # for different reasons: consecutive starts are one execution
        # re-recording itself, while a start after an interrupt continues
        # work the platform took away. Only events after the build's most
        # recent BUILD_RESUMED are counted at all.
        self.task_events: dict[str, list[str]] = {}
        # Where each task's current round begins in its event list — moved
        # to the end of the list by ``build_resume_aio``. Absent = the build
        # has never been resumed, so the round is the whole list.
        self.round_start: dict[str, int] = {}
        # Set False to emulate a server predating attempt_count: the field
        # is absent from the payload, so the model default (None) applies —
        # which is what makes "this registry cannot count" distinguishable
        # from "this task has not been attempted" (0).
        self.serves_attempt_counts = True
        # error_message of every recorded failure, per task — the text an
        # operator actually reads in the UI.
        self.fail_reasons: dict[str, list[str | None]] = {}
        # ...and the same for interruptions.
        self.interrupt_reasons: dict[str, list[str | None]] = {}
        # Set False to emulate a server predating interrupt_count (the
        # field is absent, so the model default None applies). Distinct
        # from serves_attempt_counts: the two shipped separately, and the
        # degradations differ.
        self.serves_interrupt_counts = True
        # task_id -> task_data body, served by task_get_metadata_aio
        # (rehydration fallback); missing key -> KeyError, like a 404.
        self.metadata_bodies: dict[str, dict] = {}
        # --- cross-build scope (mirrors the API's two scopes) ---
        # ``statuses`` is environment-global; these task ids exist in the
        # environment but are NOT in this build's task set, so they gate
        # this build's tasks (dependency edges are global too) while
        # contributing to neither ``actionable`` nor ``status_counts``.
        self.not_in_build: set[str] = set()
        self.blocker_attempts: dict[str, int] = {}
        # task_id -> the build whose event produced the current status. An
        # ABSENT key means "this build's own doing" (the common case), which
        # is what makes a task NOT an external blocker; an explicit None is a
        # row predating status denormalisation — not this build's doing
        # either, and with no build to ask about it.
        self.status_build_id: dict[str, UUID | None] = {}
        # task_id -> (namespace, name), echoed on blocker entries.
        self.task_names: dict[str, tuple[str, str]] = {}
        self.blocked_by_external_truncated = False
        # Derived status of OTHER builds in the environment, served by
        # build_get_aio — how the tick decides whether the build owning a
        # blocker is still going to schedule it. Absent ids 404, and
        # ``build_get_status`` of None emulates a server that doesn't report
        # the field.
        self.other_build_statuses: dict[UUID, str | None] = {}
        self.build_get_calls: list[UUID] = []
        # Set False to emulate a server predating blocked_by_external: the
        # fields stay at their model defaults, as they would deserialising
        # a response that never carried them.
        self.serves_blocked_by_external = True
        # Tick summaries reported by run_tick_aio, and an optional error to
        # raise from the reporting endpoint.
        self.reported_tick_summaries: list[dict] = []
        self.tick_summary_error: Exception | None = None
        # Set to make the frontier fetch blow up, i.e. crash the tick itself.
        self.frontier_error: Exception | None = None

    # --- test setup helpers ---

    def add_task(
        self,
        task_id: str,
        status: str = "pending",
        upstreams: set[str] | None = None,
        executor: str | None = None,
        executor_ref: str | None = None,
        status_at: "datetime | None" = None,
        expires_at: "datetime | None" = None,
        attempt_count: int | None = None,
        interrupt_count: int = 0,
    ) -> None:
        self.statuses[task_id] = status
        self.upstreams.setdefault(task_id, set()).update(upstreams or set())
        if executor or executor_ref:
            self.refs[task_id] = (executor, executor_ref)
        if status_at is not None:
            self.status_at[task_id] = status_at
        if expires_at is not None:
            self.expires_at[task_id] = expires_at
        if attempt_count is not None:
            # Pre-seed the history of a task whose earlier attempts this test
            # does not simulate start-by-start (the common shape: a task
            # fabricated straight into RUNNING or PENDING). Each attempt is
            # a start followed by something else; a RUNNING task's last
            # attempt is still open, so it ends on its start.
            events: list[str] = []
            for _ in range(attempt_count):
                events.extend(["start", "other"])
            if status == "running" and events:
                events.pop()
            self.task_events[task_id] = events
        if interrupt_count:
            # Interruptions are appended as start/interrupt pairs, which is
            # the shape the real stream has and — because a start after an
            # interrupt opens no new attempt — leaves ``attempt_count``
            # alone. That property is exactly what these tests are for, so
            # the fake has to reproduce it rather than assert it.
            for _ in range(interrupt_count):
                self.task_events.setdefault(task_id, []).extend(["start", "interrupt"])

    def _count_event(self, task_id: str, *, kind: str) -> None:
        """Record one lifecycle event: "start", "interrupt" or "other"."""
        self.task_events.setdefault(task_id, []).append(kind)

    def _round_events(self, task_id: str) -> list[str]:
        events = self.task_events.get(task_id, [])
        return events[self.round_start.get(task_id, 0) :]

    def attempt_count(self, task_id: str) -> int:
        """Starts in the current round, collapsing consecutive ones.

        The server's rule, applied to the recorded history rather than
        maintained as a counter — so a round boundary needs no bookkeeping
        beyond noting where it fell.

        An "interrupt" predecessor collapses a start too, exactly like
        another start: the platform took the container away, so the run
        that follows continues work rather than beginning a new attempt.
        """
        count = 0
        previous = None
        for kind in self._round_events(task_id):
            if kind == "start" and previous not in ("start", "interrupt"):
                count += 1
            previous = kind
        return count

    def interrupt_count(self, task_id: str) -> int:
        """Interruptions in the current round — a plain count, no collapsing.

        Nothing emits more than one per execution (the dying worker reports
        it once), so unlike attempts there is nothing to de-duplicate.
        """
        return sum(1 for kind in self._round_events(task_id) if kind == "interrupt")

    def add_blocking_task(
        self,
        task_id: str,
        *,
        blocks: "set[str]",
        status: str = "running",
        owner_build_id: "UUID | None" = None,
        owner_build_status: "str | None" = "running",
        owner_build_known: bool = True,
        name: str = "BlockingTask",
        namespace: str = "",
        status_at: "datetime | None" = None,
        expires_at: "datetime | None" = None,
        in_build: bool = False,
        attempt_count: int = 0,
    ) -> None:
        """Register an upstream whose current status this build did not set.

        Defaults to the #208 A1 shape: RUNNING under some *other* build and
        absent from this build's task set, so it gates ``blocks`` while
        appearing in neither this build's ``running`` nor its
        ``status_counts``. ``owner_build_status`` is what ``build_get`` will
        report for that other build (None = a server that doesn't report it);
        ``owner_build_known=False`` makes the lookup 404 instead.
        """
        self.statuses[task_id] = status
        owner_build_id = owner_build_id or uuid4()
        self.status_build_id[task_id] = owner_build_id
        if owner_build_known:
            self.other_build_statuses[owner_build_id] = owner_build_status
        self.task_names[task_id] = (namespace, name)
        if status_at is not None:
            self.status_at[task_id] = status_at
        if expires_at is not None:
            self.expires_at[task_id] = expires_at
        if not in_build:
            self.not_in_build.add(task_id)
        self.blocker_attempts[task_id] = attempt_count
        for downstream in blocks:
            self.upstreams.setdefault(downstream, set()).add(task_id)

    # --- registry surface used by the tick ---

    async def task_register_bulk_aio(self, build_id, tasks, *, limit_keys=None):
        infos = []
        for task in tasks:
            tid = str(task.id)
            self.statuses.setdefault(tid, "pending")
            self.upstreams.setdefault(tid, set()).update(
                str(dep.id)
                for dep in __import__("stardag").flatten_task_struct(task.requires())
            )
            self.calls.append(("register", tid))
            executor, executor_ref = self.refs.get(tid, (None, None))
            infos.append(
                RegisteredTaskInfo(
                    task_id=tid,
                    latest_status=self.statuses[tid],
                    latest_executor=executor,
                    latest_executor_ref=executor_ref,
                )
            )
        return infos

    async def task_start_aio(
        self,
        build_id,
        task,
        executor=None,
        executor_ref=None,
        executor_metadata=None,
        claim_ttl_seconds=None,
    ):
        tid = str(task.id)
        self.calls.append(("start", tid))
        self._count_event(tid, kind="start")
        self.sent_claim_ttls.setdefault(tid, []).append(claim_ttl_seconds)
        self.statuses[tid] = "running"
        self.refs[tid] = (executor, executor_ref)
        self.start_metadata[tid] = executor_metadata
        if self.auto_complete:
            # Instant worker: completes and wakes the scheduler.
            self.statuses[tid] = "completed"
            self.needs_tick = True

    async def task_start_claim_aio(
        self,
        build_id,
        task,
        executor=None,
        executor_ref=None,
        executor_metadata=None,
        limit_keys=None,
        claim_ttl_seconds=None,
        *,
        claim=True,
    ):
        """Real claim arbitration, mirroring the API's claim-on-start.

        ``claim=False`` acquires the limit slots only — the same orthogonal
        flags the API's one ``/start`` endpoint carries."""
        from stardag.registry import StartClaimResult

        tid = str(task.id)
        self.calls.append(("start_claim", tid))
        self.claim_limit_keys[tid] = list(limit_keys or [])
        if claim and self.statuses.get(tid) == "running":
            executor_name, ref = self.refs.get(tid, (None, None))
            return StartClaimResult(
                started=False,
                denied_reason="already_running",
                executor=executor_name,
                executor_ref=ref,
            )
        if claim and self.statuses.get(tid) == "completed":
            return StartClaimResult(started=False, denied_reason="already_completed")
        started = await self._acquire_limits(
            build_id,
            task,
            executor=executor,
            executor_ref=executor_ref,
            executor_metadata=executor_metadata,
            limit_keys=limit_keys,
            claim_ttl_seconds=claim_ttl_seconds,
        )
        if not started:
            return StartClaimResult(started=False, denied_reason="limit")
        return StartClaimResult(started=True)

    async def _acquire_limits(
        self,
        build_id,
        task,
        executor=None,
        executor_ref=None,
        executor_metadata=None,
        limit_keys=None,
        claim_ttl_seconds=None,
    ):
        # The limits half of a start, mirroring the API's semantics: count
        # running holders per key against configured caps (self.limits);
        # all-or-nothing acquisition. A helper on this double, not a
        # registry method — the API has one start endpoint, and the SDK now
        # has one method reaching it.
        self.calls.append(("acquire_limits", str(task.id)))
        for key in limit_keys or []:
            cap = self.limits.get(key)
            if cap is None:
                continue
            active = sum(
                1
                for tid, keys in self.task_limit_keys.items()
                if key in keys
                and self.statuses.get(tid) == "running"
                and tid != str(task.id)
            )
            if active >= cap:
                return False
        self.task_limit_keys[str(task.id)] = set(limit_keys or [])
        await self.task_start_aio(
            build_id,
            task,
            executor=executor,
            executor_ref=executor_ref,
            executor_metadata=executor_metadata,
            claim_ttl_seconds=claim_ttl_seconds,
        )
        return True

    async def task_complete_aio(self, build_id, task):
        tid = str(task.id)
        self.calls.append(("complete", tid))
        self._count_event(tid, kind="other")
        self.statuses[tid] = "completed"

    async def task_retry_by_id_aio(self, build_id, task_id):
        """Id-based retry — what a tick uses for a blocker it cannot
        reconstruct (it has the id off the frontier, not the object)."""
        self.calls.append(("retry", task_id))
        self._count_event(task_id, kind="other")
        if self.statuses.get(task_id) in _RETRYABLE_STATUSES:
            self.statuses[task_id] = "pending"
            self.refs.pop(task_id, None)

    async def task_retry_aio(self, build_id, task):
        tid = str(task.id)
        self.calls.append(("retry", tid))
        self._count_event(tid, kind="other")
        # Same retryable set the server applies (suspended included: a
        # suspended task has no live execution to orphan).
        if self.statuses.get(tid) in _RETRYABLE_STATUSES:
            self.statuses[tid] = "pending"
            self.refs.pop(tid, None)

    async def build_add_roots_aio(self, build_id, root_task_ids):
        self.calls.append(("add_roots", ",".join(root_task_ids)))
        self.root_task_ids += [t for t in root_task_ids if t not in self.root_task_ids]

    async def task_cancel_aio(self, build_id, task):
        tid = str(task.id)
        self.calls.append(("cancel", tid))
        self._count_event(tid, kind="other")
        self.statuses[tid] = "cancelled"

    async def task_fail_aio(self, build_id, task, error_message=None):
        tid = str(task.id)
        self.calls.append(("fail", tid))
        self._count_event(tid, kind="other")
        self.fail_reasons.setdefault(tid, []).append(error_message)
        self.statuses[tid] = "failed"

    async def task_interrupt_aio(self, build_id, task, reason=None):
        """What a worker reports when the platform took its container.

        Mirrors the server: the claim goes (so no expiry survives) but the
        executor ref stays, because a backend running its own retries may
        be restarting that very call and a scheduler needs the ref to
        probe for it.
        """
        tid = str(task.id)
        self.calls.append(("interrupt", tid))
        self._count_event(tid, kind="interrupt")
        self.interrupt_reasons.setdefault(tid, []).append(reason)
        self.statuses[tid] = "interrupted"
        self.expires_at.pop(tid, None)

    async def build_complete_aio(self, build_id):
        self.calls.append(("build_complete", None))
        self.build_status = "completed"

    async def build_fail_aio(self, build_id, error_message=None):
        self.calls.append(("build_fail", None))
        self.build_status = "failed"
        self.build_error_message = error_message

    async def task_get_metadata_aio(self, task_id):
        from stardag.registry._base import TaskMetadata

        body = self.metadata_bodies[str(task_id)]
        return TaskMetadata(
            id=task_id,
            body=body,
            name=body.get("__name", ""),
            namespace=body.get("__namespace", ""),
            version=body.get("version", ""),
            output_uri=None,
            status=self.statuses.get(str(task_id), "pending"),
            registered_at=None,
            started_at=None,
            completed_at=None,
            error_message=None,
        )

    async def build_skip_blocked_aio(self, build_id):
        # Mirrors the API: pending/suspended tasks transitively downstream
        # of a failed/cancelled/skipped task become skipped.
        self.calls.append(("skip_blocked", None))
        blocked = {
            tid
            for tid, status in self.statuses.items()
            if status in ("failed", "cancelled", "skipped")
        }
        # Blockage only propagates through nodes that will themselves never
        # complete (mirrors the API's CTE gate): a completed intermediate
        # satisfies its downstream; a running one may still complete.
        propagating = ("failed", "cancelled", "skipped", "pending", "suspended")
        changed = True
        while changed:
            changed = False
            for tid, ups in self.upstreams.items():
                if tid not in blocked and any(
                    up in blocked and self.statuses.get(up) in propagating for up in ups
                ):
                    blocked.add(tid)
                    changed = True
        skipped = []
        for tid in blocked:
            if self.statuses.get(tid) in ("pending", "suspended", "interrupted"):
                self.statuses[tid] = "skipped"
                skipped.append(tid)
        return skipped

    async def build_report_tick_summary_aio(self, build_id, summary):
        # Observability sink. ``tick_summary_error`` lets a test make it
        # fail the way a real registry can (route missing, server down)
        # and assert the tick is unaffected.
        self.reported_tick_summaries.append(summary)
        if self.tick_summary_error is not None:
            raise self.tick_summary_error

    async def build_set_reactive_meta_aio(
        self, build_id, *, app_name, tick_kwargs=None
    ):
        self.calls.append(("set_reactive_meta", app_name))
        self.reactive_app_name = app_name
        if tick_kwargs is not None:
            self.reactive_tick_kwargs = tick_kwargs

    async def build_get_aio(self, build_id):
        from stardag.registry import BuildInfo

        self.build_get_calls.append(build_id)
        if build_id not in self.other_build_statuses:
            raise NotFoundError(f"Build {build_id} not found", detail="Build not found")
        return BuildInfo(id=build_id, status=self.other_build_statuses[build_id])

    async def build_resume_aio(self, build_id, executor_metadata=None):
        """BUILD_RESUMED — what a re-trigger of this build id records.

        It starts a new round, and attempt counts are windowed to the
        round, so every task's count restarts at zero. The tick never calls
        this; tests use it the way ``_trigger_reactive`` does, BEFORE the
        discovery retries that reset failed tasks to pending.
        """
        self.calls.append(("build_resume", None))
        self.build_status = "running"
        for tid, events in self.task_events.items():
            self.round_start[tid] = len(events)

    async def build_notify_aio(self, build_id, *, can_spawn=True) -> BuildNotifyResult:
        self.needs_tick = True
        return BuildNotifyResult(build_id=build_id, scheduler_live=True)

    async def build_clear_notify_aio(self, build_id):
        self.needs_tick = False

    async def build_get_notify_aio(self, build_id) -> BuildNotifyResult:
        # The cheap flag read, overridden rather than inherited: the ABC
        # default delegates to the frontier, and a double that inherits it
        # cannot tell "read the flag" from "read the frontier" — which is
        # exactly what the linger poll's cost depends on.
        return BuildNotifyResult(build_id=build_id, needs_tick=self.needs_tick)

    # --- scheduler lease (STA-17: on the build row, not a lock table) ---

    async def build_acquire_scheduler_lease_aio(
        self, build_id, *, owner_id, ttl_seconds
    ):
        # A lapsed lease is re-acquirable — that takeover *is* the healing
        # mechanism, and it is what the SDK falls back on when a renewal is
        # refused. A *stolen* one is not: somebody else holds it.
        self._lease_acquires += 1
        if self.lease_stolen and self._lease_acquires > 1:
            self.lease_owner = "__successor__"
            return SchedulerLeaseResult(build_id=build_id, held=False)
        self.lease_lapsed = False
        self.lease_owner = owner_id if self.lease_acquired else self.lease_owner
        return SchedulerLeaseResult(build_id=build_id, held=self.lease_acquired)

    async def build_renew_scheduler_lease_aio(self, build_id, *, owner_id, ttl_seconds):
        # The server refuses a renewal for two different reasons, and the
        # SDK responds to them differently, so both are modelled.
        #
        # ``lease_lapsed``: our own lease expired between renewals and
        # nobody took it, so the owner column still says us. The SDK
        # re-acquires and carries on.
        #
        # ``lease_stolen``: somebody else holds it, so the re-acquire fails
        # too and the tick must stop.
        if self.lease_stolen:
            # The theft is visible in the owner column, exactly as on the
            # server: the successor's acquire replaced our id, so every
            # later owner-checked call by us is refused — including the
            # exit release, which must not clear the successor's lease.
            self.lease_owner = "__successor__"
            return SchedulerLeaseResult(build_id=build_id, held=False)
        if self.lease_lapsed:
            return SchedulerLeaseResult(build_id=build_id, held=False)
        return SchedulerLeaseResult(
            build_id=build_id, held=self.lease_owner == owner_id
        )

    async def build_release_scheduler_lease_aio(self, build_id, *, owner_id):
        held = self.lease_owner == owner_id
        if held:
            self.lease_owner = None
        if self.lease_on_release is not None:
            # Fires on the release *attempt*, deliberately not only on a
            # successful release: it models a wake-up landing in the
            # release window, and the notifier is a third party whose
            # timing does not depend on whether this release was still the
            # holder's to make. Gating it on ``held`` would leave the
            # lease-lost exit-handshake test asserting against a flag that
            # can never be set.
            self.lease_on_release()
        return SchedulerLeaseResult(build_id=build_id, held=held)

    async def build_get_frontier_aio(self, build_id) -> BuildFrontier:
        if self.frontier_error is not None:
            raise self.frontier_error

        def ref(tid: str) -> FrontierTaskRef:
            executor, executor_ref = self.refs.get(tid, (None, None))
            return FrontierTaskRef(
                task_id=tid,
                latest_status=self.statuses[tid],
                latest_executor=executor,
                latest_executor_ref=executor_ref,
                latest_status_at=self.status_at.get(tid),
                latest_status_expires_at=self.expires_at.get(tid),
                attempt_count=(
                    self.attempt_count(tid) if self.serves_attempt_counts else None
                ),
                interrupt_count=(
                    self.interrupt_count(tid) if self.serves_interrupt_counts else None
                ),
            )

        # Build-scoped, like the API: actionable/running/status_counts see
        # only this build's task set. Dependency gating below stays global.
        in_build = {
            tid: status
            for tid, status in self.statuses.items()
            if tid not in self.not_in_build
        }
        actionable = [
            ref(tid)
            for tid, status in in_build.items()
            if status in ("pending", "suspended", "running", "interrupted")
            and all(
                self.statuses.get(up) == "completed"
                for up in self.upstreams.get(tid, set())
            )
        ]
        counts: dict[str, int] = {}
        for status in in_build.values():
            counts[status] = counts.get(status, 0) + 1
        running = [ref(tid) for tid, status in in_build.items() if status == "running"]
        # Mirrors the API: computed ONLY when the build has nothing
        # actionable and nothing running, so an empty list means "not
        # blocked externally, OR not stalled".
        blocked_by_external: list[FrontierExternalBlocker] = []
        if self.serves_blocked_by_external and not actionable and not running:
            for tid, status in in_build.items():
                if status not in ("pending", "suspended", "running", "interrupted"):
                    continue
                for up in sorted(self.upstreams.get(tid, set())):
                    if up not in self.statuses:
                        continue
                    if self.statuses[up] == "completed":
                        continue
                    if up not in self.status_build_id:
                        continue  # this build's own doing — not external
                    owner_id = self.status_build_id[up]
                    if owner_id == build_id:
                        continue
                    namespace, name = self.task_names.get(up, ("", up))
                    blocked_by_external.append(
                        FrontierExternalBlocker(
                            task_id=tid,
                            blocking_task_id=up,
                            blocking_task_namespace=namespace,
                            blocking_task_name=name,
                            blocking_status=self.statuses[up],
                            blocking_status_at=self.status_at.get(up),
                            # Mirrors the server: only a RUNNING task holds
                            # a claim, so only a RUNNING blocker can carry
                            # an expiry.
                            blocking_status_expires_at=(
                                self.expires_at.get(up)
                                if self.statuses[up] == "running"
                                else None
                            ),
                            blocking_status_build_id=owner_id,
                            blocking_in_build=up not in self.not_in_build,
                            # Mirrors the server: attempts are per build, so
                            # only a blocker in this build's plan has any.
                            blocking_attempt_count=(
                                self.blocker_attempts.get(up, 0)
                                if up not in self.not_in_build
                                else None
                            ),
                        )
                    )
        return BuildFrontier(
            build_id=build_id,
            build_status=self.build_status,
            needs_tick=self.needs_tick,
            root_task_ids=self.root_task_ids,
            roots=[ref(t) for t in self.root_task_ids if t in self.statuses],
            status_counts=counts,
            actionable=actionable,
            running=running,
            blocked_by_external=blocked_by_external,
            blocked_by_external_truncated=(
                self.blocked_by_external_truncated and bool(blocked_by_external)
            ),
            reactive_app_name=self.reactive_app_name,
            reactive_tick_kwargs=self.reactive_tick_kwargs,
        )


class FakeTickExecutor(TaskExecutorABC):
    """Detached executor for ticks: spawn only (results never awaited)."""

    def __init__(
        self,
        statuses: dict[str, DetachedExecutionStatus] | None = None,
        timeout_seconds: float | None = None,
    ):
        # ref -> probe status
        self.probe_statuses = statuses or {}
        self.spawned: list[UUID] = []
        self.cancelled_refs: list[str] = []
        self._spawn_count = 0
        # The backend's own wall-clock limit, from which the tick derives
        # the claim TTL. None = a backend that enforces none.
        self.timeout_seconds = timeout_seconds

    async def submit(self, task):
        raise AssertionError("ticks must not use blocking submit")

    def execution_timeout_seconds(self, task: BaseTask) -> float | None:
        return self.timeout_seconds

    def supports_detached(self, task: BaseTask) -> bool:
        return True

    async def submit_detached(self, task: BaseTask) -> DetachedHandle:
        self.spawned.append(task.id)
        self._spawn_count += 1

        async def wait():
            raise AssertionError("ticks must not await detached results")

        return DetachedHandle(
            executor="fake", ref=f"ref-{self._spawn_count}", wait=wait
        )

    async def detached_status(self, task, executor, ref):
        return self.probe_statuses.get(ref, DetachedExecutionStatus.UNKNOWN)

    async def cancel_detached(self, task, executor, ref):
        self.cancelled_refs.append(ref)

    async def setup(self):
        pass

    async def teardown(self):
        pass


class InMemoryTaskStore(BuildTaskStore):
    """BuildTaskStore on a dict — no target roots needed in engine tests.

    Pickle-only now: the reactive marker/owner/config live in the registry
    (see ``FakeReactiveRegistry.reactive_meta``), not the store.
    """

    def __init__(self, build_id: UUID):
        super().__init__(build_id)
        self._tasks: dict[str, BaseTask] = {}

    def save_task(self, task: BaseTask) -> None:
        self._tasks[str(task.id)] = task

    def load_task(self, task_id):
        return self._tasks.get(str(task_id))


# =============================================================================
# Helpers
# =============================================================================


def _chain(*names: str) -> list[BaseTask]:
    """Build a linear chain of SyncOnlyTasks: first is leaf, last is root."""
    tasks: list[BaseTask] = []
    prev: tuple = ()
    for name in names:
        task = SyncOnlyTask(name=name, deps=prev)
        tasks.append(task)
        prev = (task,)
    return tasks


def _setup(
    tasks: list[BaseTask],
    *,
    auto_complete: bool = True,
    lease_acquired: bool = True,
    executor: FakeTickExecutor | None = None,
    on_release: typing.Callable[[], None] | None = None,
) -> tuple[
    FakeReactiveRegistry,
    FakeTickExecutor,
    InMemoryTaskStore,
]:
    root = tasks[-1]
    registry = FakeReactiveRegistry(
        root_task_ids=[str(root.id)], auto_complete=auto_complete
    )
    # The scheduler lease is registry state now, not a separate lock
    # manager, so the knobs that used to configure the fake lock manager
    # configure the fake registry.
    registry.lease_acquired = lease_acquired
    registry.lease_on_release = on_release
    for task in tasks:
        registry.add_task(
            str(task.id),
            upstreams={str(d.id) for d in flatten_task_struct(task.requires())},
        )
    store = InMemoryTaskStore(uuid4())
    store.save_tasks(tasks)
    return (
        registry,
        executor or FakeTickExecutor(),
        store,
    )


# =============================================================================
# Tests
# =============================================================================


class TestTickHappyPath:
    async def test_completes_chain_within_one_lingering_tick(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """Instant workers: one tick drives dep → root → BUILD_COMPLETED via
        linger wake-ups, spawning in dependency order."""
        dep, root = _chain("tick-dep", "tick-root")
        registry, executor, store = _setup([dep, root])

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=FAST_TICK,
        )

        assert summary.outcome == "terminal"
        assert summary.terminal_status == "completed"
        assert executor.spawned == [dep.id, root.id]
        assert registry.build_status == "completed"
        assert summary.spawned == 2

    async def test_lease_held_is_noop(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        dep, root = _chain("lease-dep", "lease-root")
        registry, executor, store = _setup([dep, root], lease_acquired=False)

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=FAST_TICK,
        )

        assert summary.outcome == "lease_held"
        assert executor.spawned == []
        assert registry.calls == []

    async def test_not_reactive_build_is_noop(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        (root,) = _chain("not-reactive-root")
        registry, executor, store = _setup([root])
        # No reactive_app_name on the frontier → not a reactively-scheduled
        # build; the tick must not act on it.
        registry.reactive_app_name = None

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=FAST_TICK,
        )

        assert summary.outcome == "not_reactive"
        assert executor.spawned == []
        # No scheduling happened — the tick observed reactive_meta is None on
        # its first frontier fetch and bailed before acting.
        assert registry.build_status == "running"


class TestRunningTaskResolution:
    async def test_live_ref_left_alone(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        (root,) = _chain("live-root")
        executor = FakeTickExecutor(
            statuses={"fc-live": DetachedExecutionStatus.RUNNING}
        )
        registry, executor, store = _setup(
            [root], auto_complete=False, executor=executor
        )
        registry.add_task(
            str(root.id), status="running", executor="fake", executor_ref="fc-live"
        )

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=FAST_TICK,
        )

        assert summary.outcome == "lingered_out"
        assert executor.spawned == []
        assert summary.self_healed == 0

    async def test_target_exists_self_heals_completion(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """Worker wrote the output then died before reporting: the tick
        emits the completion (target is ground truth) and the build
        finishes."""
        (root,) = _chain("heal-root")
        root.run()  # target now exists
        registry, executor, store = _setup([root], auto_complete=False)
        registry.add_task(
            str(root.id), status="running", executor="fake", executor_ref="fc-gone"
        )

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=FAST_TICK,
        )

        assert summary.outcome == "terminal"
        assert summary.terminal_status == "completed"
        assert summary.self_healed == 1
        assert ("complete", str(root.id)) in registry.calls
        assert executor.spawned == []

    async def test_failed_ref_records_failure_and_fails_build(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        (root,) = _chain("failed-ref-root")
        executor = FakeTickExecutor(
            statuses={"fc-dead": DetachedExecutionStatus.FAILED}
        )
        registry, _, store = _setup([root], auto_complete=False, executor=executor)
        registry.add_task(
            str(root.id),
            status="running",
            executor="fake",
            executor_ref="fc-dead",
            # At the default 2-attempt budget: the failure is final, which
            # is what this test is about. Retry behaviour below budget has
            # its own tests (see TestAttemptBudget).
            attempt_count=2,
        )

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=FAST_TICK,
        )

        assert summary.failed_recorded == 1
        assert summary.outcome == "terminal"
        assert summary.terminal_status == "failed"
        assert registry.build_status == "failed"

    async def test_unknown_ref_left_alone(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """UNKNOWN probe status → conservatively leave (no duplicate spawn)."""
        (root,) = _chain("unknown-ref-root")
        registry, executor, store = _setup([root], auto_complete=False)
        registry.add_task(
            str(root.id),
            status="running",
            executor="other-backend",
            executor_ref="job-1",
        )

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=FAST_TICK,
        )

        assert summary.outcome == "lingered_out"
        assert executor.spawned == []


class TestTerminalHandling:
    async def test_cancelled_build_cancels_running_refs(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        (root,) = _chain("cancelled-root")
        registry, executor, store = _setup([root], auto_complete=False)
        registry.add_task(
            str(root.id), status="running", executor="fake", executor_ref="fc-run"
        )
        registry.build_status = "cancelled"

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=FAST_TICK,
        )

        assert summary.outcome == "terminal"
        assert summary.terminal_status == "cancelled"
        assert executor.cancelled_refs == ["fc-run"]
        assert executor.spawned == []

    async def test_blocked_build_fails_instead_of_idling(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """CONTINUE mode with a failed upstream: nothing runnable/running →
        the tick fails the build rather than idling forever."""
        dep, root = _chain("blocked-dep", "blocked-root")
        registry, executor, store = _setup([dep, root], auto_complete=False)
        registry.add_task(str(dep.id), status="failed")

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=TickConfig(
                linger_seconds=0.3,
                poll_interval_seconds=0.01,
                fail_mode=FailMode.CONTINUE,
            ),
        )

        assert summary.outcome == "terminal"
        assert summary.terminal_status == "failed"
        assert registry.build_status == "failed"

    async def test_fail_fast_cancels_running_and_fails(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        dep, root = _chain("ff-dep", "ff-root")
        other = SyncOnlyTask(name="ff-other")
        registry, executor, store = _setup([dep, root], auto_complete=False)
        store.save_task(other)
        registry.add_task(str(dep.id), status="failed")
        registry.add_task(
            str(other.id), status="running", executor="fake", executor_ref="fc-x"
        )
        executor.probe_statuses["fc-x"] = DetachedExecutionStatus.RUNNING

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=FAST_TICK,  # FAIL_FAST default
        )

        assert summary.outcome == "terminal"
        assert summary.terminal_status == "failed"
        assert executor.cancelled_refs == ["fc-x"]
        assert registry.build_status == "failed"


class TestDiscoverAndRegister:
    async def test_post_order_registration_and_previously_completed(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        done_leaf = SyncOnlyTask(name="disc-done-leaf")
        done_leaf.run()  # complete
        fresh_leaf = SyncOnlyTask(name="disc-fresh-leaf")
        root = SyncOnlyTask(name="disc-root", deps=(done_leaf, fresh_leaf))

        registry = FakeReactiveRegistry(root_task_ids=[str(root.id)])
        result = await discover_and_register_aio(registry, uuid4(), root)

        assert set(result.incomplete) == {fresh_leaf.id, root.id}
        assert [t.id for t in result.previously_completed] == [done_leaf.id]
        register_order = [tid for (m, tid) in registry.calls if m == "register"]
        assert register_order.index(str(fresh_leaf.id)) < register_order.index(
            str(root.id)
        )
        # Previously-complete tasks are reflected as completed in the registry
        # (the frontier is the scheduler state).
        assert registry.statuses[str(done_leaf.id)] == "completed"


class TestBuildTaskStoreRoundTrip:
    def test_pickle_round_trip(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        # The store is pickle-only; the reactive marker/config live in the
        # registry, not here.
        build_id = uuid4()
        store = BuildTaskStore(build_id)

        task = SyncOnlyTask(name="store-roundtrip")
        store.save_tasks([task])

        loaded = store.load_task(task.id)
        assert loaded is not None
        assert loaded.id == task.id
        assert isinstance(loaded, SyncOnlyTask)
        assert loaded.name == "store-roundtrip"

    def test_load_missing_task_returns_none(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        store = BuildTaskStore(uuid4())
        assert store.load_task(uuid4()) is None


class TestMissingTaskStoreEntry:
    async def test_missing_pickle_fails_task_instead_of_stalling(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """A pending actionable task whose object is missing from the store
        can never be scheduled — the tick fails it (and thereby the build)
        rather than leaving it in the frontier forever, where endless
        watchdog ticks would do nothing."""
        (root,) = _chain("missing-pickle-root")
        registry, executor, store = _setup([root], auto_complete=False)
        store._tasks.clear()  # simulate a lost/never-written pickle

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=FAST_TICK,
        )

        assert summary.failed_recorded == 1
        assert summary.outcome == "terminal"
        assert summary.terminal_status == "failed"
        assert registry.build_status == "failed"
        assert executor.spawned == []


class TestRetryPath:
    async def test_retry_failed_discovery_resets_and_build_completes(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """The reactive retry path: a task failed in a previous run is reset
        to pending by discovery (retry_failed=True) and the re-triggered
        build runs to completion instead of FAIL_FASTing on tick 1."""
        (root,) = _chain("retry-root")
        registry, executor, store = _setup([root])
        registry.add_task(str(root.id), status="failed")

        # What the reactive trigger does on (re-)trigger:
        result = await discover_and_register_aio(
            registry, uuid4(), root, retry_failed=True
        )
        assert [t.id for t in result.retried] == [root.id]
        assert registry.statuses[str(root.id)] == "pending"

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=FAST_TICK,
        )
        assert summary.terminal_status == "completed"
        assert registry.build_status == "completed"

    async def test_retry_failed_resets_an_abandoned_suspended_task(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """#208 A2: a task left SUSPENDED — its execution yielded dynamic
        dependencies and returned, then the build was abandoned — used to be
        permanently unschedulable, since the re-trigger's retry skipped it.
        It is now reset like any other non-completed status."""
        (root,) = _chain("suspended-retry-root")
        registry, executor, store = _setup([root])
        registry.add_task(str(root.id), status="suspended")

        result = await discover_and_register_aio(
            registry, uuid4(), root, retry_failed=True
        )

        assert [t.id for t in result.retried] == [root.id]
        assert registry.statuses[str(root.id)] == "pending"

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=FAST_TICK,
        )
        assert summary.terminal_status == "completed"

    async def test_worker_dynamic_dep_registration_never_resets_suspended(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """The other caller of discover_and_register_aio is a worker
        registering its dynamically yielded deps — it takes the default
        retry_failed=False, so widening the retryable set cannot make a
        suspending worker reset its own task."""
        (root,) = _chain("suspended-worker-root")
        registry, _, _ = _setup([root], auto_complete=False)
        registry.add_task(str(root.id), status="suspended")

        result = await discover_and_register_aio(registry, uuid4(), root)

        assert result.retried == []
        assert registry.statuses[str(root.id)] == "suspended"
        assert not any(method == "retry" for (method, _) in registry.calls)

    async def test_without_retry_failed_build_fail_fasts(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """Control: without the retry, the failed status poisons the build
        (the pre-fix behavior the stack review flagged)."""
        (root,) = _chain("poison-root")
        registry, executor, store = _setup([root])
        registry.add_task(str(root.id), status="failed")

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=FAST_TICK,
        )
        assert summary.terminal_status == "failed"


class TestAddedRootsTerminalDetection:
    async def test_added_roots_gate_completion(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """Roots appended mid-build keep the build running until they too
        complete (previously completion of the original roots stranded
        re-triggered subtrees)."""
        (r1,) = _chain("roots-r1")
        r2 = SyncOnlyTask(name="roots-r2")
        registry, executor, store = _setup([r1])
        store.save_task(r2)
        # Original root completed already; new root appended (as the
        # re-trigger path does server-side) but still pending.
        registry.statuses[str(r1.id)] = "completed"
        await registry.build_add_roots_aio(uuid4(), [str(r2.id)])
        registry.add_task(str(r2.id), status="pending")

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=FAST_TICK,
        )
        # r2 was spawned (auto-completes) and only then the build completed.
        assert executor.spawned == [r2.id]
        assert summary.terminal_status == "completed"


class TestCancelDynamicDepWindow:
    async def test_cancel_reaches_non_actionable_running(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """A RUNNING task inside the dynamic-dep window (incomplete upstream
        → not actionable) is still cancelled on build cancellation."""
        blocker = SyncOnlyTask(name="cxl-blocker")
        runner = SyncOnlyTask(name="cxl-runner")
        registry, executor, store = _setup([blocker, runner], auto_complete=False)
        registry.add_task(
            str(runner.id),
            status="running",
            upstreams={str(blocker.id)},  # dynamic edge, blocker incomplete
            executor="fake",
            executor_ref="fc-window",
        )
        registry.build_status = "cancelled"

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=FAST_TICK,
        )

        assert summary.terminal_status == "cancelled"
        assert executor.cancelled_refs == ["fc-window"]


class TestConcurrencyLimits:
    async def test_denied_task_stays_in_frontier_no_false_deadlock(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """Two tasks under a 1-slot key: only one spawns per round; a denied
        task never triggers the stuck-build failure (the slot holder may
        even be in another build)."""
        a = SyncOnlyTask(name="lim-a")
        b = SyncOnlyTask(name="lim-b")
        root = SyncOnlyTask(name="lim-root", deps=(a, b))
        registry, executor, store = _setup([a, b, root], auto_complete=False)
        registry.limits["one-slot"] = 1

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=TickConfig(
                linger_seconds=0.2,
                poll_interval_seconds=0.01,
                limit_key_selector=lambda t: ["one-slot"]
                if t.id in (a.id, b.id)
                else [],
            ),
        )

        # One acquired + spawned, one denied; build keeps waiting (no
        # terminal failure) and the tick lingers out.
        assert summary.spawned == 1
        assert summary.limit_denied >= 1
        assert summary.outcome == "lingered_out"
        assert registry.build_status == "running"

    async def test_slot_release_lets_denied_task_proceed(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """With instant workers, the whole chain completes within one tick:
        each completion frees the slot and wakes the scheduler, which then
        acquires it for the next task."""
        a = SyncOnlyTask(name="rel-a")
        b = SyncOnlyTask(name="rel-b")
        root = SyncOnlyTask(name="rel-root", deps=(a, b))
        registry, executor, store = _setup([a, b, root])
        registry.limits["one-slot"] = 1

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=TickConfig(
                linger_seconds=0.5,
                poll_interval_seconds=0.01,
                limit_key_selector=lambda t: ["one-slot"],
            ),
        )

        assert summary.outcome == "terminal"
        assert summary.terminal_status == "completed"
        assert summary.spawned == 3
        assert registry.build_status == "completed"

    async def test_no_selector_claims_without_limit_keys(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """Without a selector the claiming start still happens (it is the
        exactly-once arbitration), but carries no limit keys — so nothing
        is enforced and no slot is held."""
        (root,) = _chain("nolim-root")
        registry, executor, store = _setup([root])

        await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=FAST_TICK,
        )

        assert registry.claim_limit_keys[str(root.id)] == []


class TestRunningWithoutRef:
    """The claiming start is recorded BEFORE the spawn, so a tick that dies
    in between leaves a task RUNNING with no ref: nothing to probe, no
    worker to report it, and its concurrency-limit slots held indefinitely.
    Whether that shape is dead or merely mid-spawn is decided by the
    claim's own expiry, not by how long it has sat there."""

    async def _tick_on_running_root(
        self, expires_at: "datetime | None", attempt_count: int = 2
    ):
        # Default: at the default 2-attempt budget, so a lapsed claim ends
        # as a plain failure. Pass a lower count to exercise the retry.
        (root,) = _chain(f"noref-root-{expires_at}-{attempt_count}")
        registry, executor, store = _setup([root], auto_complete=False)
        registry.add_task(
            str(root.id),
            status="running",
            status_at=datetime.now(timezone.utc) - timedelta(hours=1),
            expires_at=expires_at,
            attempt_count=attempt_count,
        )
        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=FAST_TICK,
        )
        return summary, executor, registry, root

    async def test_lapsed_claim_without_ref_is_failed(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """Lapsed claim: the server will hand the task to the next claimant
        anyway, so leaving it RUNNING only leaks the slots it holds."""
        summary, _, _, _ = await self._tick_on_running_root(
            datetime.now(timezone.utc) - timedelta(minutes=1)
        )

        assert summary.failed_recorded == 1
        assert summary.terminal_status == "failed"

    async def test_lapsed_claim_failure_is_retryable_and_counts_once(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """A claim lapses precisely when a worker vanished — the OOM /
        preemption case ``TickConfig.max_attempts`` exists for. The failure
        it records must be retryable like any other, and expiry and retry
        must not each charge an attempt."""
        summary, executor, registry, root = await self._tick_on_running_root(
            datetime.now(timezone.utc) - timedelta(minutes=1), attempt_count=1
        )

        assert summary.failed_recorded == 1
        assert summary.retried == 1
        assert summary.spawned == 1
        assert executor.spawned == [root.id]
        # One attempt closed by the expiry, one opened by the respawn — not
        # three. (The respawn's claim + ref starts collapse into one.)
        assert registry.attempt_count(str(root.id)) == 2

    async def test_live_claim_without_ref_is_left(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """A live claim is left alone however old the status is — the age
        was only ever a proxy for the question the expiry answers."""
        summary, executor, _, _ = await self._tick_on_running_root(
            datetime.now(timezone.utc) + timedelta(hours=1)
        )

        assert summary.failed_recorded == 0
        assert summary.outcome == "lingered_out"
        assert executor.spawned == []

    async def test_claim_without_an_expiry_is_left(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """No expiry (older server, or a start predating the column): the
        spawn-in-progress window of a healthy tick looks identical from
        here, so leave it rather than kill a task about to start."""
        summary, executor, _, _ = await self._tick_on_running_root(None)

        assert summary.failed_recorded == 0
        assert summary.outcome == "lingered_out"
        assert executor.spawned == []


class TestDerivedClaimTtl:
    """Every start the tick records carries a TTL derived from the
    executor's own timeout, so the expiry other schedulers read is tied to
    when the execution is actually killed."""

    async def test_derived_ttl_is_sent_on_both_starts(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        from stardag.build._reactive import _CLAIM_TTL_GRACE_SECONDS

        (root,) = _chain("ttl-root")
        registry, _, store = _setup([root])
        executor = FakeTickExecutor(timeout_seconds=3600.0)

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=FAST_TICK,
        )

        expected = int(3600.0 + _CLAIM_TTL_GRACE_SECONDS)
        assert summary.spawned == 1
        # The claiming start and the post-spawn ref-recording start: the
        # second must carry it too, or it would hand the claim straight
        # back to the registry's generic default.
        assert registry.sent_claim_ttls[str(root.id)] == [expected, expected]

    async def test_no_executor_timeout_leaves_the_ttl_to_the_registry(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        (root,) = _chain("ttl-none-root")
        registry, executor, store = _setup([root])

        await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,  # no timeout
            task_store=store,
            config=FAST_TICK,
        )

        assert registry.sent_claim_ttls[str(root.id)] == [None, None]

    def test_ttl_is_clamped_to_the_servers_accepted_range(self):
        """A 10-second task and a 100-day task are both legitimate; each
        gets the closest expiry the server can express, not a 422."""
        from stardag.build._reactive import (
            _MAX_CLAIM_TTL_SECONDS,
            _MIN_CLAIM_TTL_SECONDS,
            claim_ttl_seconds,
        )

        (task,) = _chain("ttl-clamp")

        class _Timeout(FakeTickExecutor):
            def __init__(self, seconds):
                super().__init__(timeout_seconds=seconds)

        assert (
            claim_ttl_seconds(task, _Timeout(_MAX_CLAIM_TTL_SECONDS * 10))
            == _MAX_CLAIM_TTL_SECONDS
        )
        assert claim_ttl_seconds(task, _Timeout(None)) is None
        short = claim_ttl_seconds(task, _Timeout(1.0))
        assert short is not None and short >= _MIN_CLAIM_TTL_SECONDS

    def test_a_raising_executor_falls_back_to_the_registry_default(self):
        """Resolving a timeout is a diagnostic; it must never fail a start."""
        from stardag.build._reactive import claim_ttl_seconds

        (task,) = _chain("ttl-raises")

        class _Raising(FakeTickExecutor):
            def execution_timeout_seconds(self, task):
                raise RuntimeError("backend unreachable")

        assert claim_ttl_seconds(task, _Raising()) is None


class TestSkipBlockedOnFailure:
    async def test_fail_fast_skips_blocked_descendants(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """On a failure terminal the tick marks transitively blocked tasks
        skipped — they no longer dangle pending while the build is failed."""
        bad = SyncOnlyTask(name="skip-bad")
        mid = SyncOnlyTask(name="skip-mid", deps=(bad,))
        root = SyncOnlyTask(name="skip-root", deps=(mid,))
        registry, executor, store = _setup([bad, mid, root], auto_complete=False)
        registry.add_task(str(bad.id), status="failed")

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=FAST_TICK,  # FAIL_FAST default
        )

        assert summary.terminal_status == "failed"
        assert summary.skipped == 2
        assert registry.statuses[str(mid.id)] == "skipped"
        assert registry.statuses[str(root.id)] == "skipped"

    async def test_cancelled_branch_descendants_also_skipped(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """FAIL_FAST with a second, still-running branch: the cancel pass
        records TASK_CANCELLED (a cancelled task must not dangle RUNNING —
        workers killed by the executor's cancel can't self-report), so the
        cancelled branch's descendants land in the skip closure too."""
        bad = SyncOnlyTask(name="cb-bad")
        long_running = SyncOnlyTask(name="cb-running")
        downstream = SyncOnlyTask(name="cb-downstream", deps=(long_running,))
        root = SyncOnlyTask(name="cb-root", deps=(bad, downstream))
        registry, executor, store = _setup(
            [bad, long_running, downstream, root], auto_complete=False
        )
        registry.add_task(str(bad.id), status="failed")
        registry.add_task(
            str(long_running.id),
            status="running",
            executor="fake",
            executor_ref="ref-live",
        )
        store.save_task(long_running)

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=FAST_TICK,  # FAIL_FAST default
        )

        assert summary.terminal_status == "failed"
        assert executor.cancelled_refs == ["ref-live"]
        assert registry.statuses[str(long_running.id)] == "cancelled"
        assert registry.statuses[str(downstream.id)] == "skipped"
        assert registry.statuses[str(root.id)] == "skipped"

    async def test_blocked_terminal_in_continue_mode_also_skips(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        dep, root = _chain("skip-cont-dep", "skip-cont-root")
        registry, executor, store = _setup([dep, root], auto_complete=False)
        registry.add_task(str(dep.id), status="failed")

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=TickConfig(
                linger_seconds=0.2,
                poll_interval_seconds=0.01,
                fail_mode=FailMode.CONTINUE,
            ),
        )

        assert summary.terminal_status == "failed"
        assert registry.statuses[str(root.id)] == "skipped"


class _SkipBlocked404Registry(NoOpRegistry):
    """Registry whose skip-blocked endpoint 404s with a given detail."""

    def __init__(self, detail: str):
        super().__init__()
        self.detail = detail

    async def build_skip_blocked_aio(self, build_id) -> list[str]:
        raise NotFoundError(
            "Skip blocked tasks: resource not found", detail=self.detail
        )


class TestSkipBlockedErrorHandling:
    async def test_missing_route_tolerated(self):
        """Old server without the endpoint (FastAPI default 404) → skip
        silently omitted, no raise."""
        summary = TickSummary(outcome="noop")
        await _skip_blocked(_SkipBlocked404Registry("Not Found"), uuid4(), summary)
        assert summary.skipped == 0

    async def test_app_level_404_reraised(self):
        """A 404 raised inside the endpoint (e.g. build no longer exists)
        signals a registry inconsistency and must propagate."""
        with pytest.raises(NotFoundError):
            await _skip_blocked(
                _SkipBlocked404Registry("Build not found"),
                uuid4(),
                TickSummary(outcome="noop"),
            )


class TestRehydrationFallback:
    async def test_store_miss_rehydrates_from_registry(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """A task missing from the pickle store is reconstructed from the
        registry's stored task_data and scheduled — instead of being failed."""
        import stardag as sd

        @sd.task(name="RehydrateFallbackTask")
        def fallback_task(limit: int) -> list[int]:
            return list(range(limit))

        root = fallback_task(limit=3)
        registry = FakeReactiveRegistry(
            root_task_ids=[str(root.id)], auto_complete=True
        )
        registry.add_task(str(root.id))
        registry.metadata_bodies[str(root.id)] = root.model_dump(mode="json")
        store = InMemoryTaskStore(uuid4())  # empty: no pickle for the task

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=(executor := FakeTickExecutor()),
            task_store=store,
            config=FAST_TICK,
        )

        assert executor.spawned == [root.id]
        assert summary.terminal_status == "completed"
        assert summary.failed_recorded == 0
        # Healed back into the store for subsequent ticks.
        assert store.load_task(root.id) is not None

    async def test_store_miss_and_no_metadata_still_fails_task(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """Without rehydratable data either, the stall-prevention failure
        path is preserved."""
        (root,) = _chain("no-rehydrate-root")
        registry, executor, store = _setup([root], auto_complete=False)
        store._tasks.clear()
        # no metadata_bodies entry -> fallback raises -> task failed

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=FAST_TICK,
        )

        assert summary.failed_recorded == 1
        assert summary.terminal_status == "failed"


class TestRehydrationDiagnostics:
    """A rehydration failure names the declared task modules that failed to
    import — "class X unresolved" and "the module defining X blew up on
    import" are the same incident seen from two ends, and only the
    annotation connects them."""

    @pytest.fixture
    def failed_task_module_import(self):
        from stardag.build._task_modules import (
            _reset_import_state_for_tests,
            import_task_modules,
        )

        _reset_import_state_for_tests()
        import_task_modules(["stardag_no_such_declared_task_module"])
        yield
        _reset_import_state_for_tests()

    async def test_failure_note_is_appended_to_the_rehydration_error(
        self,
        caplog,
        failed_task_module_import,
        default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
    ):
        (root,) = _chain("diagnostic-root")
        registry, executor, store = _setup([root], auto_complete=False)
        store._tasks.clear()  # neither a pickle nor rehydratable metadata

        with caplog.at_level("WARNING"):
            await run_tick_aio(
                uuid4(),
                registry=registry,
                task_executor=executor,
                task_store=store,
                config=FAST_TICK,
            )

        messages = "\n".join(record.getMessage() for record in caplog.records)
        assert "could not be rehydrated from registry data" in messages
        assert "stardag_no_such_declared_task_module" in messages
        assert "likely cause" in messages

    async def test_no_note_when_every_task_module_imported(
        self, caplog, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        from stardag.build._task_modules import _reset_import_state_for_tests

        _reset_import_state_for_tests()
        (root,) = _chain("diagnostic-clean-root")
        registry, executor, store = _setup([root], auto_complete=False)
        store._tasks.clear()

        with caplog.at_level("WARNING"):
            await run_tick_aio(
                uuid4(),
                registry=registry,
                task_executor=executor,
                task_store=store,
                config=FAST_TICK,
            )

        messages = "\n".join(record.getMessage() for record in caplog.records)
        assert "could not be rehydrated from registry data" in messages
        assert "failed to import" not in messages


class TestExecutorMetadataRecording:
    """The post-spawn ref-recording start carries the handle's
    executor_metadata (and drops it for pre-metadata registries)."""

    class MetadataTickExecutor(FakeTickExecutor):
        METADATA = {"kind": "modal", "app_name": "tick-app"}

        async def submit_detached(self, task: BaseTask) -> DetachedHandle:
            handle = await super().submit_detached(task)
            return DetachedHandle(
                executor=handle.executor,
                ref=handle.ref,
                wait=handle.wait,
                executor_metadata=self.METADATA,
            )

    async def test_post_spawn_start_carries_handle_metadata(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        (root,) = _chain("meta-root")
        registry, _, store = _setup([root])

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=self.MetadataTickExecutor(),
            task_store=store,
            config=FAST_TICK,
        )

        assert summary.spawned == 1
        assert registry.start_metadata[str(root.id)] == (
            self.MetadataTickExecutor.METADATA
        )


class TestAcquiringStartExecutorMetadata:
    """The limits-acquiring TASK_STARTED (recorded BEFORE the spawn) carries
    the executor metadata resolvable pre-spawn, closing the acquire→spawn
    window where a RUNNING task would otherwise show blank executor info."""

    PRE_SPAWN_METADATA = {"kind": "modal", "app_name": "tick-app"}

    class PreSpawnMetadataExecutor(FakeTickExecutor):
        async def get_executor_metadata(self, task: BaseTask):
            return TestAcquiringStartExecutorMetadata.PRE_SPAWN_METADATA

    class AcquireRecordingRegistry(FakeReactiveRegistry):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.acquire_metadata: dict[str, dict | None] = {}

        async def _acquire_limits(
            self,
            build_id,
            task,
            executor=None,
            executor_ref=None,
            executor_metadata=None,
            limit_keys=None,
            claim_ttl_seconds=None,
        ):
            self.acquire_metadata[str(task.id)] = executor_metadata
            return await super()._acquire_limits(
                build_id,
                task,
                executor=executor,
                executor_ref=executor_ref,
                executor_metadata=executor_metadata,
                limit_keys=limit_keys,
                claim_ttl_seconds=claim_ttl_seconds,
            )

    async def test_acquiring_start_carries_metadata(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        (root,) = _chain("acquire-meta-root")
        registry = self.AcquireRecordingRegistry(
            root_task_ids=[str(root.id)], auto_complete=True
        )
        registry.add_task(str(root.id))
        registry.limits["gpu"] = 1
        store = InMemoryTaskStore(uuid4())
        store.save_tasks([root])
        config = TickConfig(
            linger_seconds=0.3,
            poll_interval_seconds=0.01,
            limit_key_selector=lambda t: ["gpu"],
        )

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=self.PreSpawnMetadataExecutor(),
            task_store=store,
            config=config,
        )

        assert summary.spawned == 1
        assert registry.acquire_metadata[str(root.id)] == self.PRE_SPAWN_METADATA


class ClaimingReactiveRegistry(FakeReactiveRegistry):
    """FakeReactiveRegistry with a scriptable cross-build claim race.

    ``claim_race_once`` simulates the race the claim closes: the frontier
    snapshot says PENDING, but by claim time another build's scheduler has
    already started (and instantly completed) the task.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.claim_race_once: set[str] = set()

    async def task_start_claim_aio(
        self,
        build_id,
        task,
        executor=None,
        executor_ref=None,
        executor_metadata=None,
        limit_keys=None,
        claim_ttl_seconds=None,
        *,
        claim=True,
    ):
        from stardag.registry import StartClaimResult

        tid = str(task.id)
        # Gated on ``claim`` like the server and the other doubles: an
        # unclaiming acquire cannot be denied ``already_running``, so a
        # double that raced it regardless could not emulate the limiter.
        if claim and tid in self.claim_race_once:
            # "Another build" won this task just before us and its instant
            # worker completed it (completion wakes our scheduler).
            self.claim_race_once.discard(tid)
            self.calls.append(("start_claim", tid))
            self.statuses[tid] = "completed"
            self.needs_tick = True
            return StartClaimResult(
                started=False,
                denied_reason="already_running",
                executor="fake",
                executor_ref="fc-other-build",
            )
        return await super().task_start_claim_aio(
            build_id,
            task,
            executor=executor,
            executor_ref=executor_ref,
            executor_metadata=executor_metadata,
            limit_keys=limit_keys,
            claim_ttl_seconds=claim_ttl_seconds,
            claim=claim,
        )


class TestTickClaims:
    async def test_claim_race_lost_then_build_completes(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """The tick loses the claim to 'another build' — the task stays in
        the frontier, no duplicate spawn happens, no false stuck-failure,
        and the build completes once the winner's completion is observed."""
        (root,) = _chain("tick-claim-race")
        registry = ClaimingReactiveRegistry(
            root_task_ids=[str(root.id)], auto_complete=True
        )
        registry.add_task(str(root.id))
        registry.claim_race_once.add(str(root.id))
        store = InMemoryTaskStore(uuid4())
        store.save_tasks([root])
        executor = FakeTickExecutor()

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=FAST_TICK,
        )

        assert summary.outcome == "terminal"
        assert summary.terminal_status == "completed"
        assert summary.claim_denied == 1
        assert executor.spawned == []  # the duplicate spawn never happened
        assert registry.build_status == "completed"

    async def test_claims_and_limits_compose(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """With claims on, limit denials still resolve via the claim start
        (single acquiring call) and the chain completes under a 1-slot key."""
        a = SyncOnlyTask(name="tick-claim-a")
        b = SyncOnlyTask(name="tick-claim-b")
        root = SyncOnlyTask(name="tick-claim-root", deps=(a, b))
        registry = ClaimingReactiveRegistry(
            root_task_ids=[str(root.id)], auto_complete=True
        )
        for task in (a, b, root):
            registry.add_task(
                str(task.id),
                upstreams={str(d.id) for d in flatten_task_struct(task.requires())},
            )
        registry.limits["one-slot"] = 1
        store = InMemoryTaskStore(uuid4())
        store.save_tasks([a, b, root])
        executor = FakeTickExecutor()

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=TickConfig(
                linger_seconds=0.5,
                poll_interval_seconds=0.01,
                limit_key_selector=lambda t: ["one-slot"],
            ),
        )

        assert summary.outcome == "terminal"
        assert summary.terminal_status == "completed"
        assert summary.spawned == 3
        # all acquisitions went through the claiming start
        assert any(m == "start_claim" for (m, _) in registry.calls)


class TestExternalBlockers:
    """Terminal detection when the blocker lives outside this build (#208 A1).

    Dependency gating is environment-global while ``running`` and
    ``status_counts`` are build-scoped, so a task another build is executing
    gates this build's tasks while appearing in neither count. Read as
    "nothing runnable, nothing running", that shape used to fail the build
    outright.
    """

    BLOCKER_ID = "blocking-task-id"

    def _blocked_build(
        self,
        *,
        blocker_status: str = "running",
        blocker_age_seconds: float | None = 60.0,
        blocker_expires_in_seconds: float | None = None,
        in_build: bool = False,
        blocker_attempts: int = 0,
        owner_build_id: "UUID | None" = None,
        owner_build_status: "str | None" = "running",
        owner_build_known: bool = True,
    ):
        """A build whose only task is gated by an upstream it does not own."""
        (root,) = _chain("ext-blocked-root")
        registry, executor, store = _setup([root], auto_complete=False)
        registry.add_blocking_task(
            self.BLOCKER_ID,
            blocks={str(root.id)},
            status=blocker_status,
            status_at=(
                None
                if blocker_age_seconds is None
                else datetime.now(timezone.utc) - timedelta(seconds=blocker_age_seconds)
            ),
            expires_at=(
                None
                if blocker_expires_in_seconds is None
                else datetime.now(timezone.utc)
                + timedelta(seconds=blocker_expires_in_seconds)
            ),
            in_build=in_build,
            attempt_count=blocker_attempts,
            owner_build_id=owner_build_id,
            owner_build_status=owner_build_status,
            owner_build_known=owner_build_known,
            namespace="pipelines",
            name="Ingest",
        )
        return root, registry, executor, store

    async def test_running_blocker_in_another_build_waits_instead_of_failing(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """The exact A1 repro: this build has nothing actionable and nothing
        running because its task waits on an upstream another build is
        executing. That upstream's completion will unblock it, so the tick
        must wait (as it does for a concurrency-limit denial) rather than
        fail the build."""
        _, registry, executor, store = self._blocked_build()

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=FAST_TICK,
        )

        assert summary.outcome == "lingered_out"
        assert registry.build_status == "running"
        assert not any(method == "build_fail" for (method, _) in registry.calls)
        assert executor.spawned == []
        assert (summary.external_blockers, summary.external_blockers_waited) == (1, 1)
        assert summary.external_blockers_fatal == 0

    async def test_running_blocker_with_a_live_claim_waits(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """The claim's expiry is in the future: somebody is executing it and
        the server still honours their claim. Wait — no lookup, no timer."""
        _, registry, executor, store = self._blocked_build(
            blocker_expires_in_seconds=3600
        )

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=FAST_TICK,
        )

        assert summary.outcome == "lingered_out"
        assert summary.external_blockers_waited == 1
        assert summary.external_blockers_fatal == 0
        assert registry.build_status == "running"
        # A RUNNING blocker is decided from its claim alone.
        assert registry.build_get_calls == []

    async def test_running_blocker_with_a_lapsed_claim_fails(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """The claim lapsed: it is not "presumed" abandoned, it provably is
        — the server will hand it to the next claimant. This build is not
        that claimant, so it still fails, but now with certainty, naming the
        blocking task and the build that owns it."""
        _, registry, executor, store = self._blocked_build(
            blocker_age_seconds=60 * 60 * 24,
            blocker_expires_in_seconds=-60,  # lapsed a minute ago
        )

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=FAST_TICK,
        )

        assert summary.terminal_status == "failed"
        assert summary.external_blockers_fatal == 1
        assert summary.external_blockers_waited == 0
        assert registry.build_get_calls == []
        message = registry.build_error_message or ""
        assert "pipelines.Ingest" in message  # the blocking task
        assert self.BLOCKER_ID in message
        assert str(registry.status_build_id[self.BLOCKER_ID]) in message  # its owner
        assert "execution claim lapsed" in message
        # A reset cannot take a claim, lapsed or not, so the remedy is a
        # release — and it is the only extra clause the remedy carries.
        assert "release it first" in message
        # Named in surfaces the reader has, not as a REST route they would have
        # to find a base URL and a token for.
        assert "'Release claim' action" in message
        assert "stardag tasks cancel <owning-build-id> <blocking-task-id>" in message
        assert "/api/v1" not in message
        # And it has to say *whose* build id: any build in the environment is
        # accepted, but addressing it to *this* build makes this build the owner
        # of the cancelled status, at which point the task stops being an
        # external blocker and the reset it would have got never happens.
        assert "the build that owns the blocker (named above), not this one" in message

    async def test_running_blocker_without_an_expiry_waits(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """NULL expiry — an older server, or a start recorded before the
        column. That is the server's own encoding of "never lapses", not
        evidence of death: waiting keeps a live blocker from failing a
        healthy build, and the log line says the wait cannot be shown to
        end. Deliberate; see _classify_external_blockers."""
        _, registry, executor, store = self._blocked_build(
            blocker_age_seconds=60 * 60 * 24 * 365,  # a year, and still waited on
            blocker_expires_in_seconds=None,
        )

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=FAST_TICK,
        )

        assert summary.outcome == "lingered_out"
        assert summary.external_blockers_waited == 1
        assert summary.external_blockers_fatal == 0
        assert registry.build_status == "running"

    @pytest.mark.parametrize("owner_build_status", ["completed", "running"])
    async def test_a_running_blockers_owning_build_is_never_consulted(
        self,
        owner_build_status: str,
        default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
    ):
        """A live owning build proves nothing about one of its claims, and a
        terminal one does not release them — so for a RUNNING blocker the
        owner's status is not consulted in either direction."""
        _, registry, executor, store = self._blocked_build(
            blocker_expires_in_seconds=3600,
            owner_build_status=owner_build_status,
        )

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=FAST_TICK,
        )

        assert summary.outcome == "lingered_out"
        assert registry.build_get_calls == []

    @pytest.mark.parametrize("blocker_status", ["pending", "suspended"])
    async def test_owner_driven_blocker_of_a_terminal_build_fails_immediately(
        self,
        blocker_status: str,
        default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
    ):
        """PENDING and SUSPENDED say "the owning build is going to move this".
        Once that build has gone terminal, nobody is, and waiting would be
        waiting forever. Fail now, naming the task, the build that owns it and
        the one remedy there is."""
        _, registry, executor, store = self._blocked_build(
            blocker_status=blocker_status, owner_build_status="completed"
        )

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=FAST_TICK,
        )

        assert summary.terminal_status == "failed"
        assert summary.external_blockers_fatal == 1
        assert summary.external_blockers_waited == 0
        message = registry.build_error_message or ""
        assert "pipelines.Ingest" in message
        assert blocker_status.upper() in message
        assert f"under build {registry.status_build_id[self.BLOCKER_ID]}" in message
        # One remedy, since the blocker is in this build's plan: re-trigger.
        # Only a RUNNING blocker needs the cancel-first hint.
        assert "Re-trigger this build" in message
        assert "release it first" not in message

    @pytest.mark.parametrize("owner_build_status", ["running", "pending"])
    async def test_non_running_blocker_of_a_live_build_waits(
        self,
        owner_build_status: str,
        default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
    ):
        """A PENDING blocker belonging to a build that is still live *will* be
        scheduled by that build — failing here would be a brand-new spurious
        failure, the very class of bug being fixed. A build the server still
        reports as pending counts as live too: it may yet start, and the
        staleness bound is what keeps that from being an unbounded wait."""
        _, registry, executor, store = self._blocked_build(
            blocker_status="pending", owner_build_status=owner_build_status
        )

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=FAST_TICK,
        )

        assert summary.outcome == "lingered_out"
        assert registry.build_status == "running"
        assert summary.external_blockers_waited == 1
        assert summary.external_blockers_fatal == 0

    @pytest.mark.parametrize("blocker_status", ["pending", "suspended"])
    async def test_a_non_running_blocker_carries_no_expiry_so_the_owner_is_asked(
        self,
        blocker_status: str,
        default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
    ):
        """The two-row collapse applies to RUNNING blockers ONLY. A SUSPENDED
        (or PENDING) task holds no execution claim, so the server clears the
        expiry with it and there is nothing to read — the owning-build lookup
        is not kept out of caution, it is the only evidence that exists. The
        abandoned-SUSPENDED wedge is real and still has to be decided."""
        _, registry, executor, store = self._blocked_build(
            blocker_status=blocker_status,
            blocker_age_seconds=60 * 60 * 24 * 30,  # age is not the question
            owner_build_status="running",
        )

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=FAST_TICK,
        )

        assert summary.outcome == "lingered_out"
        assert summary.external_blockers_waited == 1
        assert len(registry.build_get_calls) == 1  # the lookup did happen

    async def test_blocker_with_no_status_owning_build_fails(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """No build owns the blocker's status (a row predating status
        denormalisation), so no status-moving event was ever recorded against
        it — there is no evidence anyone intends to run it, and no build to
        ask. Fail, with the remediation intact."""
        _, registry, executor, store = self._blocked_build(blocker_status="pending")
        registry.status_build_id[self.BLOCKER_ID] = None

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=FAST_TICK,
        )

        assert summary.terminal_status == "failed"
        assert summary.external_blockers_fatal == 1
        assert registry.build_get_calls == []  # nothing to look up
        message = registry.build_error_message or ""
        assert "no build owns its status" in message
        # The remedy needs no owning build id to be actionable: the blocker is
        # in *this* build's plan, so re-triggering *this* build resets it.
        assert "Re-trigger this build" in message

    async def test_failed_owner_lookup_fails_without_propagating(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """An unresolvable owner (deleted build, unreachable registry) is not
        evidence of life: the build fails with a precise message rather than
        the lookup error escaping the tick."""
        _, registry, executor, store = self._blocked_build(
            blocker_status="pending", owner_build_known=False
        )

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=FAST_TICK,
        )

        assert summary.terminal_status == "failed"
        assert summary.external_blockers_fatal == 1
        assert "status is unknown" in (registry.build_error_message or "")

    async def test_server_not_reporting_build_status_fails(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """Same treatment for a server (or custom registry) that doesn't
        report the derived build status: the field defaults to None, unknown
        is not evidence of life, and the message says so."""
        _, registry, executor, store = self._blocked_build(
            blocker_status="pending", owner_build_status=None
        )

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=FAST_TICK,
        )

        assert summary.terminal_status == "failed"
        assert "status is unknown" in (registry.build_error_message or "")

    async def test_owner_status_is_resolved_once_per_build(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """A wide DAG stalled behind one build yields one blocker entry per
        blocked edge, all naming the same owner — that must cost one request,
        not one per entry."""
        root, registry, executor, store = self._blocked_build(
            blocker_status="pending", owner_build_id=(owner := uuid4())
        )
        sibling = SyncOnlyTask(name="ext-memo-sibling")
        store.save_task(sibling)
        registry.add_task(str(sibling.id), status="pending")
        registry.add_blocking_task(
            "second-blocker",
            blocks={str(sibling.id)},
            status="pending",
            owner_build_id=owner,  # same owner as the first blocker
            namespace="pipelines",
            name="Transform",
        )
        registry.build_get_calls.clear()

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=TickConfig(linger_seconds=0.0, poll_interval_seconds=0.01),
        )

        assert summary.external_blockers_waited == 2
        assert registry.build_get_calls == [owner]

    async def test_cancelled_in_build_blocker_is_retried_not_failed(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """Builds collaborate: a task in *this build's own plan* is this
        build's to run, whatever build last touched it.

        The motivating shape is fail-fast. Build A starts a shared task,
        hits an unrelated failure, and cascade-cancels the tasks it started
        — correctly, since it owns those claims. The cancel releases the
        claim, which is the whole point. But the task is left CANCELLED,
        which is not schedulable, so build B — which shares the dependency
        and has failed at nothing — used to die on it too. One build's
        fail-fast became every overlapping build's failure.

        Nothing owns the task now: no claim, no live execution. It is in B's
        plan, so B resets it and runs it.
        """
        _, registry, executor, store = self._blocked_build(
            blocker_status="cancelled", in_build=True
        )

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=TickConfig(
                linger_seconds=0.2,
                poll_interval_seconds=0.01,
                fail_mode=FailMode.CONTINUE,
            ),
        )

        # Reset, not failed: the next tick finds it actionable.
        assert summary.terminal_status is None
        assert ("retry", self.BLOCKER_ID) in registry.calls
        assert registry.statuses[self.BLOCKER_ID] == "pending"
        assert registry.build_error_message is None

    async def test_a_shared_cancelled_blocker_is_reset_once_per_task(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """The frontier reports one entry per (blocked, blocker) *edge*.

        A cancelled upstream shared by several of this build's tasks therefore
        arrives once per dependent — the normal shape for the fan-out this path
        exists to unblock, and a diamond is enough to produce it. Resetting per
        entry would call retry N times on one task: the calls after the first
        hit a row that is already PENDING, so they fail and log, and
        ``in_build_blockers_reset`` would count edges rather than tasks.
        """
        root, registry, executor, store = self._blocked_build(
            blocker_status="cancelled", in_build=True
        )
        # A second task of this build gated by the *same* blocker.
        sibling = SyncOnlyTask(name="shared-blocker-sibling")
        store.save_task(sibling)
        registry.add_task(str(sibling.id), status="pending")
        registry.upstreams.setdefault(str(sibling.id), set()).add(self.BLOCKER_ID)

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=TickConfig(
                linger_seconds=0.2,
                poll_interval_seconds=0.01,
                fail_mode=FailMode.CONTINUE,
            ),
        )

        assert summary.terminal_status is None
        # Two edges reported, one task reset.
        assert summary.external_blockers == 0  # short-circuited by the reset
        retries = [
            call for call in registry.calls if call == ("retry", self.BLOCKER_ID)
        ]
        assert len(retries) == 1, registry.calls
        assert summary.in_build_blockers_reset == 1
        assert registry.statuses[self.BLOCKER_ID] == "pending"

    async def test_in_build_retry_respects_the_attempt_budget(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """Resetting an in-plan blocker must not become an infinite loop.

        A task that genuinely fails every time would otherwise be reset,
        rerun and re-failed forever. The budget that already bounds ordinary
        retries bounds this one too, and the build fails once it is spent —
        which is the honest outcome, since nothing will move the task.
        """
        _, registry, executor, store = self._blocked_build(
            blocker_status="cancelled", in_build=True, blocker_attempts=5
        )

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=TickConfig(
                linger_seconds=0.2,
                poll_interval_seconds=0.01,
                fail_mode=FailMode.CONTINUE,
                max_attempts=2,
            ),
        )

        assert summary.terminal_status == "failed"
        assert ("retry", self.BLOCKER_ID) not in registry.calls
        message = registry.build_error_message or ""
        assert "Blocked by" in message
        assert "attempt budget in this build is spent" in message

    @pytest.mark.parametrize("blocker_status", ["failed", "skipped"])
    async def test_a_result_in_this_builds_plan_is_left_to_fail_mode(
        self,
        blocker_status: str,
        default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
    ):
        """S1, and the line the collaboration rule stops at: a task in this
        build's plan is this build's to *run*, but only a CANCELLED status is
        a revocation of permission to run. FAILED and SKIPPED are results.

        Build A ran the shared task and it failed. Nothing about that failure
        belongs to B: resetting it would rerun a task that just told the
        environment it does not work, on nobody's request, and would override
        the ``fail_mode`` B was triggered with. B fails instead, naming the
        blocker — and a re-trigger, where the user *does* ask, resets it.

        Budget deliberately intact (attempts 0 of 2): the point is the status,
        not an exhausted retry allowance.
        """
        _, registry, executor, store = self._blocked_build(
            blocker_status=blocker_status, in_build=True, blocker_attempts=0
        )

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=TickConfig(
                linger_seconds=0.2,
                poll_interval_seconds=0.01,
                # CONTINUE, or FAIL_FAST would fail the build on the status
                # count before terminal detection ever looks at a blocker.
                fail_mode=FailMode.CONTINUE,
                max_attempts=2,
            ),
        )

        assert summary.terminal_status == "failed"
        assert ("retry", self.BLOCKER_ID) not in registry.calls
        assert registry.statuses[self.BLOCKER_ID] == blocker_status
        # A result influences neither half of the wait-or-fail decision: the
        # build's own terminal logic owns the outcome.
        assert summary.external_blockers == 1
        assert summary.external_blockers_waited == 0
        assert summary.external_blockers_fatal == 0
        assert summary.in_build_blockers_reset == 0
        message = registry.build_error_message or ""
        assert "pipelines.Ingest" in message
        assert "a result rather than a revocation" in message
        assert "Re-trigger this build" in message

    def _suspended_shared_task(
        self, *, owner_build_status: str, child_claim_lapsed: bool
    ):
        """S2's shape: a shared task suspended on a dynamic child of its own.

        The child is registered into the *owning* build's plan only, which is
        what happens when this build registered before the owner's worker
        yielded. It also keeps the suspended parent out of ``actionable``: a
        suspended task with every upstream complete is simply schedulable, and
        that is not the state S2 is about.
        """
        _, registry, executor, store = self._blocked_build(
            blocker_status="suspended",
            in_build=True,
            owner_build_id=(owner := uuid4()),
            owner_build_status=owner_build_status,
        )
        registry.add_blocking_task(
            "dynamic-child",
            blocks={self.BLOCKER_ID},
            status="running",
            owner_build_id=owner,
            # Same owner, so the same status — the fake keeps one entry per
            # build and the default would overwrite the parent's.
            owner_build_status=owner_build_status,
            expires_at=datetime.now(timezone.utc)
            + timedelta(minutes=-1 if child_claim_lapsed else 60),
            namespace="pipelines",
            name="Child",
        )
        return registry, executor, store

    async def test_suspended_blocker_in_this_builds_plan_is_waited_on(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """S2: build A is mid-flight through the shared task's dynamic deps.

        A ran the shared task, its worker registered dynamic dependencies,
        yielded, and returned — leaving the task SUSPENDED with the children
        in A's plan. The task is in B's plan too, but resetting it would rerun
        all of its pre-yield work for nothing while A is legitimately making
        progress, and would race A's own scheduling of the children.

        So B waits. Bounded, not open-ended: SUSPENDED persists only while A
        progresses the children, and A stalling on one of them stalls on a
        RUNNING task whose claim expires.
        """
        registry, executor, store = self._suspended_shared_task(
            owner_build_status="running", child_claim_lapsed=False
        )

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=TickConfig(
                linger_seconds=0.2,
                poll_interval_seconds=0.01,
                fail_mode=FailMode.CONTINUE,
            ),
        )

        assert summary.terminal_status is None
        assert registry.build_status == "running"
        assert ("retry", self.BLOCKER_ID) not in registry.calls
        assert registry.statuses[self.BLOCKER_ID] == "suspended"
        assert summary.in_build_blockers_reset == 0
        # Both edges are waited on: the suspended parent (its owner is live)
        # and the running child (its claim is live).
        assert summary.external_blockers_waited == 2
        assert summary.external_blockers_fatal == 0

    async def test_suspended_blocker_of_a_dead_owner_fails(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """The other end of S2's bound, and why the wait is not a hang.

        The owning build died while its dynamic child was running, so the
        child's claim lapses and the suspended parent has nobody left to
        progress it. Both are fatal on their own evidence — the claim for the
        child, the owner's status for the parent — and the build fails naming
        them instead of waiting forever. A re-trigger resets the whole chain.
        """
        registry, executor, store = self._suspended_shared_task(
            owner_build_status="failed", child_claim_lapsed=True
        )

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=TickConfig(
                linger_seconds=0.2,
                poll_interval_seconds=0.01,
                fail_mode=FailMode.CONTINUE,
            ),
        )

        assert summary.terminal_status == "failed"
        assert ("retry", self.BLOCKER_ID) not in registry.calls
        assert summary.external_blockers_fatal == 2
        assert summary.external_blockers_waited == 0
        message = registry.build_error_message or ""
        assert "its owning build is failed" in message  # the suspended parent
        assert "execution claim lapsed" in message  # its dynamic child

    async def test_in_build_blocker_that_cannot_be_retried_still_fails(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """Not every in-plan blocker is recoverable. One in a status no
        retry moves is named and the build fails, rather than idling."""
        _, registry, executor, store = self._blocked_build(
            blocker_status="unregistered", in_build=True
        )

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=TickConfig(
                linger_seconds=0.2,
                poll_interval_seconds=0.01,
                fail_mode=FailMode.CONTINUE,
            ),
        )

        assert summary.terminal_status == "failed"
        assert summary.external_blockers == 1
        assert summary.external_blockers_waited == 0
        assert summary.external_blockers_fatal == 0
        message = registry.build_error_message or ""
        assert "No runnable or running tasks left" in message
        assert "Blocked by" in message
        assert "pipelines.Ingest" in message

    async def test_a_fatal_blocker_wins_over_a_waitable_one(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """One blocker nothing will ever run means the build cannot complete,
        whatever else it is also waiting on."""
        root, registry, executor, store = self._blocked_build()
        registry.add_blocking_task(
            "dead-blocker",
            blocks={str(root.id)},
            status="suspended",
            owner_build_status="cancelled",
            namespace="pipelines",
            name="Abandoned",
            status_at=datetime.now(timezone.utc),
        )

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=FAST_TICK,
        )

        assert summary.terminal_status == "failed"
        assert summary.external_blockers == 2
        assert summary.external_blockers_waited == 1
        assert summary.external_blockers_fatal == 1
        # Only the fatal one is named as the reason to fail.
        message = registry.build_error_message or ""
        assert "pipelines.Abandoned" in message
        assert "pipelines.Ingest" not in message

    async def test_truncated_blocker_list_is_flagged_in_the_message(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """The server caps its blocker list; the failure must not read as an
        exhaustive account of what is holding the build back."""
        _, registry, executor, store = self._blocked_build(
            blocker_status="suspended", owner_build_status="failed"
        )
        registry.blocked_by_external_truncated = True

        await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=FAST_TICK,
        )

        message = registry.build_error_message or ""
        assert "capped the blocker list" in message

    async def test_older_server_without_the_fields_behaves_exactly_as_before(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """A server predating blocked_by_external leaves the fields at their
        defaults, and terminal detection degrades to the pre-fix failure —
        the bug is unfixable client-side there, but nothing regresses."""
        _, registry, executor, store = self._blocked_build()
        registry.serves_blocked_by_external = False

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=FAST_TICK,
        )

        assert summary.terminal_status == "failed"
        assert summary.external_blockers == 0
        assert summary.external_blockers_waited == 0
        assert summary.external_blockers_fatal == 0
        assert "No runnable or running tasks left" in (
            registry.build_error_message or ""
        )

    async def test_healthy_build_never_evaluates_blockers(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """The frontier reports blockers only while a build looks stalled, so
        a build that runs to completion records none."""
        dep, root = _chain("ext-healthy-dep", "ext-healthy-root")
        registry, executor, store = _setup([dep, root])

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=FAST_TICK,
        )

        assert summary.terminal_status == "completed"
        assert summary.external_blockers == 0

    async def test_blocker_completion_releases_the_waiting_build(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """End of the wait: once the owning build completes the blocker (and
        wakes this scheduler), the same lingering tick schedules the task it
        would previously have failed the build over."""
        root, registry, executor, store = self._blocked_build()
        registry.auto_complete = True

        async def complete_blocker_soon() -> None:
            await asyncio.sleep(0.05)
            registry.statuses[self.BLOCKER_ID] = "completed"
            registry.needs_tick = True

        waiter = asyncio.create_task(complete_blocker_soon())
        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=TickConfig(linger_seconds=1.0, poll_interval_seconds=0.01),
        )
        await waiter

        assert summary.terminal_status == "completed"
        assert executor.spawned == [root.id]
        assert summary.external_blockers_waited >= 1


class TestTickSummaryReporting:
    """B6: a tick's own account of what it did must outlive its container.

    Reporting is strictly best-effort — it sits at the end of every tick,
    and observability failing must never fail a tick or change its
    outcome. These tests pin both halves: that the summaries do get
    reported, and that nothing about reporting can leak into the result.
    """

    @pytest.fixture(autouse=True)
    def _reset_route_flag(self):
        """The missing-route latch is process-global; isolate the tests."""
        reactive_module._tick_summary_route_missing = False
        yield
        reactive_module._tick_summary_route_missing = False

    async def _run(
        self,
        registry: FakeReactiveRegistry,
        executor,
        store,
        config: TickConfig | None = None,
    ) -> TickSummary:
        return await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=config or FAST_TICK,
        )

    async def test_terminal_tick_is_reported(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        dep, root = _chain("report-dep", "report-root")
        registry, executor, store = _setup([dep, root])

        summary = await self._run(registry, executor, store)

        assert summary.outcome == "terminal"
        assert len(registry.reported_tick_summaries) == 1
        reported = registry.reported_tick_summaries[0]
        # The whole dataclass rides, so a field added later needs no server
        # change (the summary is stored as an open blob).
        assert reported["outcome"] == "terminal"
        assert reported["terminal_status"] == "completed"
        assert reported["spawned"] == 2
        assert set(reported) == set(vars(summary))

    async def test_lingered_out_tick_is_reported(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """A tick that found nothing to do still says so."""
        (task,) = _chain("linger-only")
        registry, executor, store = _setup([task], auto_complete=False)
        # Nothing actionable and something running -> not terminal, so the
        # tick lingers and exits on its deadline.
        registry.statuses[str(task.id)] = "running"
        registry.refs[str(task.id)] = ("fake", "ref-live")
        executor.probe_statuses["ref-live"] = DetachedExecutionStatus.RUNNING

        summary = await self._run(registry, executor, store)

        assert summary.outcome == "lingered_out"
        assert [s["outcome"] for s in registry.reported_tick_summaries] == [
            "lingered_out"
        ]

    async def test_lease_held_tick_is_reported(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """Contention is signal: many of these means ticks are piling up."""
        dep, root = _chain("held-dep", "held-root")
        registry, executor, store = _setup([dep, root], lease_acquired=False)

        summary = await self._run(registry, executor, store)

        assert summary.outcome == "lease_held"
        assert [s["outcome"] for s in registry.reported_tick_summaries] == [
            "lease_held"
        ]

    async def test_not_reactive_tick_is_not_reported(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """A stray tick on a non-reactive build learnt nothing worth keeping."""
        dep, root = _chain("stray-dep", "stray-root")
        registry, executor, store = _setup([dep, root])
        registry.reactive_app_name = None

        summary = await self._run(registry, executor, store)

        assert summary.outcome == "not_reactive"
        assert registry.reported_tick_summaries == []

    async def test_disabled_by_config(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        dep, root = _chain("off-dep", "off-root")
        registry, executor, store = _setup([dep, root])

        summary = await self._run(
            registry,
            executor,
            store,
            config=TickConfig(
                linger_seconds=0.3,
                poll_interval_seconds=0.01,
                report_tick_summaries=False,
            ),
        )

        assert summary.outcome == "terminal"
        assert registry.reported_tick_summaries == []

    async def test_reporting_failure_does_not_affect_the_tick(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """The contract: a broken registry must not fail or alter a tick."""
        dep, root = _chain("raise-dep", "raise-root")
        registry, executor, store = _setup([dep, root])
        registry.tick_summary_error = RuntimeError("registry exploded")

        summary = await self._run(registry, executor, store)

        assert summary.outcome == "terminal"
        assert summary.terminal_status == "completed"
        assert summary.spawned == 2
        assert registry.build_status == "completed"
        # It was attempted — the failure is swallowed, not skipped.
        assert len(registry.reported_tick_summaries) == 1

    async def test_missing_route_is_tolerated_and_latched(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """An older server 404s the route; don't pay for it every tick."""
        dep, root = _chain("route-dep", "route-root")
        registry, executor, store = _setup([dep, root])
        # FastAPI's generic missing-route body — version skew, not an error.
        registry.tick_summary_error = NotFoundError(
            "Report tick summary: resource not found", detail="Not Found"
        )

        summary = await self._run(registry, executor, store)
        assert summary.outcome == "terminal"
        assert len(registry.reported_tick_summaries) == 1
        assert reactive_module._tick_summary_route_missing is True

        # Second tick on a fresh build: the latch keeps it from re-trying.
        dep2, root2 = _chain("route-dep-2", "route-root-2")
        registry2, executor2, store2 = _setup([dep2, root2])
        summary2 = await self._run(registry2, executor2, store2)
        assert summary2.outcome == "terminal"
        assert registry2.reported_tick_summaries == []

    async def test_resource_404_does_not_latch(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """A build that vanished is not a reason to stop reporting others."""
        dep, root = _chain("gone-dep", "gone-root")
        registry, executor, store = _setup([dep, root])
        registry.tick_summary_error = NotFoundError(
            "Report tick summary: resource not found", detail="Build not found"
        )

        summary = await self._run(registry, executor, store)

        assert summary.outcome == "terminal"
        assert reactive_module._tick_summary_route_missing is False

    async def test_crashed_tick_is_reported_and_the_exception_propagates(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """A crashed tick is the most informative "why did this stall?" answer.

        Recording it must not change what the caller sees: the original
        exception is re-raised, with its type and message captured.
        """
        dep, root = _chain("crash-dep", "crash-root")
        registry, executor, store = _setup([dep, root])
        registry.frontier_error = RuntimeError("frontier query exploded")

        with pytest.raises(RuntimeError, match="frontier query exploded"):
            await self._run(registry, executor, store)

        assert len(registry.reported_tick_summaries) == 1
        reported = registry.reported_tick_summaries[0]
        assert reported["outcome"] == "error"
        assert reported["error_type"] == "RuntimeError"
        assert reported["error_message"] == "frontier query exploded"

    async def test_error_message_is_bounded(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """An unbounded message would blow the server's 8 KiB summary cap —
        turning a recorded failure into no record at all."""
        dep, root = _chain("huge-dep", "huge-root")
        registry, executor, store = _setup([dep, root])
        registry.frontier_error = RuntimeError("x" * 50_000)

        with pytest.raises(RuntimeError):
            await self._run(registry, executor, store)

        reported = registry.reported_tick_summaries[0]
        message = reported["error_message"]
        assert len(message) == reactive_module._MAX_ERROR_MESSAGE_CHARS
        assert message.endswith(reactive_module._TRUNCATION_MARKER)
        # The whole summary stays well inside the server's cap.
        assert len(json.dumps(reported, separators=(",", ":")).encode()) < 8192

    async def test_failing_to_report_a_crash_does_not_mask_it(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """A failure to record the failure is swallowed, never substituted."""
        dep, root = _chain("mask-dep", "mask-root")
        registry, executor, store = _setup([dep, root])
        registry.frontier_error = RuntimeError("the original problem")
        registry.tick_summary_error = RuntimeError("and the reporter died too")

        with pytest.raises(RuntimeError, match="the original problem"):
            await self._run(registry, executor, store)

    async def test_crash_reporting_respects_the_config_toggle(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        dep, root = _chain("crash-off-dep", "crash-off-root")
        registry, executor, store = _setup([dep, root])
        registry.frontier_error = RuntimeError("boom")

        with pytest.raises(RuntimeError, match="boom"):
            await self._run(
                registry,
                executor,
                store,
                config=TickConfig(
                    linger_seconds=0.3,
                    poll_interval_seconds=0.01,
                    report_tick_summaries=False,
                ),
            )

        assert registry.reported_tick_summaries == []


# =============================================================================
# Concurrent DAG discovery
# =============================================================================


class TrackedTask(SyncOnlyTask):
    """SyncOnlyTask whose completion check is observable and suspends.

    The suspension is what makes concurrency measurable at all: the
    in-memory target answers synchronously, so without it a "concurrent"
    walk and a serial one are indistinguishable.
    """

    # Class-level because discovery constructs nothing — the tracker has to
    # outlive individual instances and be shared across the whole walk.
    tracker: typing.ClassVar[dict[str, int]] = {}

    async def complete_aio(self) -> bool:
        TrackedTask.tracker["in_flight"] = TrackedTask.tracker.get("in_flight", 0) + 1
        TrackedTask.tracker["max_in_flight"] = max(
            TrackedTask.tracker.get("max_in_flight", 0),
            TrackedTask.tracker["in_flight"],
        )
        TrackedTask.tracker["checks"] = TrackedTask.tracker.get("checks", 0) + 1
        try:
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            return await super().complete_aio()
        finally:
            TrackedTask.tracker["in_flight"] -= 1


async def _serial_discover(
    tasks: TaskStructType,
) -> tuple[list[UUID], list[UUID], list[UUID]]:
    """The pre-concurrency walk, verbatim, as the reference implementation.

    Returns ``(post_order, incomplete, previously_completed)`` as id lists.
    Kept in the test rather than in the module so the concurrent
    implementation has something independent to be identical to.
    """
    post_order: list[BaseTask] = []
    incomplete: dict[UUID, BaseTask] = {}
    previously_completed: list[BaseTask] = []
    seen: set[UUID] = set()

    async def walk(task: BaseTask) -> None:
        if task.id in seen:
            return
        seen.add(task.id)
        if await task.complete_aio():
            previously_completed.append(task)
            post_order.append(task)
            return
        for dep in flatten_task_struct(task.requires()):
            await walk(dep)
        incomplete[task.id] = task
        post_order.append(task)

    for task in flatten_task_struct(tasks):
        await walk(task)
    return (
        [t.id for t in post_order],
        list(incomplete),
        [t.id for t in previously_completed],
    )


def _diamond() -> tuple[BaseTask, list[BaseTask]]:
    """A diamond with a shared leaf, a completed branch, and two roots.

    Shape (arrows point at dependencies)::

        root ─┬─ left  ─┬─ shared ── deep
              └─ right ─┘
              └─ done            (already complete: not recursed into)
    """
    deep = TrackedTask(name="dia-deep")
    shared = TrackedTask(name="dia-shared", deps=(deep,))
    left = TrackedTask(name="dia-left", deps=(shared,))
    right = TrackedTask(name="dia-right", deps=(shared,))
    done_dep = TrackedTask(name="dia-done-dep")
    done = TrackedTask(name="dia-done", deps=(done_dep,))
    done.run()  # complete → its subtree must NOT be walked
    root = TrackedTask(name="dia-root", deps=(left, right, done))
    return root, [deep, shared, left, right, done, done_dep, root]


class TestConcurrentDiscovery:
    async def test_matches_the_serial_walk_exactly_for_a_diamond(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """Same DAG, same DiscoveryResult — element for element, in order.

        Concurrency here buys throughput and nothing else: a walk whose
        whole job is to get an ordering right may not have its output
        depend on which completion check answered first.
        """
        root, _ = _diamond()
        (
            expected_post_order,
            expected_incomplete,
            expected_completed,
        ) = await _serial_discover(root)

        registry = FakeReactiveRegistry(root_task_ids=[str(root.id)])
        result = await discover_and_register_aio(registry, uuid4(), root)

        assert list(result.incomplete) == expected_incomplete
        assert [t.id for t in result.previously_completed] == expected_completed
        assert result.retried == []
        # Registration order is the post-order the bulk endpoint relies on.
        registered = [
            UUID(tid) for (method, tid) in registry.calls if method == "register"
        ]
        assert registered == expected_post_order

    async def test_post_order_holds_under_concurrency(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """Every dependency is registered before the task that needs it —
        which is what keeps the bulk endpoint from creating phantom rows
        while resolving ``dependency_task_ids``."""
        root, all_tasks = _diamond()
        registry = FakeReactiveRegistry(root_task_ids=[str(root.id)])

        await discover_and_register_aio(registry, uuid4(), root)

        order = {
            tid: index
            for index, tid in enumerate(
                [tid for (method, tid) in registry.calls if method == "register"]
            )
            if tid is not None
        }
        by_id = {str(task.id): task for task in all_tasks}
        for tid, index in order.items():
            task = by_id[tid]
            if str(task.id) == str(
                next(t.id for t in all_tasks if getattr(t, "name") == "dia-done")
            ):
                continue  # complete → not recursed into, deps not registered
            for dep in flatten_task_struct(task.requires()):
                assert order[str(dep.id)] < index

    async def test_completion_checks_run_concurrently_within_the_bound(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """The bound is pinned, not just "it works": a wide layer's checks
        overlap, and never more than ``max_concurrent_discover`` at once.

        Without the semaphore the peak would be the whole layer; without
        the TaskGroup it would be 1 — the serial wall that made discovery
        50x slower than the resident engine's."""
        width, bound = 120, 6
        leaves = [TrackedTask(name=f"disc-wide-{i}") for i in range(width)]
        root = TrackedTask(name="disc-wide-root", deps=tuple(leaves))
        registry = FakeReactiveRegistry(root_task_ids=[str(root.id)])
        TrackedTask.tracker.clear()

        result = await discover_and_register_aio(
            registry, uuid4(), root, max_concurrent_discover=bound
        )

        assert len(result.incomplete) == width + 1
        assert TrackedTask.tracker["max_in_flight"] <= bound
        assert TrackedTask.tracker["max_in_flight"] == bound

    async def test_shared_dependency_is_checked_and_registered_once(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """The dedupe the serial walk got for free from being serial: two
        concurrent walkers reaching the same dep must not double-register
        it, and must not lose the branch either."""
        root, all_tasks = _diamond()
        registry = FakeReactiveRegistry(root_task_ids=[str(root.id)])
        TrackedTask.tracker.clear()

        result = await discover_and_register_aio(registry, uuid4(), root)

        registered = [tid for (method, tid) in registry.calls if method == "register"]
        assert len(registered) == len(set(registered))
        # deep/shared/left/right/root incomplete; done complete; done's own
        # dep never walked (complete subtrees are not recursed into).
        by_name = {typing.cast(typing.Any, t).name: t for t in all_tasks}
        assert set(result.incomplete) == {
            by_name[name].id
            for name in ("dia-deep", "dia-shared", "dia-left", "dia-right", "dia-root")
        }
        assert [t.id for t in result.previously_completed] == [by_name["dia-done"].id]
        assert str(by_name["dia-done-dep"].id) not in registered
        # One completion check per visited task, no more.
        assert TrackedTask.tracker["checks"] == len(registered)

    async def test_retry_failed_preserves_order_and_membership(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """Retries now run concurrently; ``retried`` still comes back in the
        registry's own reporting order, not in completion order."""
        leaves = [SyncOnlyTask(name=f"disc-retry-{i}") for i in range(20)]
        root = SyncOnlyTask(name="disc-retry-root", deps=tuple(leaves))
        registry = FakeReactiveRegistry(root_task_ids=[str(root.id)])
        for task in [*leaves, root]:
            registry.add_task(str(task.id), status="failed")

        result = await discover_and_register_aio(
            registry, uuid4(), root, retry_failed=True
        )

        registered = [
            UUID(tid) for (method, tid) in registry.calls if method == "register"
        ]
        assert [t.id for t in result.retried] == registered
        assert all(registry.statuses[str(t.id)] == "pending" for t in [*leaves, root])


# =============================================================================
# Bounded concurrent fan-out
# =============================================================================


class InstrumentedTickExecutor(FakeTickExecutor):
    """FakeTickExecutor that records spawn concurrency and interleaving.

    ``submit_detached`` suspends (``asyncio.sleep(0)``) so several spawn
    coroutines can genuinely be in flight at once — without a suspension
    point the fakes complete synchronously and every "concurrent" pass
    would look serial no matter what the scheduler does.
    """

    def __init__(self, *, call_log: list[tuple[str, str | None]], **kwargs) -> None:
        super().__init__(**kwargs)
        self.call_log = call_log
        self.in_flight = 0
        self.max_in_flight = 0

    async def submit_detached(self, task: BaseTask) -> DetachedHandle:
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            # Two suspensions: one to let siblings pile up against the
            # semaphore, one to make sure the peak is observed while they
            # are all still inside this block.
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            self.call_log.append(("spawn", str(task.id)))
            return await super().submit_detached(task)
        finally:
            self.in_flight -= 1


def _wide_layer(prefix: str, width: int) -> tuple[list[BaseTask], BaseTask]:
    """``width`` independent leaves plus a root depending on all of them."""
    leaves = [SyncOnlyTask(name=f"{prefix}-{index}") for index in range(width)]
    root = SyncOnlyTask(name=f"{prefix}-root", deps=tuple(leaves))
    return list(leaves), root


class TestFanOutConcurrency:
    async def test_wide_layer_spawns_concurrently_within_the_bound(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """A wide layer fans out concurrently — and never wider than
        ``max_concurrent_actions``.

        This is the test that pins the bound. Without the semaphore the
        peak would be the whole layer (200), which is exactly the
        unbounded fan-out that would just move the failure from the tick's
        clock to the registry's connection pool; without the TaskGroup it
        would be 1, which is the serial wall this change removes.
        """
        width, bound = 200, 5
        leaves, root = _wide_layer("fanout", width)
        registry, _, store = _setup([*leaves, root], auto_complete=False)
        executor = InstrumentedTickExecutor(call_log=registry.calls)

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=TickConfig(
                linger_seconds=0.0,
                poll_interval_seconds=0.01,
                max_concurrent_actions=bound,
            ),
        )

        assert summary.spawned == width
        assert len(executor.spawned) == width
        assert set(executor.spawned) == {leaf.id for leaf in leaves}
        assert executor.max_in_flight <= bound
        assert executor.max_in_flight == bound  # the bound is saturated
        assert summary.outcome == "lingered_out"

    async def test_ordering_holds_per_task_under_concurrency(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """Concurrency reorders tasks against each other, never the three
        steps *within* one task: the acquiring start precedes the spawn (a
        denied task must never occupy a worker), and the ref-recording
        start follows it (no executor ref for an execution that does not
        exist yet)."""
        width = 40
        leaves, root = _wide_layer("order", width)
        registry, _, store = _setup([*leaves, root], auto_complete=False)
        executor = InstrumentedTickExecutor(call_log=registry.calls)

        await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=TickConfig(
                linger_seconds=0.0,
                poll_interval_seconds=0.01,
                max_concurrent_actions=8,
            ),
        )

        calls = registry.calls
        # Interleaving across tasks is real (otherwise this asserts nothing).
        assert executor.max_in_flight > 1
        for leaf in leaves:
            tid = str(leaf.id)
            claim_at = calls.index(("start_claim", tid))
            spawn_at = calls.index(("spawn", tid))
            # The last start for this task is the post-spawn one carrying
            # the executor ref (the claim records one too, ref-less).
            ref_start_at = len(calls) - 1 - calls[::-1].index(("start", tid))
            assert claim_at < spawn_at < ref_start_at
        # And the ref actually landed, for every task.
        assert all(registry.refs[str(leaf.id)][1] is not None for leaf in leaves)

    async def test_counters_stay_accurate_under_concurrency(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """Three outcomes in one concurrent pass — spawned, self-healed and
        two flavours of recorded failure — all counted exactly once."""
        spawnable = [SyncOnlyTask(name=f"count-spawn-{i}") for i in range(12)]
        healed = [SyncOnlyTask(name=f"count-heal-{i}") for i in range(5)]
        dead = [SyncOnlyTask(name=f"count-dead-{i}") for i in range(4)]
        lost = [SyncOnlyTask(name=f"count-lost-{i}") for i in range(3)]
        root = SyncOnlyTask(
            name="count-root", deps=tuple([*spawnable, *healed, *dead, *lost])
        )
        registry, _, store = _setup(
            [*spawnable, *healed, *dead, *lost, root], auto_complete=False
        )
        executor = InstrumentedTickExecutor(call_log=registry.calls)
        for index, task in enumerate(healed):
            task.run()  # target exists → self-heal on probe
            registry.add_task(
                str(task.id),
                status="running",
                executor="fake",
                executor_ref=f"heal-{index}",
            )
        for index, task in enumerate(dead):
            registry.add_task(
                str(task.id),
                status="running",
                executor="fake",
                executor_ref=f"dead-{index}",
                attempt_count=2,  # at budget: probed-dead stays failed
            )
            executor.probe_statuses[f"dead-{index}"] = DetachedExecutionStatus.FAILED
        for task in lost:
            store._tasks.pop(str(task.id), None)  # no pickle, no registry data

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=TickConfig(
                linger_seconds=0.0,
                poll_interval_seconds=0.01,
                max_concurrent_actions=4,
                fail_mode=FailMode.CONTINUE,
            ),
        )

        assert summary.spawned == len(spawnable)
        assert summary.self_healed == len(healed)
        assert summary.failed_recorded == len(dead) + len(lost)
        assert sorted(executor.spawned) == sorted(task.id for task in spawnable)

    async def test_denied_task_never_reaches_a_worker(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """A one-slot limit against a concurrent fan-out: exactly one task
        acquires and spawns, and no denied task is ever submitted."""
        width = 10
        leaves, root = _wide_layer("denied", width)
        registry, _, store = _setup([*leaves, root], auto_complete=False)
        executor = InstrumentedTickExecutor(call_log=registry.calls)
        registry.limits["one-slot"] = 1

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=TickConfig(
                linger_seconds=0.05,
                poll_interval_seconds=0.01,
                max_concurrent_actions=width,
                limit_key_selector=lambda t: ["one-slot"],
            ),
        )

        assert summary.spawned == 1
        # Cumulative across the tick's passes (the denied nine are re-tried
        # on every fresh frontier), so at least one full round of denials.
        assert summary.limit_denied >= width - 1
        assert summary.limit_denied % (width - 1) == 0
        assert len(executor.spawned) == 1
        # The denied ones were claimed-and-refused, never spawned.
        spawned_ids = set(executor.spawned)
        denied = [leaf for leaf in leaves if leaf.id not in spawned_ids]
        assert len(denied) == width - 1
        for leaf in denied:
            assert ("spawn", str(leaf.id)) not in registry.calls
        assert registry.build_status == "running"


class TestSpawnCap:
    async def test_cap_truncates_and_the_tick_re_acts_immediately(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """``linger_seconds=0`` is the probe: the linger loop returns on its
        first check, so the only way the remaining tasks get spawned in this
        same tick is the ``acted`` path re-evaluating on a fresh frontier.
        A cap that "just truncated" would leave 20 of the 30 unspawned."""
        width, cap = 30, 10
        leaves, root = _wide_layer("cap", width)
        registry, executor, store = _setup([*leaves, root], auto_complete=False)

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=TickConfig(
                linger_seconds=0.0,
                poll_interval_seconds=30.0,  # never reached: no lingering
                max_spawns_per_tick=cap,
            ),
        )

        assert summary.spawned == width
        assert len(executor.spawned) == width
        # Three acting passes of `cap` each, plus the pass that found
        # nothing left to do and let the tick linger out.
        assert summary.iterations == width // cap + 1
        assert summary.outcome == "lingered_out"

    async def test_uncapped_layer_is_one_pass(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """Control for the test above: the same layer under the default cap
        goes out in a single acting pass."""
        width = 30
        leaves, root = _wide_layer("uncapped", width)
        registry, executor, store = _setup([*leaves, root], auto_complete=False)

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=TickConfig(linger_seconds=0.0, poll_interval_seconds=30.0),
        )

        assert summary.spawned == width
        assert summary.iterations == 2

    async def test_ticks_timeout_bounds_a_real_pass(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """End to end: the tick's own timeout reaches the fan-out and
        truncates it, rather than only being readable in _spawn_cap."""
        width = 200
        leaves, root = _wide_layer("tick-timeout", width)
        registry, executor, store = _setup([*leaves, root], auto_complete=False)
        # A tiny container: min cap (50) per pass, so 200 leaves take four.
        config = TickConfig(
            linger_seconds=0.0,
            poll_interval_seconds=30.0,
            max_concurrent_actions=10,
            tick_timeout_seconds=1.0,
            # A backend that would have justified a far larger batch.
            report_tick_summaries=False,
        )
        assert (
            reactive_module._spawn_cap([], FakeTickExecutor(), config).limit
            == reactive_module._MIN_SPAWN_CAP
        )

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=config,
        )

        assert summary.spawned == width
        assert summary.iterations == width // reactive_module._MIN_SPAWN_CAP + 1

    async def test_the_cap_and_its_source_are_logged_once_per_tick(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
        caplog: pytest.LogCaptureFixture,
    ):
        """Three of the four rungs produce plausible-looking numbers from
        very different inputs, so a truncating tick is only diagnosable if
        the log says which one was read."""
        leaves, root = _wide_layer("cap-log", 3)
        registry, executor, store = _setup([*leaves, root], auto_complete=False)

        with caplog.at_level(logging.INFO, logger="stardag.build._reactive"):
            await run_tick_aio(
                uuid4(),
                registry=registry,
                task_executor=executor,
                task_store=store,
                config=TickConfig(
                    linger_seconds=0.0,
                    poll_interval_seconds=30.0,
                    tick_timeout_seconds=900.0,
                ),
            )

        announcements = [
            record.message
            for record in caplog.records
            if "will spawn at most" in record.message
        ]
        assert len(announcements) == 1  # once per tick, not once per pass
        assert "tick container's own timeout (900s)" in announcements[0]

    def test_cap_prefers_the_ticks_own_timeout_over_the_workers(self):
        """The rung that matters: a five-minute tick spawning hour-long
        workers must size its fan-out to the five minutes.

        The two inputs differ by two orders of magnitude here, and the
        worker-derived cap is the dangerous one — a tick that commits to a
        container's worth of work it cannot live long enough to finish is
        exactly the failure the cap exists to prevent. Asserting the cap
        *tracks the tick's* number (and not merely "is smaller") is what
        makes a regression to the proxy fail loudly."""
        tasks = [SyncOnlyTask(name="tick-vs-worker")]
        # A 24-hour worker under a 5-minute tick.
        executor = FakeTickExecutor(timeout_seconds=86_400.0)
        config = TickConfig(max_concurrent_actions=10, tick_timeout_seconds=300.0)

        cap = reactive_module._spawn_cap(tasks, executor, config)

        assert cap.limit == reactive_module._derived_spawn_cap(300.0, config)
        assert "tick container's own timeout" in cap.source
        # And it is emphatically not the worker-derived answer, which the
        # ceiling alone would not have saved us from.
        worker_derived = reactive_module._spawn_cap(
            tasks, executor, TickConfig(max_concurrent_actions=10)
        )
        assert worker_derived.limit == reactive_module._MAX_SPAWN_CAP
        assert cap.limit < worker_derived.limit

    def test_cap_is_derived_from_the_ticks_timeout(self):
        """No explicit cap → the cap is a duration budget: a fraction of the
        container's own wall clock, spread over the in-flight bound."""
        tasks = [SyncOnlyTask(name="derive")]
        config = TickConfig(max_concurrent_actions=10, tick_timeout_seconds=600.0)

        cap = reactive_module._spawn_cap(tasks, FakeTickExecutor(), config)

        assert cap.limit == int(
            reactive_module._SPAWN_BUDGET_FRACTION
            * 600.0
            * 10
            / reactive_module._SECONDS_PER_SPAWN
        )

    def test_executor_timeout_is_the_proxy_when_the_tick_has_none(self):
        """Rung 3: no tick timeout is known, so the executor's is read —
        and the source says so, because it is a proxy for a different
        quantity."""
        tasks = [SyncOnlyTask(name="proxy")]
        config = TickConfig(max_concurrent_actions=10)

        cap = reactive_module._spawn_cap(
            tasks, FakeTickExecutor(timeout_seconds=600.0), config
        )

        assert cap.limit == reactive_module._derived_spawn_cap(600.0, config)
        assert "as a proxy" in cap.source

    def test_cap_uses_the_tightest_timeout_across_candidates(self):
        """Heterogeneous routing: the smallest backend limit bounds the
        pass, so the proxy rung is derived from it."""

        class PerTaskTimeoutExecutor(FakeTickExecutor):
            def execution_timeout_seconds(self, task: BaseTask) -> float | None:
                return {"tight": 400.0}.get(typing.cast(typing.Any, task).name, 4000.0)

        tasks = [SyncOnlyTask(name="tight"), SyncOnlyTask(name="loose")]
        config = TickConfig(max_concurrent_actions=10)

        cap = reactive_module._spawn_cap(tasks, PerTaskTimeoutExecutor(), config)

        assert (
            cap.limit
            == reactive_module._spawn_cap(
                [SyncOnlyTask(name="tight")], PerTaskTimeoutExecutor(), config
            ).limit
        )
        assert (
            cap.limit
            < reactive_module._spawn_cap(
                [SyncOnlyTask(name="loose")], PerTaskTimeoutExecutor(), config
            ).limit
        )

    def test_cap_falls_back_when_no_timeout_is_known_anywhere(self):
        """Bottom rung: neither the tick nor the executor enforces a
        wall-clock limit — but the cap is still a cap, never "everything"."""
        tasks = [SyncOnlyTask(name="no-timeout")]

        cap = reactive_module._spawn_cap(
            tasks, FakeTickExecutor(timeout_seconds=None), TickConfig()
        )

        assert cap.limit == reactive_module._DEFAULT_MAX_SPAWNS_PER_TICK
        assert "no wall-clock limit is known" in cap.source

    def test_derived_cap_is_clamped(self):
        """Floor and ceiling, so neither a 30-second container nor a 30-day
        one produces a nonsense batch size."""
        tasks = [SyncOnlyTask(name="clamp")]

        assert (
            reactive_module._spawn_cap(
                tasks,
                FakeTickExecutor(),
                TickConfig(max_concurrent_actions=1, tick_timeout_seconds=1.0),
            ).limit
            == reactive_module._MIN_SPAWN_CAP
        )
        assert (
            reactive_module._spawn_cap(
                tasks,
                FakeTickExecutor(),
                TickConfig(max_concurrent_actions=50, tick_timeout_seconds=2_592_000.0),
            ).limit
            == reactive_module._MAX_SPAWN_CAP
        )

    def test_explicit_cap_wins(self):
        """Top rung: the override beats every derivation below it."""
        cap = reactive_module._spawn_cap(
            [SyncOnlyTask(name="explicit")],
            FakeTickExecutor(timeout_seconds=600.0),
            TickConfig(max_spawns_per_tick=7, tick_timeout_seconds=600.0),
        )

        assert cap.limit == 7
        assert "set explicitly" in cap.source


class SpawnFailingExecutor(FakeTickExecutor):
    """Executor whose detached submit always raises.

    The failure this PR exists for: the backend refused the spawn, so no
    container ever ran and no function-level retry policy can apply.
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.spawn_attempts = 0

    async def submit_detached(self, task: BaseTask) -> DetachedHandle:
        self.spawn_attempts += 1
        raise RuntimeError("backend refused the spawn")


class TestAttemptBudget:
    """``TickConfig.max_attempts``: a per-build, per-task budget on starts.

    It exists because a backend's function-level retries only cover
    exceptions *inside* the container. Everything a tick observes — a spawn
    that never produced a container, an execution the backend killed, a
    claim that lapsed under a vanished worker — is outside that, and used
    to end a FAIL_FAST build on the first occurrence.
    """

    async def test_spawn_failure_under_budget_is_retried_then_exhausts(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """The headline case, both halves: the first spawn failure is
        retried, the second exhausts the 2-attempt budget and fails the
        build — bounded, not a loop."""
        (root,) = _chain("budget-spawn-fail")
        executor = SpawnFailingExecutor()
        registry, _, store = _setup([root], auto_complete=False, executor=executor)

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=FAST_TICK,  # max_attempts=2, FAIL_FAST
        )

        assert executor.spawn_attempts == 2
        assert summary.retried == 1
        assert summary.retry_exhausted == 1
        assert summary.failed_recorded == 2
        assert summary.terminal_status == "failed"
        assert registry.build_status == "failed"
        # Each spawn's two starts (claim + ref-recording) collapse into one
        # attempt, so two attempts is what the budget counted.
        assert registry.attempt_count(str(root.id)) == 2

    async def test_probed_dead_execution_under_budget_is_respawned(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """An execution the backend reports FAILED is retried while the
        budget allows — and FAIL_FAST does not kill the build over the
        failure recorded on the way there."""
        (root,) = _chain("budget-probe-retry")
        executor = FakeTickExecutor(statuses={"fc-oom": DetachedExecutionStatus.FAILED})
        registry, _, store = _setup([root], auto_complete=False, executor=executor)
        registry.add_task(
            str(root.id),
            status="running",
            executor="fake",
            executor_ref="fc-oom",
            attempt_count=1,
        )

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=FAST_TICK,
        )

        assert summary.retried == 1
        assert summary.failed_recorded == 1
        assert summary.spawned == 1
        assert executor.spawned == [root.id]
        # FAIL_FAST reads the pre-action frontier snapshot, so the failure
        # recorded and retried inside one pass never counts as a
        # build-killing failure.
        assert summary.terminal_status is None
        assert registry.build_status == "running"
        assert registry.statuses[str(root.id)] == "running"

    async def test_at_budget_the_tick_declines_and_says_why(
        self, caplog, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """Budget spent: no respawn, and a message naming the task, the
        count, the budget and the escape that actually works."""
        (root,) = _chain("budget-probe-exhausted")
        executor = FakeTickExecutor(
            statuses={"fc-dead": DetachedExecutionStatus.FAILED}
        )
        registry, _, store = _setup([root], auto_complete=False, executor=executor)
        registry.add_task(
            str(root.id),
            status="running",
            executor="fake",
            executor_ref="fc-dead",
            attempt_count=2,
        )

        with caplog.at_level("ERROR"):
            summary = await run_tick_aio(
                uuid4(),
                registry=registry,
                task_executor=executor,
                task_store=store,
                config=FAST_TICK,
            )

        assert summary.retried == 0
        assert summary.retry_exhausted == 1
        assert executor.spawned == []
        assert summary.terminal_status == "failed"
        assert ("retry", str(root.id)) not in registry.calls
        messages = "\n".join(record.getMessage() for record in caplog.records)
        assert str(root.id) in messages
        assert "will NOT be retried" in messages
        assert "2 of 2 allowed attempt(s) spent" in messages
        # The escape is the re-trigger, and the message must not leave the
        # reader thinking a bare retry would have done.
        assert "RE-TRIGGER THIS BUILD" in messages
        assert "starts a new round and resets every task's attempt count" in messages
        assert "Retrying the task on its own does NOT reset the count" in messages
        assert 'tick_kwargs={"max_attempts": 4}' in messages

    async def test_bare_retry_at_budget_is_refused_and_names_the_re_trigger(
        self, caplog, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """The trap: a *bare* retry of a task already at budget.

        The retry succeeds server-side and the task returns to PENDING, but
        it records no BUILD_RESUMED, so the round the count is measured
        against is unchanged and the scheduler would never start it. The
        distinction from a re-trigger (which does reset) is the whole
        reason this message exists.
        """
        (root,) = _chain("budget-operator-retry")
        registry, executor, store = _setup([root], auto_complete=False)
        # Exactly what `stardag tasks retry` / the UI's Retry leaves behind:
        # pending again, with the attempts already spent.
        registry.add_task(str(root.id), status="pending", attempt_count=2)

        with caplog.at_level("ERROR"):
            summary = await run_tick_aio(
                uuid4(),
                registry=registry,
                task_executor=executor,
                task_store=store,
                config=FAST_TICK,
            )

        assert summary.budget_denied == 1
        assert summary.retried == 0
        assert executor.spawned == []
        assert ("start_claim", str(root.id)) not in registry.calls
        assert summary.terminal_status == "failed"
        messages = "\n".join(record.getMessage() for record in caplog.records)
        assert str(root.id) in messages
        assert "a BARE RETRY put it back" in messages
        assert "That retry SUCCEEDED" in messages
        assert "a bare retry does not start a new build round" in messages
        assert "What you wanted is a RE-TRIGGER of this build" in messages
        # The operator reads the task, not only the tick's logs.
        reason = registry.fail_reasons[str(root.id)][0]
        assert reason is not None
        assert "Attempt budget spent (2 of 2 allowed attempt(s)" in reason
        assert "A bare retry does not reset it" in reason
        assert "re-trigger this build" in reason

    async def test_a_re_trigger_starts_a_new_round_and_resets_the_budget(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """The escape both exhaustion messages point at, end to end.

        A re-trigger of an existing build id records BUILD_RESUMED *before*
        its discovery retries the failed tasks, so the round boundary lands
        ahead of them and they arrive at zero — the same task the tick
        refused a moment ago is now an ordinary spawn candidate.
        """
        build_id = uuid4()
        (root,) = _chain("budget-round-reset")
        registry, executor, store = _setup([root], auto_complete=False)
        registry.add_task(str(root.id), status="pending", attempt_count=2)

        refused = await run_tick_aio(
            build_id,
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=FAST_TICK,
        )

        assert refused.budget_denied == 1
        assert executor.spawned == []
        assert registry.build_status == "failed"

        # Exactly what ``_trigger_reactive`` does, in exactly that order:
        # resume the build (the round boundary), then let discovery reset
        # the failed task to pending.
        await registry.build_resume_aio(build_id)
        await registry.task_retry_aio(build_id, root)
        assert registry.attempt_count(str(root.id)) == 0

        resumed = await run_tick_aio(
            build_id,
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=FAST_TICK,
        )

        assert resumed.budget_denied == 0
        assert resumed.spawned == 1
        assert executor.spawned == [root.id]
        # One attempt into the new round (the spawn's claim + ref starts
        # collapse), with the previous round's two no longer counted.
        assert registry.attempt_count(str(root.id)) == 1

    async def test_a_zero_attempt_count_never_denies_a_start(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """0 is "not attempted in this build", never "out of budget" — even
        with retries switched off entirely."""
        (root,) = _chain("budget-zero-count")
        registry, executor, store = _setup([root], auto_complete=False)
        registry.add_task(str(root.id), status="pending", attempt_count=0)

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=TickConfig(
                linger_seconds=0.0, poll_interval_seconds=0.01, max_attempts=1
            ),
        )

        assert summary.budget_denied == 0
        assert summary.spawned == 1
        assert executor.spawned == [root.id]

    async def test_suspended_resumption_is_never_budget_gated(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """Resuming a dynamic-dependency yield records a fresh start, so a
        suspend-heavy task is "over budget" while perfectly healthy. Gating
        it would cap dynamic dependencies, not retries."""
        (root,) = _chain("budget-suspended")
        registry, executor, store = _setup([root], auto_complete=False)
        registry.add_task(str(root.id), status="suspended", attempt_count=5)

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=TickConfig(
                linger_seconds=0.0, poll_interval_seconds=0.01, max_attempts=2
            ),
        )

        assert summary.budget_denied == 0
        assert summary.spawned == 1
        assert executor.spawned == [root.id]

    async def test_unrehydratable_task_never_spends_the_budget(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """A task whose object cannot be resolved fails deterministically:
        the second reading finds the same absence. Retrying it would burn
        the budget to arrive at the same failure, later."""
        (root,) = _chain("budget-no-object")
        registry, executor, store = _setup([root], auto_complete=False)
        store._tasks.pop(str(root.id), None)  # no pickle, no registry data

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=FAST_TICK,
        )

        assert summary.failed_recorded == 1
        assert summary.retried == 0
        assert summary.retry_exhausted == 0
        assert ("retry", str(root.id)) not in registry.calls
        assert registry.attempt_count(str(root.id)) == 0
        assert summary.terminal_status == "failed"

    async def test_max_attempts_one_records_the_failure_and_never_respawns(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """The pre-``max_attempts`` behaviour, still available verbatim."""
        (root,) = _chain("budget-disabled")
        executor = SpawnFailingExecutor()
        registry, _, store = _setup([root], auto_complete=False, executor=executor)

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=TickConfig(
                linger_seconds=0.0, poll_interval_seconds=0.01, max_attempts=1
            ),
        )

        assert executor.spawn_attempts == 1
        assert summary.retried == 0
        # Not "exhausted": nothing was budgeted away, retries are off.
        assert summary.retry_exhausted == 0
        assert summary.terminal_status == "failed"

    async def test_a_registry_that_cannot_count_attempts_never_retries(
        self, caplog, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """A server predating ``attempt_count`` reports nothing, so no
        budget can bound a retry loop. Degrade to the old behaviour rather
        than to an unbounded one — and say so, because a configured retry
        policy silently doing nothing is its own trap."""
        (root,) = _chain("budget-old-server")
        executor = SpawnFailingExecutor()
        registry, _, store = _setup([root], auto_complete=False, executor=executor)
        registry.serves_attempt_counts = False

        with caplog.at_level("WARNING"):
            summary = await run_tick_aio(
                uuid4(),
                registry=registry,
                task_executor=executor,
                task_store=store,
                config=FAST_TICK,
            )

        assert executor.spawn_attempts == 1
        assert summary.retried == 0
        assert summary.retry_exhausted == 0
        assert summary.budget_denied == 0
        assert summary.terminal_status == "failed"
        messages = "\n".join(record.getMessage() for record in caplog.records)
        assert "does not report per-round attempt counts" in messages
        assert "Upgrade stardag-api" in messages

    async def test_summary_counters_are_reported_to_the_registry(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """The budget counters ride the persisted TickSummary, so "why did
        this build fail on a transient error?" is answerable without logs."""
        (root,) = _chain("budget-summary")
        executor = SpawnFailingExecutor()
        registry, _, store = _setup([root], auto_complete=False, executor=executor)

        await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=FAST_TICK,
        )

        reported = registry.reported_tick_summaries[-1]
        assert reported["retried"] == 1
        assert reported["retry_exhausted"] == 1
        assert reported["budget_denied"] == 0


class TestBlockerAgeFormatting:
    """The blocker message is what a stalled build's owner acts on."""

    def test_age_is_human_readable_not_raw_seconds(self):
        from stardag.build._reactive import _format_age

        # Found in a live run: "RUNNING for 10889s" made the reader do
        # arithmetic before they could judge whether it was alarming.
        assert _format_age(10889) == "3h 1m"
        assert _format_age(5) == "5s"
        assert _format_age(95) == "1m"
        assert _format_age(3600) == "1h"
        assert _format_age(90000) == "1d 1h"

    def test_discovery_is_bounded_lower_than_frontier_actions(self):
        """Discovery is limited by the target backend, not the registry.

        Measured live: a 64-task layer against a Modal volume completed at 16
        in flight, stalled at 32 and failed at 50. Sharing one constant with
        the registry-bound actions conflated two different ceilings.
        """
        from stardag.build._reactive import (
            _DEFAULT_MAX_CONCURRENCY,
            _DEFAULT_MAX_CONCURRENT_DISCOVER,
        )

        assert _DEFAULT_MAX_CONCURRENT_DISCOVER < _DEFAULT_MAX_CONCURRENCY


class TestInterruptedTasks:
    """What a tick does with a task the platform interrupted.

    An interruption is the execution backend taking a container away — a
    function timeout, a reclaimed instance — reported by the dying worker
    in its grace window. It is not a failure, so it must not fail a
    FAIL_FAST build; it is not running, so it holds no claim; and it is
    still the scheduler's to act on, which is what these tests pin.

    There is no policy to configure: the status is written only for a
    task that raised ``ResumableInterruption``, so reaching this code means
    the task asked. An interruption a task did not catch never gets here —
    the worker reports nothing, the execution dies, and a later pass
    records an ordinary retryable failure.
    """

    async def test_an_interrupted_task_is_resumed(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """No configuration involved: the status exists only because a
        worker asked to be resumed, so the tick resumes it."""
        (root,) = _chain("interrupted-default")
        registry, executor, store = _setup([root], auto_complete=True)
        registry.statuses[str(root.id)] = "interrupted"

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=FAST_TICK,
        )

        assert summary.interruptions_restarted == 1
        assert summary.interruptions_failed == 0
        assert summary.failed_recorded == 0
        assert ("fail", str(root.id)) not in registry.calls
        assert executor.spawned == [root.id]
        assert summary.terminal_status == "completed"

    async def test_a_resumption_request_respawns_without_a_failure(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """An INTERRUPTED task is one that asked to be resumed, so it goes
        straight back to the frontier with no failure in its history."""
        (root,) = _chain("interrupted-restart")
        registry, executor, store = _setup([root], auto_complete=True)
        registry.statuses[str(root.id)] = "interrupted"

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=TickConfig(
                linger_seconds=0.3,
                poll_interval_seconds=0.01,
            ),
        )

        assert summary.interruptions_restarted == 1
        assert summary.interruptions_failed == 0
        assert summary.failed_recorded == 0
        assert ("fail", str(root.id)) not in registry.calls
        assert executor.spawned == [root.id]
        assert summary.terminal_status == "completed"

    async def test_resumption_is_bounded_by_its_own_budget(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """Exempt from the attempt budget does not mean unbounded: a task
        that times out forever must stop, with a message naming the knob."""
        (root,) = _chain("interrupted-exhausted")
        registry, executor, store = _setup([root], auto_complete=True)
        registry.add_task(str(root.id), status="interrupted", interrupt_count=3)

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=TickConfig(
                linger_seconds=0.3,
                poll_interval_seconds=0.01,
                max_interruptions=3,
            ),
        )

        assert summary.interruptions_exhausted == 1
        assert summary.interruptions_restarted == 0
        assert executor.spawned == []
        reason = registry.fail_reasons[str(root.id)][-1] or ""
        assert "Interruption budget spent (3 of 3" in reason
        assert "max_interruptions" in reason

    async def test_interruptions_do_not_spend_the_attempt_budget(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """The property the whole separate budget exists for. Two
        interruptions with ``max_attempts=2`` — a task charged for them
        would already be refused a start."""
        (root,) = _chain("interrupted-not-an-attempt")
        registry, executor, store = _setup([root], auto_complete=True)
        registry.add_task(str(root.id), status="interrupted", interrupt_count=2)
        assert registry.attempt_count(str(root.id)) == 1

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=TickConfig(
                linger_seconds=0.3,
                poll_interval_seconds=0.01,
                max_attempts=2,
                max_interruptions=10,
            ),
        )

        assert summary.interruptions_restarted == 1
        assert summary.budget_denied == 0
        assert executor.spawned == [root.id]

    async def test_a_live_ref_is_re_probed_until_it_is_not(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """An interrupted task whose execution still probes as live is left
        alone — the backend may be retrying the input under the same ref,
        and spawning would run it twice.

        **But "left alone" must not mean "abandoned".** Nothing will ever
        emit an event when that ref stops being live: the worker that would
        have reported is dead, and an interrupted task produces nothing
        further. A tick that lingered on the wake-up flag here would stall
        the build until the watchdog — which is off by default. So the pass
        re-probes instead, and picks the task up as soon as the ref
        resolves.

        The executor below answers RUNNING once and FAILED after, which is
        exactly the shape of the race this guards: the interruption is
        reported inside the grace window and wakes a tick immediately, so
        the probe can easily land before the call has finished unwinding.
        """
        (root,) = _chain("interrupted-backend-retry")

        class SettlingExecutor(FakeTickExecutor):
            probes = 0

            async def detached_status(self, task, executor, ref):
                SettlingExecutor.probes += 1
                if SettlingExecutor.probes == 1:
                    return DetachedExecutionStatus.RUNNING
                return DetachedExecutionStatus.FAILED

        registry, executor, store = _setup(
            [root], auto_complete=True, executor=SettlingExecutor()
        )
        registry.add_task(
            str(root.id),
            status="interrupted",
            executor="fake",
            executor_ref="fc-live",
        )

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=FAST_TICK,
        )

        # It waited on the first probe...
        assert summary.interruptions_backend_retrying >= 1
        assert SettlingExecutor.probes >= 2, "the tick stopped re-probing"
        # ...and resumed the task once the ref resolved, rather than
        # lingering out with the build stalled.
        assert summary.interruptions_restarted == 1
        assert executor.spawned == [root.id]
        assert summary.terminal_status == "completed"

    async def test_a_live_ref_is_not_spawned_in_the_same_pass(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """The other half: while the ref stays live, nothing is spawned.
        A permanently-live ref lingers out rather than duplicating the
        execution."""
        (root,) = _chain("interrupted-still-live")
        registry, executor, store = _setup(
            [root],
            auto_complete=False,
            executor=FakeTickExecutor(
                statuses={"fc-live": DetachedExecutionStatus.RUNNING}
            ),
        )
        registry.add_task(
            str(root.id),
            status="interrupted",
            executor="fake",
            executor_ref="fc-live",
        )

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=TickConfig(linger_seconds=0.1, poll_interval_seconds=0.02),
        )

        assert summary.interruptions_backend_retrying >= 1
        assert summary.interruptions_restarted == 0
        assert executor.spawned == []
        assert registry.statuses[str(root.id)] == "interrupted"

    async def test_a_dead_ref_does_not_block_the_restart(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """The control for the guard above: a ref that probes FAILED is a
        finished execution, so the task is the scheduler's to start."""
        (root,) = _chain("interrupted-dead-ref")
        registry, executor, store = _setup(
            [root],
            auto_complete=True,
            executor=FakeTickExecutor(
                statuses={"fc-dead": DetachedExecutionStatus.FAILED}
            ),
        )
        registry.add_task(
            str(root.id),
            status="interrupted",
            executor="fake",
            executor_ref="fc-dead",
        )

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=TickConfig(
                linger_seconds=0.3,
                poll_interval_seconds=0.01,
            ),
        )

        assert summary.interruptions_backend_retrying == 0
        assert summary.interruptions_restarted == 1
        assert executor.spawned == [root.id]

    async def test_unknown_probe_does_not_stall_the_task(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """UNKNOWN is deliberately NOT treated as "hands off", unlike the
        RUNNING-task path. An interrupted task holds no claim and is
        nobody else's to run, and an executor that does not recognise the
        recorded ref's backend answers UNKNOWN forever — so waiting on it
        would wedge the build rather than protect it."""
        (root,) = _chain("interrupted-unknown-ref")
        # FakeTickExecutor answers UNKNOWN for any ref it was not told about.
        registry, executor, store = _setup([root], auto_complete=True)
        registry.add_task(
            str(root.id),
            status="interrupted",
            executor="some-other-backend",
            executor_ref="ref-we-cannot-probe",
        )

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=TickConfig(
                linger_seconds=0.3,
                poll_interval_seconds=0.01,
            ),
        )

        assert summary.interruptions_backend_retrying == 0
        assert executor.spawned == [root.id]

    async def test_no_interrupt_counter_falls_back_to_the_attempt_budget(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """A server predating ``interrupt_count`` cannot bound a resume
        loop, so resumption degrades to the bounded thing rather than to an
        unbounded one."""
        (root,) = _chain("interrupted-no-counter")
        registry, executor, store = _setup([root], auto_complete=True)
        registry.statuses[str(root.id)] = "interrupted"
        registry.serves_interrupt_counts = False

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=TickConfig(
                linger_seconds=0.3,
                poll_interval_seconds=0.01,
            ),
        )

        assert summary.interruptions_restarted == 0
        assert summary.interruptions_failed == 1

    async def test_an_interruption_does_not_fail_a_fail_fast_build(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """The headline property, stated directly. Under FAIL_FAST a single
        FAILED task kills the build on the next pass — which is exactly
        what must not happen when the platform, not the task, ended the
        run. The dep is interrupted and the build still completes."""
        dep, root = _chain("ff-dep", "ff-root")
        registry, executor, store = _setup([dep, root], auto_complete=True)
        registry.statuses[str(dep.id)] = "interrupted"

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=TickConfig(
                linger_seconds=0.3,
                poll_interval_seconds=0.01,
                fail_mode=FailMode.FAIL_FAST,
            ),
        )

        assert summary.terminal_status == "completed"
        assert registry.build_status == "completed"

    async def test_fail_fast_cancels_an_interrupted_task(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """An interrupted task may still have a live execution — that is
        the premise of the backend-retry guard — so a dying build must
        cancel it, and must not leave it INTERRUPTED.

        Left behind it is a permanent wedge for every other build gated on
        it: ``_OWNER_DRIVEN_STATUSES`` reads interrupted as "the owner will
        move it", so a neighbour waits and then fails, where a CANCELLED
        task is reset and run.
        """
        # Siblings, not a chain: a failed *upstream* would gate the
        # interrupted task out of `actionable` and the test would pass for
        # the wrong reason.
        broken = SyncOnlyTask(name="ff-cancel-broken", deps=())
        resuming = SyncOnlyTask(name="ff-cancel-resuming", deps=())
        root = SyncOnlyTask(name="ff-cancel-root", deps=(broken, resuming))
        registry, executor, store = _setup(
            [broken, resuming, root],
            auto_complete=False,
            executor=FakeTickExecutor(
                statuses={"fc-live": DetachedExecutionStatus.RUNNING}
            ),
        )
        registry.statuses[str(broken.id)] = "failed"
        registry.add_task(
            str(resuming.id),
            status="interrupted",
            executor="fake",
            executor_ref="fc-live",
        )

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=TickConfig(
                linger_seconds=0.05,
                poll_interval_seconds=0.02,
                fail_mode=FailMode.FAIL_FAST,
            ),
        )

        assert summary.terminal_status == "failed"
        assert "fc-live" in executor.cancelled_refs
        assert registry.statuses[str(resuming.id)] == "cancelled"

    async def test_fail_fast_does_not_cancel_what_this_pass_just_resumed(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """Terminal handling runs on the snapshot taken BEFORE the pass
        acted, so an interrupted task the pass resumed still reads
        ``interrupted`` there, under its old ref.

        Acting on that stale copy is worse than doing nothing: cancelling
        the dead ref is a no-op, while the TASK_CANCELLED it records
        releases the claim on the execution that just started — and a
        cancelled task with no claim is exactly what a neighbouring build
        treats as recoverable, so it resets it and spawns a second
        execution while the first is still writing the target. Hence the
        re-read in ``_cancel_running``.
        """
        broken = SyncOnlyTask(name="ff-fresh-broken", deps=())
        resuming = SyncOnlyTask(name="ff-fresh-resuming", deps=())
        root = SyncOnlyTask(name="ff-fresh-root", deps=(broken, resuming))
        registry, executor, store = _setup(
            [broken, resuming, root],
            auto_complete=False,
            # The interrupted task's OLD ref is dead, so the pass resumes it.
            executor=FakeTickExecutor(
                statuses={"fc-dead": DetachedExecutionStatus.FAILED}
            ),
        )
        registry.statuses[str(broken.id)] = "failed"
        registry.add_task(
            str(resuming.id),
            status="interrupted",
            executor="fake",
            executor_ref="fc-dead",
        )

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=TickConfig(
                linger_seconds=0.05,
                poll_interval_seconds=0.02,
                fail_mode=FailMode.FAIL_FAST,
            ),
        )

        assert summary.terminal_status == "failed"
        # It was resumed, so it is RUNNING under a NEW ref by the time the
        # build dies — and it is that ref which must be cancelled, never
        # the stale one the pre-action snapshot carried.
        assert executor.spawned == [resuming.id]
        assert "fc-dead" not in executor.cancelled_refs, (
            "cancelled the stale ref: the live execution is orphaned and "
            "its claim released"
        )
        assert executor.cancelled_refs == ["ref-1"]
        assert registry.statuses[str(resuming.id)] == "cancelled"

    async def test_interrupted_is_reset_by_a_re_trigger(self):
        """``_RETRYABLE_STATUSES`` covers it, so discovery's
        ``retry_failed`` picks up a task abandoned mid-interruption. Leave
        it out and that task is unschedulable forever."""
        assert "interrupted" in _RETRYABLE_STATUSES


class TestLingerPollCost:
    """A lingering tick with nothing to do must not read the frontier.

    The poll asks one question — "has anything changed?" — every few
    seconds, per lingering build. It used to ask it by fetching the whole
    frontier: seven statements, one of them a window-function aggregate over
    the event log, of which it read a single boolean.
    """

    async def test_linger_poll_reads_the_flag_not_the_frontier(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        (root,) = _chain("poll-cost-root")
        registry, executor, store = _setup([root], auto_complete=False)
        registry.add_task(
            str(root.id),
            status="running",
            executor="other-backend",
            executor_ref="job-1",
        )

        frontier_reads: list[int] = []
        notify_reads: list[int] = []
        real_frontier = registry.build_get_frontier_aio
        real_notify = registry.build_get_notify_aio

        async def counting_frontier(bid):
            frontier_reads.append(1)
            return await real_frontier(bid)

        async def counting_notify(bid):
            notify_reads.append(1)
            return await real_notify(bid)

        registry.build_get_frontier_aio = counting_frontier  # type: ignore[method-assign]
        registry.build_get_notify_aio = counting_notify  # type: ignore[method-assign]

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=dataclasses.replace(
                FAST_TICK, linger_seconds=0.2, poll_interval_seconds=0.02
            ),
        )

        assert summary.outcome == "lingered_out"
        # Several polls happened — otherwise this proves nothing.
        assert len(notify_reads) >= 3, notify_reads
        # ...and the frontier was read once, by the pass itself. Not once
        # per poll. The exact number is the point of the test, so it is
        # asserted rather than bounded.
        assert len(frontier_reads) == 1, (
            f"the linger poll read the frontier {len(frontier_reads) - 1} "
            "extra time(s); it must ask the one-row flag endpoint"
        )


class TestSchedulerLease:
    """The single-flight lease, now a column on the build row.

    It used to ride on the deprecated global concurrency lock, which meant
    every reader assembled a lock name from a build id and queried a second
    table to answer "does this build have a scheduler?".
    """

    async def test_a_held_lease_makes_the_tick_a_no_op(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        (root,) = _chain("lease-held-root")
        registry, executor, store = _setup([root], lease_acquired=False)

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=FAST_TICK,
        )

        assert summary.outcome == "lease_held"
        assert executor.spawned == [], "a refused tick must not schedule"

    async def test_losing_the_lease_stops_the_tick(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
        monkeypatch,
    ):
        """A renewal answering "not yours" means the lease already lapsed
        and a successor took it over. Carrying on would be exactly the
        double-scheduling the lease exists to prevent, so the tick stops
        instead — the successor holds the flag and will act on it.
        """
        (root,) = _chain("lease-lost-root")
        registry, executor, store = _setup([root], auto_complete=False)
        registry.add_task(
            str(root.id),
            status="running",
            executor="other-backend",
            executor_ref="job-1",
        )
        # A successor took it over, so both the renewal and the re-acquire
        # behind it are refused. The renewal interval is 20 s in production,
        # far longer than this test lingers.
        monkeypatch.setattr(reactive_module, "_LEASE_RENEW_INTERVAL_SECONDS", 0.01)
        registry.lease_stolen = True
        # Land a wake-up in the release window, so a tick that still held
        # the build *would* legitimately hand off. Without this the flag is
        # already cleared by exit time and the assertion below would hold
        # for the wrong reason — it would be about having nothing to hand
        # off rather than about having no standing to.
        registry.reactive_app_name = "my-app"
        registry.lease_on_release = lambda: setattr(registry, "needs_tick", True)
        spawned: list[UUID] = []

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=dataclasses.replace(
                FAST_TICK,
                linger_seconds=5.0,
                poll_interval_seconds=0.01,
                spawn_tick=lambda bid, app: spawned.append(bid),
            ),
        )

        assert summary.outcome == "lease_lost"
        # And it must not hand off. A successor already holds the lease, so
        # spawning one would only add a container that finds it held and
        # exits — the tick that lost the build has no standing to schedule
        # for it.
        assert summary.successor_spawned == 0
        assert spawned == []
        # And the exit release was refused rather than honoured: clearing
        # the successor's lease on the way out is exactly what the owner
        # check exists to prevent.
        assert registry.lease_owner == "__successor__", (
            "the lost tick's exit release cleared the successor's lease"
        )

    async def test_a_lapsed_lease_is_re_taken_rather_than_abandoning_the_build(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
        monkeypatch,
    ):
        """A refused renewal usually means our own lease expired between
        renewals — not that anything took it over, since nothing is normally
        competing for the build. Stopping there would abandon a build nobody
        else is driving, which is worse than the double-scheduling the check
        guards against. So the tick re-acquires and carries on, and only a
        re-acquire that *also* fails is a real loss.
        """
        (root,) = _chain("lease-lapse-root")
        registry, executor, store = _setup([root], auto_complete=False)
        registry.add_task(
            str(root.id),
            status="running",
            executor="other-backend",
            executor_ref="job-1",
        )
        monkeypatch.setattr(reactive_module, "_LEASE_RENEW_INTERVAL_SECONDS", 0.01)
        registry.lease_lapsed = True

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=dataclasses.replace(
                FAST_TICK, linger_seconds=0.15, poll_interval_seconds=0.01
            ),
        )

        assert summary.outcome == "lingered_out", (
            "a lease that merely lapsed must be re-taken, not treated as lost"
        )

    async def test_the_lease_is_released_on_the_way_out(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """Not left to expire: the next tick for this build should not have
        to wait out a TTL for a scheduler that already finished."""
        (root,) = _chain("lease-release-root")
        registry, executor, store = _setup([root])
        released: list[bool] = []
        registry.lease_on_release = lambda: released.append(True)

        await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=FAST_TICK,
        )

        assert released == [True]
        assert registry.lease_owner is None


class TestExitHandshake:
    """The two re-reads that make a *conditional* wake-up safe.

    A worker may skip spawning a tick when the registry reports a
    scheduler live (``BuildNotifyResult.scheduler_live``). That is only
    sound if a live scheduler cannot exit past a flag set before it
    released the lease — which the plain "poll until the deadline, then
    unwind" shape does not guarantee. See ``_run_tick_body_aio``.
    """

    def _spawner(self) -> tuple[list[UUID], typing.Callable[[UUID, str], None]]:
        spawned: list[UUID] = []

        def spawn(build_id: UUID, app_name: str) -> None:
            spawned.append(build_id)

        return spawned, spawn

    async def test_flag_set_during_release_spawns_a_successor(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """The window this whole handshake exists for: the flag lands after
        the tick's last look but before the lease is gone, and the worker
        that set it saw the lease held and did not spawn. Nobody would
        schedule."""
        (root,) = _chain("handoff-root")
        executor = FakeTickExecutor(
            statuses={"fc-live": DetachedExecutionStatus.RUNNING}
        )
        registry, executor, store = _setup(
            [root], auto_complete=False, executor=executor
        )
        registry.add_task(
            str(root.id), status="running", executor="fake", executor_ref="fc-live"
        )
        # The worker's notify lands exactly as the lease is released.
        registry.lease_on_release = lambda: setattr(registry, "needs_tick", True)

        spawned, spawn = self._spawner()
        build_id = uuid4()
        summary = await run_tick_aio(
            build_id,
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=dataclasses.replace(FAST_TICK, spawn_tick=spawn),
        )

        assert summary.outcome == "lingered_out"
        assert summary.successor_spawned == 1
        assert spawned == [build_id]
        # The flag is still set, so the successor has something to clear.
        assert registry.needs_tick is True

    async def test_flag_set_before_release_extends_the_linger(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """The fast half: the flag arrives between the last poll and the
        deadline, so the tick still holds the lease and simply keeps it —
        no successor container, no cold start."""
        (root,) = _chain("extend-root")
        registry, executor, store = _setup([root], auto_complete=False)
        registry.add_task(
            str(root.id),
            status="running",
            executor="other-backend",
            executor_ref="job-1",
        )

        # Land the wake-up in the gap deterministically. With one poll per
        # linger (poll interval > linger), the *flag* reads are: 1 the
        # single linger poll, 2 the pre-release re-check. Setting the flag
        # as read 1 answers "no" puts it exactly between the last poll and
        # the deadline — the gap the re-check exists for.
        #
        # These are notify reads, not frontier reads: the linger poll asks
        # the one-row endpoint now, so hooking the frontier would no longer
        # land the flag in this window at all.
        reads: list[int] = []
        real_notify = registry.build_get_notify_aio

        async def notify_then_set(bid):
            result = await real_notify(bid)
            reads.append(1)
            if len(reads) == 1:
                # Complete the target too, so the extended pass reaches a
                # terminal state instead of lingering out a second time.
                root.run()
                registry.needs_tick = True
            return result

        registry.build_get_notify_aio = notify_then_set  # type: ignore[method-assign]

        spawned, spawn = self._spawner()
        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=dataclasses.replace(
                FAST_TICK,
                linger_seconds=0.02,
                poll_interval_seconds=0.05,
                spawn_tick=spawn,
            ),
        )

        # It re-acted under its own lease and finished the build there.
        assert summary.outcome == "terminal"
        assert summary.terminal_status == "completed"
        assert summary.linger_extended >= 1
        assert spawned == [], "kept the lease, so no successor was needed"

    async def test_quiet_exit_spawns_nothing(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """The overwhelmingly common exit: nothing was notified, so neither
        half of the handshake fires."""
        (root,) = _chain("quiet-root")
        registry, executor, store = _setup([root], auto_complete=False)
        registry.add_task(
            str(root.id),
            status="running",
            executor="other-backend",
            executor_ref="job-1",
        )

        spawned, spawn = self._spawner()
        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=dataclasses.replace(FAST_TICK, spawn_tick=spawn),
        )

        assert summary.outcome == "lingered_out"
        assert summary.linger_extended == 0
        assert summary.successor_spawned == 0
        assert spawned == []

    async def test_terminal_exit_does_not_hand_off(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """A finished build has nothing a successor could act on, so the
        post-release read is skipped even with the flag set."""
        (root,) = _chain("terminal-handoff-root")
        registry, executor, store = _setup([root], auto_complete=True)
        registry.lease_on_release = lambda: setattr(registry, "needs_tick", True)

        spawned, spawn = self._spawner()
        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=dataclasses.replace(FAST_TICK, spawn_tick=spawn),
        )

        assert summary.outcome == "terminal"
        assert summary.terminal_status == "completed"
        assert registry.needs_tick is True  # a late worker report
        assert spawned == [], "a finished build needs no successor tick"

    async def test_lease_held_tick_does_not_hand_off(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """A tick that never held the lease has no window to close — the
        holder does. Handing off from here would spawn a tick per wake-up
        again, which is the cost this removes."""
        (root,) = _chain("held-handoff-root")
        registry, executor, store = _setup(
            [root], auto_complete=False, lease_acquired=False
        )
        registry.needs_tick = True

        spawned, spawn = self._spawner()
        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=dataclasses.replace(FAST_TICK, spawn_tick=spawn),
        )

        assert summary.outcome == "lease_held"
        assert spawned == []

    async def test_crashing_tick_hands_off_a_wakeup_that_lands_as_it_dies(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """A wake-up landing while a *crashing* tick unwinds is in the same
        release window as one landing while a healthy tick exits, and the
        worker that set it skipped its spawn either way. The hand-off is in
        a ``finally`` for exactly that — so an exception cannot bypass the
        release-window check.

        Note what is being asserted: the tick completes a pass (clearing
        the flag) and dies on the *second*, with a new notify arriving as it
        goes. It is that new wake-up which is handed off — not the one the
        first pass had already cleared and taken responsibility for. See
        ``test_a_crash_before_the_first_clear_hands_nothing_on``.
        """
        (root,) = _chain("crash-handoff-root")
        registry, executor, store = _setup([root], auto_complete=False)
        registry.add_task(
            str(root.id),
            status="running",
            executor="other-backend",
            executor_ref="job-1",
        )

        boom = RuntimeError("tick died mid-pass")
        real_clear = registry.build_clear_notify_aio
        clears: list[int] = []

        async def clear_then_die_on_the_second_pass(bid):
            clears.append(1)
            if len(clears) >= 2:
                # A worker reports as this tick dies.
                registry.needs_tick = True
                raise boom
            await real_clear(bid)
            # Provokes that second pass: the linger poll sees the flag.
            registry.needs_tick = True

        registry.build_clear_notify_aio = (  # type: ignore[method-assign]
            clear_then_die_on_the_second_pass
        )

        spawned, spawn = self._spawner()
        build_id = uuid4()
        with pytest.raises(RuntimeError, match="tick died mid-pass"):
            await run_tick_aio(
                build_id,
                registry=registry,
                task_executor=executor,
                task_store=store,
                config=dataclasses.replace(FAST_TICK, spawn_tick=spawn),
            )

        assert spawned == [build_id]

    async def test_a_crash_before_the_first_clear_hands_nothing_on(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """The cascade guard, and the reason it exists.

        Clearing the flag is the first thing every pass does. If that call
        is what keeps failing — a rate-limited or 5xx ``DELETE /notify``
        while the frontier ``GET`` stays healthy — then a tick that handed
        off on the way out would be replaced by a successor that fails
        identically, with the flag still set because the clear never
        happened: one cold container after another, forever.

        A tick that never cleared anything has taken responsibility for
        nothing, so it hands nothing on.
        """
        (root,) = _chain("cascade-guard-root")
        registry, executor, store = _setup([root], auto_complete=False)
        registry.needs_tick = True  # and it stays set: the clear never lands

        async def clear_always_fails(bid):
            raise ConnectionError("registry refused the clear")

        registry.build_clear_notify_aio = clear_always_fails  # type: ignore[method-assign]

        spawned, spawn = self._spawner()
        with pytest.raises(ConnectionError, match="refused the clear"):
            await run_tick_aio(
                uuid4(),
                registry=registry,
                task_executor=executor,
                task_store=store,
                config=dataclasses.replace(FAST_TICK, spawn_tick=spawn),
            )

        assert spawned == [], (
            "handed off after failing to clear the flag: the successor would "
            "fail the same way, and the chain would never end"
        )

    async def test_a_tick_that_clears_then_crashes_is_not_crash_recovery(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """The honest limit of the handshake, asserted so nobody documents
        it as more than it is.

        A tick that clears the flag and then dies leaves the flag false, so
        the hand-off has nothing to see and the wake-up it had taken
        responsibility for waits for the next completion or the watchdog.
        Unchanged from before the handshake existed — it closes the release
        window, it does not resurrect a crashed tick's own wake-up.
        """
        (root,) = _chain("crash-after-clear-root")
        registry, executor, store = _setup([root], auto_complete=False)
        registry.needs_tick = True
        real_clear = registry.build_clear_notify_aio

        async def clear_then_die(bid):
            await real_clear(bid)  # the flag is now false
            raise RuntimeError("died with the wake-up already claimed")

        registry.build_clear_notify_aio = clear_then_die  # type: ignore[method-assign]

        spawned, spawn = self._spawner()
        with pytest.raises(RuntimeError, match="already claimed"):
            await run_tick_aio(
                uuid4(),
                registry=registry,
                task_executor=executor,
                task_store=store,
                config=dataclasses.replace(FAST_TICK, spawn_tick=spawn),
            )

        assert registry.needs_tick is False
        assert spawned == []

    async def test_holding_the_lease_without_a_spawner_warns_once(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget], caplog
    ):
        """Nothing connects the worker's skip to the holder's ability to
        hand off: the worker decides on what the *registry* says, while the
        hand-off belongs to whoever holds the lease. Driving ``run_tick_aio``
        by hand with a default config is where the pairing comes apart, so
        it says so — once per process, since it is a property of how the
        process was configured."""
        (root,) = _chain("no-spawner-warning-root")
        registry, executor, store = _setup([root], auto_complete=True)

        reactive_module._warned_missing_successor_spawner = False
        with caplog.at_level(logging.WARNING, logger=reactive_module.__name__):
            await run_tick_aio(
                uuid4(),
                registry=registry,
                task_executor=executor,
                task_store=store,
                config=FAST_TICK,
            )
            first = caplog.text.count("cannot hand off on the way out")

            await run_tick_aio(
                uuid4(),
                registry=registry,
                task_executor=executor,
                task_store=store,
                config=FAST_TICK,
            )
            second = caplog.text.count("cannot hand off on the way out")

        assert first == 1
        assert second == 1, "warned more than once per process"

    async def test_a_configured_spawner_warns_about_nothing(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget], caplog
    ):
        (root,) = _chain("spawner-no-warning-root")
        registry, executor, store = _setup([root], auto_complete=True)
        _, spawn = self._spawner()

        reactive_module._warned_missing_successor_spawner = False
        with caplog.at_level(logging.WARNING, logger=reactive_module.__name__):
            await run_tick_aio(
                uuid4(),
                registry=registry,
                task_executor=executor,
                task_store=store,
                config=dataclasses.replace(FAST_TICK, spawn_tick=spawn),
            )

        assert "cannot hand off on the way out" not in caplog.text

    async def test_hand_off_failure_does_not_mask_the_tick(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """Best-effort means best-effort: the hand-off runs while an
        exception is unwinding, so anything it raises would replace the
        error the caller is about to see."""
        (root,) = _chain("handoff-boom-root")
        registry, executor, store = _setup([root], auto_complete=False)

        real_clear = registry.build_clear_notify_aio

        async def clear_then_die(bid):
            await real_clear(bid)
            registry.needs_tick = True
            raise RuntimeError("the real failure")

        registry.build_clear_notify_aio = clear_then_die  # type: ignore[method-assign]

        def explode(_: UUID, __: str) -> None:
            raise ConnectionError("modal is down")

        with pytest.raises(RuntimeError, match="the real failure"):
            await run_tick_aio(
                uuid4(),
                registry=registry,
                task_executor=executor,
                task_store=store,
                config=dataclasses.replace(FAST_TICK, spawn_tick=explode),
            )

    async def test_without_a_spawner_nothing_is_read_or_spawned(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """``spawn_tick=None`` (the default, and every caller
        whose wake-ups spawn unconditionally) must not pay a frontier fetch
        to discover it has nowhere to hand off to."""
        (root,) = _chain("no-spawner-root")
        registry, executor, store = _setup([root], auto_complete=False)
        registry.add_task(
            str(root.id),
            status="running",
            executor="other-backend",
            executor_ref="job-1",
        )
        trace: list[str] = []
        real_frontier = registry.build_get_frontier_aio

        async def traced_frontier(bid):
            trace.append("frontier")
            return await real_frontier(bid)

        registry.build_get_frontier_aio = traced_frontier  # type: ignore[method-assign]

        def on_release() -> None:
            trace.append("release")
            registry.needs_tick = True

        registry.lease_on_release = on_release

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=FAST_TICK,
        )

        assert summary.outcome == "lingered_out"
        assert summary.successor_spawned == 0
        assert trace[-1] == "release", (
            "read the frontier after releasing the lease with no successor "
            f"spawner configured: {trace}"
        )

    async def test_zero_linger_skips_the_pre_release_recheck(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """``linger_seconds=0`` is the watchdog sweep: one pass per build,
        many builds in one container. Extending a zero-length linger re-arms
        an already-expired deadline, so a steadily-notified build would spin
        without ever sleeping. The hand-off covers it instead."""
        (root,) = _chain("sweep-root")
        registry, executor, store = _setup([root], auto_complete=False)
        registry.add_task(
            str(root.id),
            status="running",
            executor="other-backend",
            executor_ref="job-1",
        )
        registry.needs_tick = True  # set again the instant the pass clears it
        real_clear = registry.build_clear_notify_aio

        async def clear_then_notify(bid):
            await real_clear(bid)
            registry.needs_tick = True

        registry.build_clear_notify_aio = clear_then_notify  # type: ignore[method-assign]

        spawned, spawn = self._spawner()
        build_id = uuid4()
        summary = await run_tick_aio(
            build_id,
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=TickConfig(
                linger_seconds=0,
                poll_interval_seconds=0.01,
                spawn_tick=spawn,
            ),
        )

        assert summary.outcome == "lingered_out"
        assert summary.iterations == 1, "the sweep's one-pass promise"
        assert summary.linger_extended == 0
        assert spawned == [build_id]
