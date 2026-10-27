"""The inspecting reads of :class:`~stardag.registry.APIRegistry` over HTTP
(the seams of :class:`~stardag.registry._base_reads.RegistryReadsABC`, plus
``task_get`` and ``task_list_artifacts``). ``task_instance_get`` reads
through the local instance-body cache (:mod:`._instance_cache`). Split from ``_api_registry.py``
by the module-size rule; each route is one :class:`Request`, as there.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from stardag.exceptions import NotFoundError, is_missing_route_error
from stardag.registry import _instance_cache
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
    StalledBuild,
    TaskArtifactInfo,
    TaskInfo,
    TaskInstanceInfo,
    TaskListPage,
    TickSummaryRecord,
)

# The most instances ``GET /tasks/{id}`` returns (the server's list cap):
# the fallback of ``task_instance_get`` against a registry without
# ``GET /instances/{id}`` looks among the newest this many.
_FALLBACK_INSTANCE_LIMIT = 500


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


def _instance_get_req(instance_id: UUID) -> Request[TaskInstanceInfo]:
    return Request(
        "GET",
        f"/instances/{instance_id}",
        TaskInstanceInfo.model_validate,
        operation=f"Get instance {instance_id}",
    )


def _task_get_req(task_id: str, limit: int | None = None) -> Request[TaskInfo]:
    return Request(
        "GET",
        f"/tasks/{task_id}",
        TaskInfo.model_validate,
        params={"limit": str(limit)} if limit is not None else {},
        operation=f"Get task {task_id}",
    )


def _fallback_or_raise(err: NotFoundError, task_id: str | None) -> str:
    """The task id to read the instance from when ``GET /instances/{id}``
    answered ``err``; raises when there is no fallback.

    Only a missing route falls back: a registry that has the route and
    answers "no such instance" is authoritative.
    """
    if not is_missing_route_error(err):
        raise err
    if task_id is None:
        raise NotFoundError(
            "This registry predates GET /instances/{id}: pass task_id= to "
            "read the instance from its task instead",
            detail=err.detail,
            payload=err.payload,
        ) from err
    return task_id


def _pick_instance(info: TaskInfo, instance_id: UUID) -> TaskInstanceInfo:
    for instance in info.instances:
        if instance.id == instance_id:
            return instance.model_copy(update={"task_id": info.task_id})
    raise NotFoundError(
        f"Get instance {instance_id}: resource not found",
        detail=(
            f"task {info.task_id} has no instance {instance_id} among its "
            f"newest {_FALLBACK_INSTANCE_LIMIT}"
        ),
    )


class APIRegistryReads(HTTPTransport, RegistryReadsABC):
    """See the module docstring."""

    # -- instances --------------------------------------------------------------

    def instance_cache_partition(self) -> str | None:
        kind, *rest = self.cache_identity
        if kind == "environment" and not rest[-1]:
            # No environment known and no API key to stand for one: which
            # environment answers is the server's default, not something
            # this client can key a partition by.
            return None
        return _instance_cache.partition_key(self.api_url, *self.cache_identity)

    def _cached_instance(
        self, instance_id: UUID, task_id: str | None
    ) -> tuple[str | None, TaskInstanceInfo | None]:
        partition = _instance_cache.registry_partition(self)
        if partition is None:
            return None, None
        hit = _instance_cache.read(partition, instance_id)
        if hit is not None and task_id is not None and hit.task_id != str(task_id):
            # The registry is authoritative for which task an instance
            # belongs to: re-read rather than answer from the copy.
            hit = None
        return partition, hit

    def task_instance_get(
        self, instance_id: UUID | str, *, task_id: str | None = None
    ) -> TaskInstanceInfo:
        iid = UUID(str(instance_id))
        partition, hit = self._cached_instance(iid, task_id)
        if hit is not None:
            return hit
        try:
            info = self.call(_instance_get_req(iid))
        except NotFoundError as e:
            fallback = _fallback_or_raise(e, task_id)
            task = self.call(_task_get_req(fallback, _FALLBACK_INSTANCE_LIMIT))
            info = _pick_instance(task, iid)
        if partition is not None:
            _instance_cache.write(partition, info)
        return info

    async def task_instance_get_aio(
        self, instance_id: UUID | str, *, task_id: str | None = None
    ) -> TaskInstanceInfo:
        iid = UUID(str(instance_id))
        partition, hit = self._cached_instance(iid, task_id)
        if hit is not None:
            return hit
        try:
            info = await self.acall(_instance_get_req(iid))
        except NotFoundError as e:
            fallback = _fallback_or_raise(e, task_id)
            task = await self.acall(_task_get_req(fallback, _FALLBACK_INSTANCE_LIMIT))
            info = _pick_instance(task, iid)
        if partition is not None:
            _instance_cache.write(partition, info)
        return info

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

    def build_list_stalled(
        self, *, older_than_seconds: int = 300, limit: int = 200
    ) -> list[StalledBuild]:
        return self.call(
            Request(
                "GET",
                "/stalled-builds",
                _list_of("builds", StalledBuild),
                params={
                    "older_than_seconds": str(older_than_seconds),
                    "limit": str(limit),
                },
                operation="List stalled builds",
            )
        )

    # -- tasks ------------------------------------------------------------------

    def task_get(self, task_id: str) -> TaskInfo:
        return self.call(_task_get_req(task_id))

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
