"""The claim TTL of a detached execution.

Every execution claims (D11) with a finite expiry — nothing is live
forever. An in-process execution's claim is short and renewed by the driver
(:class:`~stardag.build._session.ClaimRenewal`); a detached one cannot be
renewed by anybody alive to know it is running, so its TTL is derived from
the backend's own wall-clock limit: the backend kills the execution before
its claim lapses, so the only claims that lapse belong to executions that
are already dead, and a lapsed claim is taken over by the next claiming
start (design.md, "The runnable rule").
"""

from __future__ import annotations

import logging

from stardag._core.base_task import BaseTask
from stardag.build._base import TaskExecutorABC

logger = logging.getLogger(__name__)

# Slack added to an executor's timeout: the claim is taken *before* the
# spawn, so it absorbs queueing and cold start the timeout clock has not
# started counting; a backend does not kill the instant its timeout
# elapses; clocks differ. Generous on purpose — too short makes a live
# execution's claim stealable (a duplicate execution), too long only heals
# an abandoned claim later.
CLAIM_TTL_GRACE_SECONDS = 900.0

# The registry's accepted range for ``claim_ttl_seconds``: positive, and at
# most 24 hours (nothing is live forever).
MIN_CLAIM_TTL_SECONDS = 60
MAX_CLAIM_TTL_SECONDS = 24 * 3600


def claim_ttl_seconds(task: BaseTask, task_executor: TaskExecutorABC) -> int | None:
    """The claim TTL of a detached execution of ``task``: the executor's
    timeout plus grace, clamped to the registry's range. None when the
    executor enforces no limit, leaving the registry's default (3600 s)."""
    try:
        timeout = task_executor.execution_timeout_seconds(task)
    except Exception:
        logger.debug(
            f"Execution timeout resolution failed for task {task.id}; "
            "claiming with the registry's default TTL.",
            exc_info=True,
        )
        return None
    if timeout is None:
        return None
    ttl = int(timeout + CLAIM_TTL_GRACE_SECONDS)
    return max(MIN_CLAIM_TTL_SECONDS, min(MAX_CLAIM_TTL_SECONDS, ttl))
