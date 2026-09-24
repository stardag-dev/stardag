"""The guardrail on every ``/api/v2`` write route: the per-workspace rate
limit (``LIMITS_MAX_REQUESTS_PER_MINUTE``), carried over from v1.

A router-level dependency of the v2 router, so no write route can be added
without it; reads (``GET``/``HEAD``) are not limited. Disabled unless the
setting is configured. The 24-hour creation quota is charged in the
registration service, where the inserted rows are known.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from stardag_api.auth import SdkAuth, require_sdk_auth
from stardag_api.config import limits_settings
from stardag_api.limits import check_rate_limit
from stardag_api.services.errors import TooManyRequests

_READS = frozenset({"GET", "HEAD", "OPTIONS"})


async def v2_write_guard(
    request: Request, auth: Annotated[SdkAuth, Depends(require_sdk_auth)]
) -> None:
    if request.method in _READS:
        return
    error = check_rate_limit(auth.workspace_id, limits_settings)
    if error is not None:
        raise TooManyRequests(
            "rate_limited",
            error.message,
            limit=error.limit,
            retry_after=error.retry_after,
        )
