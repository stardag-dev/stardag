"""Reactive scheduling in :class:`~stardag.testing.InMemoryRegistry`: the
reactive marker, wake-up flags (``notify``, ``wake-candidates``), the
scheduler lease, ``stalled-builds`` and tick summaries (design.md,
"Wake-ups, limits, locks"). Every read acts on RUNNING builds only, as the
server's do."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from typing import Any
from uuid import UUID

from stardag.registry import (
    BuildInfo,
    BuildNotifyResult,
    SchedulerLeaseResult,
    StalledBuild,
    TickSummaryRecord,
    WakeCandidate,
)
from stardag.testing._registry_reads import ReadsMixin
from stardag.testing._registry_state import (
    WAKE_HANDOUT_WINDOW,
    BuildRow,
    refuse_plan_only,
)


class ReactiveMixin(ReadsMixin):
    def build_set_reactive_meta(
        self,
        build_id: UUID,
        *,
        app_name: str,
        tick_kwargs: dict[str, Any] | None = None,
    ) -> BuildInfo:
        self._record(
            "build_set_reactive_meta",
            build_id=build_id,
            app_name=app_name,
            tick_kwargs=tick_kwargs,
        )
        build = self.build(build_id)
        refuse_plan_only(build, "be reactively scheduled")
        build.reactive_app_name = app_name
        if tick_kwargs is not None:
            build.reactive_tick_kwargs = dict(tick_kwargs)
        return self._info(build)

    def _lease_live(self, build: BuildRow) -> bool:
        return (
            build.lease_expires_at is not None and build.lease_expires_at > self.now()
        )

    def build_notify(
        self, build_id: UUID, *, can_spawn: bool = True
    ) -> BuildNotifyResult:
        self._record("build_notify", build_id=build_id, can_spawn=can_spawn)
        build = self.build(build_id)
        if build.status != "running":
            return BuildNotifyResult(
                build_id=build_id, needs_tick=False, scheduler_live=False
            )
        build.needs_tick = True
        build.needs_tick_at = build.needs_tick_at or self.now()
        if can_spawn:
            build.handed_out_at = self.now()
        return BuildNotifyResult(
            build_id=build_id, needs_tick=True, scheduler_live=self._lease_live(build)
        )

    def build_get_notify(self, build_id: UUID) -> BuildNotifyResult:
        build = self.build(build_id)
        return BuildNotifyResult(build_id=build_id, needs_tick=build.needs_tick)

    def build_clear_notify(self, build_id: UUID) -> None:
        self._record("build_clear_notify", build_id=build_id)
        build = self.build(build_id)
        build.needs_tick = False
        build.needs_tick_at = None

    def build_wake_candidates(self, limit: int = 20) -> list[WakeCandidate]:
        now = self.now()
        chosen: list[WakeCandidate] = []
        for build in self.builds.values():
            if len(chosen) >= limit:
                break
            if not build.needs_tick or build.status != "running":
                continue
            if build.reactive_app_name is None or self._lease_live(build):
                continue
            if (
                build.handed_out_at is not None
                and now - build.handed_out_at < WAKE_HANDOUT_WINDOW
                and not (
                    build.lease_released_at is not None
                    and build.handed_out_at < build.lease_released_at
                )
            ):
                continue
            build.handed_out_at = now
            chosen.append(
                WakeCandidate(
                    build_id=build.id, reactive_app_name=build.reactive_app_name
                )
            )
        return chosen

    def scheduler_lease_acquire(
        self, build_id: UUID, *, owner_id: str, ttl_seconds: int
    ) -> SchedulerLeaseResult:
        build = self.build(build_id)
        if self._lease_live(build) and build.lease_owner != owner_id:
            return SchedulerLeaseResult(held=False, expires_at=build.lease_expires_at)
        build.lease_owner = owner_id
        build.lease_expires_at = self.now() + timedelta(seconds=ttl_seconds)
        return SchedulerLeaseResult(held=True, expires_at=build.lease_expires_at)

    def scheduler_lease_renew(
        self, build_id: UUID, *, owner_id: str, ttl_seconds: int
    ) -> SchedulerLeaseResult:
        build = self.build(build_id)
        if build.lease_owner != owner_id or not self._lease_live(build):
            return SchedulerLeaseResult(held=False)
        build.lease_expires_at = self.now() + timedelta(seconds=ttl_seconds)
        return SchedulerLeaseResult(held=True, expires_at=build.lease_expires_at)

    def scheduler_lease_release(
        self, build_id: UUID, *, owner_id: str
    ) -> SchedulerLeaseResult:
        build = self.build(build_id)
        if build.lease_owner != owner_id:
            return SchedulerLeaseResult(held=False)
        if self._lease_live(build):  # a lapsed holder spends no hand-out
            build.lease_released_at = self.now()
        build.lease_owner = None
        build.lease_expires_at = None
        return SchedulerLeaseResult(held=True)

    def build_list_stalled(
        self, *, older_than_seconds: int = 300, limit: int = 200
    ) -> list[StalledBuild]:
        cutoff = self.now() - timedelta(seconds=older_than_seconds)
        stalled: list[StalledBuild] = []
        for build in self.builds.values():
            if build.status != "running" or build.reactive_app_name is None:
                continue
            if self._lease_live(build):
                continue
            flagged_at = build.needs_tick_at if build.needs_tick else None
            if flagged_at is not None and flagged_at <= cutoff:
                reason, since = "flagged_unserved", flagged_at
            elif (
                build.lease_owner is not None
                and build.lease_expires_at is not None
                and build.lease_expires_at <= cutoff
            ):
                reason, since = "lease_lapsed", build.lease_expires_at
            else:
                continue
            stalled.append(
                StalledBuild(
                    build_id=build.id,
                    reactive_app_name=build.reactive_app_name,
                    reason=reason,
                    since=since,
                )
            )
        stalled.sort(key=lambda s: (s.since, s.build_id))
        return stalled[:limit]

    def build_report_tick_summary(
        self, build_id: UUID, summary: Mapping[str, Any]
    ) -> None:
        self.tick_summaries.setdefault(build_id, []).append(dict(summary))

    def build_list_tick_summaries(
        self, build_id: UUID, *, limit: int = 20
    ) -> list[TickSummaryRecord]:
        return [
            TickSummaryRecord(outcome=s.get("outcome", "?"), summary=s)
            for s in reversed(self.tick_summaries.get(build_id, []))
        ][:limit]
