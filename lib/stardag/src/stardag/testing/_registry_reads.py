"""The inspecting reads of :class:`~stardag.testing.InMemoryRegistry` —
build lists with paging, a plan with its counts, a build's plans, task
lists, a task's executions and events, a deployment — with the server's
orderings and shapes (``services/reads.py``, ``services/plan_reads.py``,
``services/executions.py``). Cursors are opaque offsets here; only their
round trip is part of the contract.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from typing import Any, TypeVar
from uuid import UUID

from stardag.registry import (
    BuildInfo,
    BuildListPage,
    DeploymentInfo,
    EventInfo,
    ExecutionInfo,
    PlanDetail,
    TaskInfo,
    TaskListPage,
)
from stardag.testing._registry_state import (
    BuildRow,
    DeploymentRow,
    ExecutionRow,
    PlanRow,
    RegistryState,
    TaskRow,
    refuse,
)

T = TypeVar("T")

MAX_LIST_LIMIT = 500


def _page(
    rows: Sequence[T], limit: int, cursor: str | None
) -> tuple[list[T], str | None]:
    limit = max(1, min(limit, MAX_LIST_LIMIT))
    start = int(cursor) if cursor else 0
    page = list(rows[start : start + limit])
    more = start + limit < len(rows)
    return page, (str(start + limit) if more else None)


class ReadsMixin(RegistryState):
    """See the module docstring."""

    # Implemented by InMemoryRegistry (deployment rows need its "current"
    # rule).
    def _deployment_info(
        self, row: DeploymentRow, *, created: bool = False
    ) -> DeploymentInfo:
        raise NotImplementedError

    # -- builds -----------------------------------------------------------------------

    def _info(self, build: BuildRow) -> BuildInfo:
        return BuildInfo(
            id=build.id,
            name=build.name,
            description=build.description,
            status=build.status,
            root_task_ids=list(build.root_task_ids),
            created_at=build.created_at,
            last_active_at=build.last_active_at,
            is_resumed=build.is_resumed,
            executor_metadata=build.executor_metadata,
            reactive_app_name=build.reactive_app_name,
            reactive_tick_kwargs=build.reactive_tick_kwargs,
            error_message=build.error_message if build.status == "failed" else None,
        )

    def build_list_page(
        self,
        *,
        status: str | None = None,
        reactive_app_name: str | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> BuildListPage:
        self._record(
            "build_list_page",
            status=status,
            reactive_app_name=reactive_app_name,
            limit=limit,
            cursor=cursor,
        )
        # Most recently active first, then by id — as the server orders
        # (``Build.last_active_at.desc(), Build.id.desc()``).
        rows = sorted(
            (
                b
                for b in self.builds.values()
                if (status is None or b.status == status)
                and (
                    reactive_app_name is None
                    or b.reactive_app_name == reactive_app_name
                )
            ),
            key=lambda b: (b.last_active_at, b.id),
            reverse=True,
        )
        page, next_cursor = _page(rows, limit, cursor)
        return BuildListPage(
            builds=[self._info(b) for b in page],
            total=len(rows),
            next_cursor=next_cursor,
        )

    def build_list(
        self,
        *,
        status: str | None = None,
        reactive_app_name: str | None = None,
        limit: int = 100,
    ) -> list[BuildInfo]:
        self._record("build_list", status=status, reactive_app_name=reactive_app_name)
        return self.build_list_page(
            status=status, reactive_app_name=reactive_app_name, limit=limit
        ).builds

    def build_list_running(
        self, *, reactive_app_name: str | None = None, limit: int = 100
    ) -> list[UUID]:
        # Delegates to build_list (most-recently-active first), as the real
        # client does.
        builds = self.build_list(
            status="running", reactive_app_name=reactive_app_name, limit=limit
        )
        return [b.id for b in builds]

    # -- plans --------------------------------------------------------------------------

    def _plan_detail(self, plan: PlanRow) -> PlanDetail:
        members = self.members.get(plan.id, {}).values()
        by_status: Counter[str] = Counter()
        excluded = 0
        for m in members:
            if m.excluded_reason is not None:
                excluded += 1
            else:
                by_status[self.tasks[m.task_id].status] += 1
        deployment = self.deployments.get(plan.deployment_id)
        return PlanDetail(
            id=plan.id,
            build_id=plan.build_id,
            deployment_id=plan.deployment_id,
            deployment=self._deployment_info(deployment) if deployment else None,
            settings_hash=plan.settings_hash,
            generation=plan.generation,
            activated_at=plan.activated_at,
            sealed_at=plan.sealed_at,
            superseded_at=plan.superseded_at,
            is_active=plan.active,
            member_count=len(members),
            root_count=sum(1 for m in members if m.is_root),
            excluded_count=excluded,
            member_counts=dict(by_status),
        )

    def plan_get(self, plan_id: UUID) -> PlanDetail:
        self._record("plan_get", plan_id=plan_id)
        return self._plan_detail(self.plan(plan_id))

    def build_list_plans(self, build_id: UUID) -> list[PlanDetail]:
        self._record("build_list_plans", build_id=build_id)
        self.build(build_id)
        plans = sorted(
            (p for p in self.plans.values() if p.build_id == build_id),
            key=lambda p: p.generation,
            reverse=True,
        )
        return [self._plan_detail(p) for p in plans]

    # -- tasks and executions ----------------------------------------------------------

    def _execution_info(self, execution: ExecutionRow) -> ExecutionInfo:
        build_id = self.plans[execution.plan_id].build_id
        active = self.active_plan(build_id)
        return ExecutionInfo(
            id=execution.id,
            task_id=execution.task_id,
            build_id=build_id,
            plan_id=execution.plan_id,
            instance_id=execution.instance_id,
            executor=execution.executor,
            executor_ref=execution.executor_ref,
            executor_metadata=execution.executor_metadata,
            in_current_plan=active is not None and active.id == execution.plan_id,
            started_at=execution.started_at,
            claim_released_at=execution.claim_released_at,
            claim_outcome=execution.claim_outcome,
            ended_at=execution.ended_at,
            outcome=execution.outcome,
        )

    def _claim_holder(self, task: TaskRow) -> tuple[UUID | None, UUID | None]:
        """The claim's plan and build while RUNNING (live or lapsed)."""
        if task.status != "running" or task.claim_plan_id is None:
            return None, None
        return task.claim_plan_id, self.plans[task.claim_plan_id].build_id

    def _task_summary(self, task: TaskRow, **extra: Any) -> TaskInfo:
        current = self.executions.get(task.execution_id) if task.execution_id else None
        claim_plan_id, claim_build_id = self._claim_holder(task)
        return TaskInfo(
            task_id=task.task_id,
            task_namespace=task.task_namespace,
            task_name=task.task_name,
            version=task.version,
            output_uri=task.output_uri,
            status=task.status,
            status_at=task.status_at,
            started_at=current.started_at if current else None,
            completed_at=task.completed_at,
            error_message=task.error_message,
            claim_expires_at=task.claim_expires_at,
            claim_plan_id=claim_plan_id,
            claim_build_id=claim_build_id,
            execution_id=task.execution_id,
            **extra,
        )

    def task_list(
        self,
        *,
        status: str | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> TaskListPage:
        self._record("task_list", status=status, limit=limit, cursor=cursor)
        rows = sorted(
            (
                t
                for t in self.tasks.values()
                if t.status_at is not None and (status is None or t.status == status)
            ),
            key=lambda t: (t.status_at, t.task_id),
            reverse=True,
        )
        page, next_cursor = _page(rows, limit, cursor)
        return TaskListPage(
            tasks=[self._task_summary(t) for t in page], next_cursor=next_cursor
        )

    def task_list_executions(
        self, task_id: str, *, include_ended: bool = True, limit: int = 100
    ) -> list[ExecutionInfo]:
        self._record(
            "task_list_executions",
            task_id=task_id,
            include_ended=include_ended,
            limit=limit,
        )
        self.task(task_id)
        rows = [
            e
            for e in self.executions.values()
            if e.task_id == task_id and (include_ended or e.ended_at is None)
        ]
        # Newest first; insertion order breaks a tie on the clock.
        order = {eid: i for i, eid in enumerate(self.executions)}
        rows.sort(key=lambda e: (e.started_at, order[e.id]), reverse=True)
        return [self._execution_info(e) for e in rows[:limit]]

    def task_events(self, task_id: str, *, limit: int = 500) -> list[EventInfo]:
        self._record("task_events", task_id=task_id, limit=limit)
        self.task(task_id)
        return [
            EventInfo(
                event_type=e.type,
                build_id=e.build_id,
                plan_id=e.plan_id,
                execution_id=e.execution_id,
                task_id=e.task_id,
                report_applied=e.applied,
                event_metadata=dict(e.detail) or None,
            )
            for e in self.events
            if e.task_id == task_id
        ][: max(1, min(limit, MAX_LIST_LIMIT))]

    # -- deployments --------------------------------------------------------------------

    def deployment_get(self, deployment_id: UUID) -> DeploymentInfo:
        self._record("deployment_get", deployment_id=deployment_id)
        row = self.deployments.get(deployment_id)
        if row is None:
            raise refuse("unknown_deployment", status=404)
        return self._deployment_info(row)
