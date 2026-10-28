"""The inspecting reads of :class:`~stardag.registry.APIRegistry` over HTTP
(the seams of :class:`~stardag.registry._base_reads.RegistryReadsABC`, plus
``task_get`` and ``task_list_artifacts``), and the one write that sits with
them, ``task_annotate``. ``task_instance_get`` reads through the local
instance-body cache (:mod:`._instance_cache`). Split from ``_api_registry.py``
by the module-size rule; each route is one :class:`Request`, as there.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from types import EllipsisType
from typing import Any
from uuid import UUID

from stardag._core.task_id import same_task_id
from stardag.exceptions import APIError, NotFoundError, is_missing_route_error
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


_BARE_STRING_TAGS = (
    "tags must be a sequence of strings, not a bare string "
    "(tags=['prod'], not tags='prod')"
)


def _tag_list(tags: Any) -> list[str]:
    """``tags`` as sent: ``None`` is refused rather than read as "clear" or
    "unchanged" (``[]`` clears, omitting leaves them)."""
    if tags is None or isinstance(tags, str):
        raise TypeError(
            "tags must be a sequence of strings: [] clears the tags, and "
            "omitting the argument leaves them unchanged"
        )
    return list(tags)


def _task_list_req(
    *,
    status: str | None,
    namespace: str | None,
    name: str | None,
    tags: Sequence[str] | None,
    id_prefix: str | None,
    q: str | None,
    limit: int,
    cursor: str | None,
) -> Request[TaskListPage]:
    params: dict[str, Any] = _page_params(
        limit,
        cursor,
        status=status,
        namespace=namespace,
        name=name,
        id_prefix=id_prefix,
        q=q,
    )
    if isinstance(tags, str):
        raise TypeError(_BARE_STRING_TAGS)
    if tags:
        params["tag"] = list(tags)
    discovery = sorted(set(params) - {"limit", "cursor", "status"})

    def parse(payload: Any) -> TaskListPage:
        # A registry that predates the discovery filters ignores them as
        # unknown query parameters and answers unfiltered; it also serves
        # no ``tags`` on its items, which is how that is told apart.
        items = (payload or {}).get("tasks") or []
        if discovery and items and "tags" not in items[0]:
            raise APIError(
                "List tasks: this registry does not support the filters "
                f"{', '.join(discovery)} and would have answered unfiltered; "
                "upgrade the registry server"
            )
        return TaskListPage.model_validate(payload)

    return Request("GET", "/tasks", parse, params=params, operation="List tasks")


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

    Only a missing route falls back: FastAPI's bare ``{"detail": "Not
    Found"}``, with no structured payload. A registry that has the route
    and answers "no such instance" (structured, whatever its message) is
    authoritative.
    """
    if err.payload is not None or not is_missing_route_error(err):
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
        if (
            hit is not None
            and task_id is not None
            and not same_task_id(hit.task_id or "", task_id)
        ):
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
        # Off the event loop: the cache is file I/O, and the first use of
        # an API-key partition derives its fingerprint (a deliberately slow
        # KDF).
        partition, hit = await asyncio.to_thread(self._cached_instance, iid, task_id)
        if hit is not None:
            return hit
        try:
            info = await self.acall(_instance_get_req(iid))
        except NotFoundError as e:
            fallback = _fallback_or_raise(e, task_id)
            task = await self.acall(_task_get_req(fallback, _FALLBACK_INSTANCE_LIMIT))
            info = _pick_instance(task, iid)
        if partition is not None:
            await asyncio.to_thread(_instance_cache.write, partition, info)
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

    def task_annotate(
        self,
        task_id: str,
        *,
        tags: Sequence[str] | EllipsisType = ...,
        description: str | None | EllipsisType = ...,
    ) -> TaskInfo:
        body: dict[str, Any] = {}
        if tags is not ...:
            body["tags"] = _tag_list(tags)
        if description is not ...:
            body["description"] = description
        try:
            return self.call(
                Request(
                    "PATCH",
                    f"/tasks/{task_id}",
                    TaskInfo.model_validate,
                    json=body,
                    operation=f"Annotate task {task_id}",
                )
            )
        except APIError as e:
            if e.status_code == 405:
                raise APIError(
                    f"Annotate task {task_id}: this registry does not support "
                    "task annotations; upgrade the registry server",
                    status_code=405,
                ) from e
            raise

    def task_list_artifacts(self, task_id: str) -> list[TaskArtifactInfo]:
        return self.call(_task_artifacts_req(task_id))

    def task_list(
        self,
        *,
        status: str | None = None,
        namespace: str | None = None,
        name: str | None = None,
        tags: Sequence[str] | None = None,
        id_prefix: str | None = None,
        q: str | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> TaskListPage:
        return self.call(
            _task_list_req(
                status=status,
                namespace=namespace,
                name=name,
                tags=tags,
                id_prefix=id_prefix,
                q=q,
                limit=limit,
                cursor=cursor,
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
