"""The inspecting reads of :class:`~stardag.registry.APIRegistry` over HTTP
(the seams of :class:`~stardag.registry._base_reads.RegistryReadsABC`, plus
``task_get`` and ``task_list_artifacts``). Split from ``_api_registry.py``
by the module-size rule; each route is one :class:`Request`, as there.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from stardag.registry._api_http import HTTPTransport, Request
from stardag.registry._api_routes import _task_artifacts_req
from stardag.registry._base_reads import RegistryReadsABC
from stardag.registry._models import (
    BuildInfo,
    BuildListPage,
    DeploymentInfo,
    EventInfo,
    ExecutionInfo,
    PlanDetail,
    TaskArtifactInfo,
    TaskInfo,
    TaskListPage,
    TickSummaryRecord,
)


def _page_params(
    limit: int, cursor: str | None, **filters: str | None
) -> dict[str, str]:
    params = {"limit": str(limit)}
    if cursor is not None:
        params["cursor"] = cursor
    params.update({k: v for k, v in filters.items() if v is not None})
    return params


def _build_list_page_req(
    status: str | None, reactive_app_name: str | None, limit: int, cursor: str | None
) -> Request[BuildListPage]:
    return Request(
        "GET",
        "/builds",
        BuildListPage.model_validate,
        params=_page_params(
            limit, cursor, status=status, reactive_app_name=reactive_app_name
        ),
        operation="List builds",
    )


def _list_of(key: str, model: Any) -> Any:
    def parse(payload: Any) -> list[Any]:
        return [model.model_validate(x) for x in (payload or {}).get(key, [])]

    return parse


class APIRegistryReads(HTTPTransport, RegistryReadsABC):
    """See the module docstring."""

    # -- builds and plans -------------------------------------------------------

    def build_list(
        self,
        *,
        status: str | None = None,
        reactive_app_name: str | None = None,
        limit: int = 100,
    ) -> list[BuildInfo]:
        return self.build_list_page(
            status=status, reactive_app_name=reactive_app_name, limit=limit
        ).builds

    def build_list_running(
        self, *, reactive_app_name: str | None = None, limit: int = 100
    ) -> list[UUID]:
        builds = self.build_list(
            status="running", reactive_app_name=reactive_app_name, limit=limit
        )
        return [b.id for b in builds]

    def build_list_page(
        self,
        *,
        status: str | None = None,
        reactive_app_name: str | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> BuildListPage:
        return self.call(_build_list_page_req(status, reactive_app_name, limit, cursor))

    def plan_get(self, plan_id: UUID) -> PlanDetail:
        return self.call(
            Request(
                "GET",
                f"/plans/{plan_id}",
                PlanDetail.model_validate,
                operation=f"Get plan {plan_id}",
            )
        )

    def build_list_plans(self, build_id: UUID) -> list[PlanDetail]:
        return self.call(
            Request(
                "GET",
                f"/builds/{build_id}/plans",
                _list_of("plans", PlanDetail),
                operation=f"List plans of build {build_id}",
            )
        )

    def build_list_tick_summaries(
        self, build_id: UUID, *, limit: int = 20
    ) -> list[TickSummaryRecord]:
        return self.call(
            Request(
                "GET",
                f"/builds/{build_id}/tick-summaries",
                _list_of("summaries", TickSummaryRecord),
                params={"limit": str(limit)},
                operation=f"List tick summaries of build {build_id}",
            )
        )

    # -- tasks ------------------------------------------------------------------

    def task_get(self, task_id: str) -> TaskInfo:
        return self.call(
            Request(
                "GET",
                f"/tasks/{task_id}",
                TaskInfo.model_validate,
                operation=f"Get task {task_id}",
            )
        )

    def task_list_artifacts(self, task_id: str) -> list[TaskArtifactInfo]:
        return self.call(_task_artifacts_req(task_id))

    def task_list(
        self,
        *,
        status: str | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> TaskListPage:
        return self.call(
            Request(
                "GET",
                "/tasks",
                TaskListPage.model_validate,
                params=_page_params(limit, cursor, status=status),
                operation="List tasks",
            )
        )

    def task_list_executions(
        self, task_id: str, *, include_ended: bool = True, limit: int = 100
    ) -> list[ExecutionInfo]:
        return self.call(
            Request(
                "GET",
                f"/tasks/{task_id}/executions",
                _list_of("executions", ExecutionInfo),
                params={
                    "include_ended": "true" if include_ended else "false",
                    "limit": str(limit),
                },
                operation=f"List executions of task {task_id}",
            )
        )

    def task_events(self, task_id: str, *, limit: int = 500) -> list[EventInfo]:
        return self.call(
            Request(
                "GET",
                f"/tasks/{task_id}/events",
                _list_of("events", EventInfo),
                params={"limit": str(limit)},
                operation=f"List events of task {task_id}",
            )
        )

    # -- deployments ------------------------------------------------------------

    def deployment_get(self, deployment_id: UUID) -> DeploymentInfo:
        return self.call(
            Request(
                "GET",
                f"/deployments/{deployment_id}",
                DeploymentInfo.model_validate,
                operation=f"Get deployment {deployment_id}",
            )
        )
