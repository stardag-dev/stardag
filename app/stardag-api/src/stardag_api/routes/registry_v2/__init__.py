"""The ``/api/v2`` registry routes, one module per resource.

Thin by rule (engineering rule 1): a route parses, resolves the environment
from the caller's credentials, calls one service, and converts its result.
Service refusals (:class:`stardag_api.services.errors.RegistryError`) are
mapped to their status codes by the one handler in ``main.py``, with the
service's ``code`` as ``detail.code``.

- ``builds``: a build, its plans, frontier, lifecycle, events and ledger
- ``plans``: registration, plan reads, member transitions, ``/yield``,
  exclusion, artifact upload
- ``tasks``: a completion, the listing by status, its executions, events,
  artifacts, the claim renewal
- ``executions``: an operator end of one execution
- ``reactive``: wake-ups, the scheduler lease, reactive meta, tick summaries
- ``scope``: deployments, settings, concurrency limits
- ``guard``: the rate limit on every write route
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from stardag_api.routes.registry_v2 import (
    builds,
    executions,
    plans,
    reactive,
    scope,
    tasks,
)
from stardag_api.routes.registry_v2.guard import v2_write_guard

# The rate limit applies to every write route, the sub-routers' included.
router = APIRouter(dependencies=[Depends(v2_write_guard)])
for module in (builds, plans, tasks, executions, reactive, scope):
    router.include_router(module.router)

__all__ = ["router"]
