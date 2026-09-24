"""Terminal detection: when a reactive build is done, and how it ends."""

from __future__ import annotations

import logging
import typing
from uuid import UUID

from stardag.exceptions import APIError
from stardag.registry import BuildFrontier, RegistryABC

if typing.TYPE_CHECKING:
    from stardag.build._reactive._config import TickConfig, TickSummary
    from stardag.build._reactive._frontier_actions import PassResult

logger = logging.getLogger(__name__)

TERMINAL_BUILD_STATUSES = ("completed", "failed", "cancelled")


async def handle_terminal(
    frontier: BuildFrontier,
    *,
    build_id: UUID,
    registry: RegistryABC,
    config: "TickConfig",
    summary: "TickSummary",
    pass_result: "PassResult",
) -> str | None:
    """Decide from a frontier (read before this pass acted) whether the
    build is finished, and end it. Returns the terminal status, or None to
    keep going.

    - A build already terminal is reported as it is: nothing to release —
      its terminal transition released the claims of its plans.
    - ``plan_complete`` (sealed, every non-excluded member COMPLETED):
      complete the build. ``/complete`` recomputes the predicate in its own
      transaction and refuses (``plan_incomplete``) if an observation
      withdrew a completion meanwhile; the tick then keeps going.
    - **Stalled** — nothing to expand, nothing runnable, nothing running,
      and this pass did nothing: what is left is a result (a failed member,
      its blocked downstream, an excluded member) that ``fail_mode`` owns.
      The build fails, and its blocked members are marked skipped. The
      frontier carries no member statuses beyond the three lists, so a
      FAIL_FAST build fails when it stalls, not at the moment a member
      fails.
    """
    if frontier.build_status in TERMINAL_BUILD_STATUSES:
        return frontier.build_status
    if frontier.plan_id is None:
        return None
    if frontier.plan_complete:
        try:
            await registry.build_complete_aio(build_id)
        except APIError as e:
            if e.code == "build_terminal":
                return _already_terminal(build_id, e)
            if e.code != "plan_incomplete":
                raise
            logger.info(f"Build {build_id} not complete after all: {e}")
            return None
        return "completed"
    if pass_result.acted or pass_result.limit_denied or pass_result.claim_denied:
        return None
    if frontier.discovery_jobs or frontier.runnable or frontier.running:
        return None
    reason = (
        "Nothing is runnable, nothing is running and the plan is not complete: "
        "what is left is a failed member, the members it blocks, or an excluded "
        f"one (fail_mode={config.fail_mode}). Re-trigger the build "
        f"(build_trigger(..., build_id={build_id}, reactive=True)) to reset "
        "failed members and try again."
    )
    if not frontier.sealed:
        reason = (
            "The plan's static phase cannot be sealed (a root not expanded, or "
            "its closure open) and nothing is left to expand or run. "
            "Re-trigger the build to finish its registration."
        )
    logger.error(f"Failing build {build_id}: {reason}")
    try:
        await registry.build_fail_aio(build_id, reason)
    except APIError as e:
        if e.code != "build_terminal":
            raise
        return _already_terminal(build_id, e)
    try:
        skipped = await registry.build_skip_blocked_aio(build_id)
        summary.skipped += len(skipped)
    except Exception as e:
        # Cosmetic, and unrecoverable once the build is terminal: logged
        # loudly rather than failing the tick.
        logger.error(f"Failed to skip blocked members of build {build_id}: {e}")
    return "failed"


def _already_terminal(build_id: UUID, error: APIError) -> str:
    """The build went terminal after the frontier was read (an operator's
    cancel): a terminal status is sticky, so the tick's own report was
    recorded, not applied, and the status it found is the one that stands."""
    status = str((error.payload or {}).get("build_status") or "cancelled")
    logger.info(f"Build {build_id} is already {status}; its status stands.")
    return status
