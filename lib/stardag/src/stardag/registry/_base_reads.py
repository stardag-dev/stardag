"""The read seams of :class:`~stardag.registry.RegistryABC` that only the
CLI (and other inspecting callers) use: build lists, a plan with its
counts, a build's plans, paged task lists, a task's executions and events,
tick summaries, a deployment. Split from ``_base.py`` by the module-size rule;
:class:`RegistryABC` inherits them.

Sync only: no async caller reads them (the watchdog's
``build_list_running`` goes through ``build_list``, synchronously). As
everywhere on the seam, a double that does not implement one raises
:class:`NotImplementedError` naming it.
"""

from __future__ import annotations

from uuid import UUID

from stardag.registry._models import (
    BuildInfo,
    BuildListPage,
    DeploymentInfo,
    EventInfo,
    ExecutionInfo,
    PlanDetail,
    TaskListPage,
    TickSummaryRecord,
)


def _missing(registry: object, method: str) -> NotImplementedError:
    return NotImplementedError(f"{type(registry).__name__} does not implement {method}")


class RegistryReadsABC:
    """See the module docstring."""

    def plan_get(self, plan_id: UUID) -> PlanDetail:
        """``GET /plans/{id}``: lifecycle, scope with the deployment
        resolved, member counts by status (excluded counted apart). Served
        for a superseded plan too."""
        raise _missing(self, "plan_get")

    def build_list_plans(self, build_id: UUID) -> list[PlanDetail]:
        """``GET /builds/{id}/plans``: every plan of the build, newest
        generation first."""
        raise _missing(self, "build_list_plans")

    def build_list(
        self,
        *,
        status: str | None = None,
        reactive_app_name: str | None = None,
        limit: int = 100,
    ) -> list[BuildInfo]:
        """``GET /builds``: the first page of builds, most recently active
        first, optionally by status and by reactive app."""
        raise _missing(self, "build_list")

    def build_list_page(
        self,
        *,
        status: str | None = None,
        reactive_app_name: str | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> BuildListPage:
        """``GET /builds`` with its paging: one page, most recently active
        first, the total over every page and the cursor of the next."""
        raise _missing(self, "build_list_page")

    def task_list(
        self,
        *,
        status: str | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> TaskListPage:
        """``GET /tasks``: completions, most recent status change first,
        optionally of one status; without instances."""
        raise _missing(self, "task_list")

    def task_list_executions(
        self, task_id: str, *, include_ended: bool = True, limit: int = 100
    ) -> list[ExecutionInfo]:
        """``GET /tasks/{id}/executions``: the task's executions across
        builds, newest first; ``include_ended=False`` keeps the ones with no
        end reported."""
        raise _missing(self, "task_list_executions")

    def task_events(self, task_id: str, *, limit: int = 500) -> list[EventInfo]:
        """``GET /tasks/{id}/events``: the task's event log, oldest first,
        at most ``limit`` (the server caps it at 500)."""
        raise _missing(self, "task_events")

    def build_list_tick_summaries(
        self, build_id: UUID, *, limit: int = 20
    ) -> list[TickSummaryRecord]:
        """``GET /builds/{id}/tick-summaries``: the reported tick summaries,
        newest first."""
        raise _missing(self, "build_list_tick_summaries")

    def deployment_get(self, deployment_id: UUID) -> DeploymentInfo:
        """``GET /deployments/{id}``."""
        raise _missing(self, "deployment_get")
