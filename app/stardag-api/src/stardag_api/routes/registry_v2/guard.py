"""The guardrail on every ``/api/v2`` write route: the per-workspace rate
limit (``LIMITS_MAX_REQUESTS_PER_MINUTE``, or the workspace's own
``max_write_requests_per_minute`` set by the hosting operator), carried
over from v1. The writes that finish running work are exempt (``_EXEMPT``).

A router-level dependency of the v2 router, so no write route can be added
without it; reads (``GET``/``HEAD``) are not limited. Disabled unless the
setting is configured. The 24-hour creation quotas are charged in the
services that insert the rows (registration, artifact upload), where the
inserted rows are known.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request
from fastapi.routing import APIRoute

from stardag_api.auth import SdkAuth, require_sdk_auth
from stardag_api.config import limits_settings
from stardag_api.limits import check_rate_limit, workspace_rate_limit_override
from stardag_api.routes.registry_v2._common import Db
from stardag_api.services.errors import TooManyRequests

_READS = frozenset({"GET", "HEAD", "OPTIONS"})

#: Writes that finish or keep alive work already running, exempt from the
#: rate limit (STA-135): an execution's end reports, an operator's (or a
#: reconciling tick's) end of one, a claim's renewal, the scheduler lease's
#: renewal and release, and the wake-up flag a finished worker sets.
#: Refusing these does not shed load -- the work is done or running either
#: way -- it only orphans claims and strands builds. They stay bounded,
#: and so does cost: each is a fixed number per claim or per lease, and
#: what takes on new work (claims, registrations, build creation, the
#: lease acquire) stays limited.
_EXEMPT = frozenset(
    {
        ("POST", "/plans/{plan_id}/members/{task_id}/complete"),
        ("POST", "/plans/{plan_id}/members/{task_id}/fail"),
        ("POST", "/plans/{plan_id}/members/{task_id}/suspend"),
        ("POST", "/plans/{plan_id}/members/{task_id}/yield"),
        ("POST", "/plans/{plan_id}/members/{task_id}/interrupt"),
        ("POST", "/plans/{plan_id}/members/{task_id}/preempt"),
        ("POST", "/executions/{execution_id}/stopped"),
        ("POST", "/tasks/{task_id}/claim/renew"),
        ("PUT", "/builds/{build_id}/scheduler-lease"),
        ("DELETE", "/builds/{build_id}/scheduler-lease"),
        ("POST", "/builds/{build_id}/notify"),
    }
)

_PREFIX = "/api/v2"


def is_exempt(method: str, route_path: str) -> bool:
    """Whether a write to ``route_path`` (the route's template, prefix
    included or not) is exempt from the rate limit."""
    return (method, route_path.removeprefix(_PREFIX)) in _EXEMPT


async def v2_write_guard(
    request: Request, auth: Annotated[SdkAuth, Depends(require_sdk_auth)], db: Db
) -> None:
    if request.method in _READS:
        return
    route = request.scope.get("route")
    if isinstance(route, APIRoute) and is_exempt(request.method, route.path):
        return
    override = await workspace_rate_limit_override(db, auth.workspace_id)
    error = check_rate_limit(auth.workspace_id, limits_settings, override=override)
    if error is not None:
        raise TooManyRequests(
            "rate_limited",
            error.message,
            limit=error.limit,
            retry_after=error.retry_after,
        )
