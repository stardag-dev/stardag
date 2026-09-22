from __future__ import annotations

import logging
import typing
from uuid import UUID

from stardag.build._base import FailMode
from stardag.exceptions import NotFoundError, is_missing_route_error
from stardag.registry import (
    BuildFrontier,
    RegistryABC,
)

from stardag.build._reactive._budgets import _retry_allowed
from stardag.build._reactive._frontier_actions import (
    _REVOKED_STATUSES,
    _RUNNING_STATUSES,
    _TERMINAL_BUILD_STATUSES,
)

if typing.TYPE_CHECKING:
    from stardag.build._reactive._tick import TickConfig, TickSummary

logger = logging.getLogger(__name__)


async def _handle_terminal(
    frontier: BuildFrontier,
    *,
    build_id: UUID,
    registry: RegistryABC,
    config: TickConfig,
    summary: TickSummary,
    denied_this_round: int = 0,
) -> str | None:
    """Evaluate terminal conditions; emit build events. Returns terminal status.

    Returning ``None`` means "not terminal — keep waiting": the build has
    work in flight, or is waiting on a concurrency-limit slot.
    """
    if frontier.build_status in _TERMINAL_BUILD_STATUSES:
        # Nothing to do but report it. This tick reaches into no
        # container and releases nothing.
        #
        # Usually the claims are already gone: a cancel and a fail release
        # them server-side, in the transaction that made the build
        # terminal. Not always, and the exception is worth knowing before
        # relying on it -- a build swept by the reaper with
        # ``ReaperSettings.cascade`` off, or bulk-cancelled with its
        # ``cascade`` off, is terminal with its claims still held until
        # they expire. Either way there is nothing for this tick to do:
        # releasing is the server's, and it is not this build's tick that
        # would know which path brought it here. STA-103 removes those two
        # switches.
        #
        # The drain used to do both from here, and that is what it got
        # wrong: it released claims for containers it had asked a backend
        # to stop, with no way to know whether the stop took. The workers
        # find out for themselves now, at their own checkpoints
        # (``stardag.cancellation``).
        return frontier.build_status

    counts = frontier.status_counts
    running = sum(counts.get(status, 0) for status in _RUNNING_STATUSES)
    failed = counts.get("failed", 0)

    if failed > 0 and config.fail_mode == FailMode.FAIL_FAST:
        # Fail first, skip second, and the order is load-bearing.
        # ``build_fail_aio`` releases the claims this build holds, in the
        # same transaction that marks it failed — so by the time
        # skip-blocked runs, this build's still-running tasks are CANCELLED
        # and therefore seeds of the blocked closure. Skipping first would
        # leave their descendants dangling PENDING, because a RUNNING
        # intermediate may still complete and so blocks nothing.
        #
        # The cost of that order: if the skip call is lost, the build is
        # already terminal and no later tick will retry it, so the
        # descendants dangle. ``_skip_blocked`` logs at ERROR for exactly
        # that reason — it is cosmetic state, but it is now unrecoverable
        # cosmetic state.
        #
        # Nothing is stopped from here. The workers under those released
        # claims find out at their next cooperative checkpoint; see
        # ``stardag.cancellation``.
        await registry.build_fail_aio(
            build_id, f"{failed} task(s) failed (fail_mode=FAIL_FAST)"
        )
        await _skip_blocked(registry, build_id, summary)
        return "failed"

    roots_known = len(frontier.roots) == len(frontier.root_task_ids) > 0
    if roots_known and all(r.latest_status == "completed" for r in frontier.roots):
        await registry.build_complete_aio(build_id)
        return "completed"

    if denied_this_round > 0:
        # Tasks denied by concurrency limits in THIS pass are waiting for
        # slots held possibly by OTHER builds (running == 0 here doesn't
        # mean the env is idle) — never declare the build stuck. Scoped to
        # the current pass: a cumulative count would keep suppressing the
        # stuck check long after the denied tasks have run. The watchdog
        # re-ticks periodically; same-build slot releases notify directly.
        return None

    # A cancelled or skipped task the frontier lists as actionable but whose
    # attempt budget this build has already spent is inert: the pass did
    # not reset it (see ``_act_on_frontier``'s revoked phase) and no later
    # pass will, so it must not keep the build looking busy. Everything else
    # in ``actionable`` is work this pass acted on.
    inert_revoked = [
        item
        for item in frontier.actionable
        if item.latest_status in _REVOKED_STATUSES
        and not _retry_allowed(item.attempt_count, config.max_attempts)
    ]
    live_actionable = len(frontier.actionable) - len(inert_revoked)

    # Note: spawns within this iteration imply frontier.actionable was
    # non-empty, so this check can't misfire on the pre-spawn snapshot.
    if live_actionable == 0 and running == 0:
        # Nothing runnable and nothing running: the build is genuinely
        # stuck, and it fails rather than idling forever. There is no
        # "waiting on another build" case left to distinguish here:
        # dependency edges are scoped to the build's own structure scope,
        # and the server re-closes the plan over that scope before it
        # reports a stalled frontier, so every gating upstream is in the
        # plan — RUNNING ones in ``running``, cancelled and skipped ones in
        # ``actionable`` (reset and run by the pass that saw them), and
        # what is left is a result (FAILED) that ``fail_mode`` owns, or a
        # task that cannot be reached. The status counts say which.
        incomplete_roots = sorted(
            r.task_id for r in frontier.roots if r.latest_status != "completed"
        )
        missing_roots = sorted(
            set(frontier.root_task_ids) - {r.task_id for r in frontier.roots}
        )
        reason = (
            "No runnable or running tasks left but roots are not complete "
            f"(status counts: {counts})"
        )
        if incomplete_roots:
            reason += f". Incomplete roots: {', '.join(incomplete_roots[:5])}"
            if len(incomplete_roots) > 5:
                reason += f" and {len(incomplete_roots) - 5} more"
        if missing_roots:
            reason += (
                f". {len(missing_roots)} root(s) are not registered in this "
                "environment at all"
            )
        if counts.get("failed"):
            reason += (
                ". A failed task in the plan is a result, which this build's "
                "fail_mode leaves alone; re-trigger the build "
                f"(build_trigger(..., build_id={build_id}, reactive=True)) to "
                "reset it and try again"
            )
        if inert_revoked:
            named = ", ".join(
                f"{item.task_id} ({item.latest_status})" for item in inert_revoked[:5]
            )
            reason += (
                f". {len(inert_revoked)} cancelled/skipped task(s) could be "
                "run by this build but their attempt budget in this build is "
                f"spent ({config.max_attempts} attempt(s) per round): {named}. "
                "Re-trigger the build to start a new round"
            )
        logger.error(f"Failing build {build_id}: {reason}")
        # Fail before skipping, as above: the failure releases this build's
        # claims, and a released claim is a seed of the blocked closure.
        await registry.build_fail_aio(build_id, reason)
        await _skip_blocked(registry, build_id, summary)
        return "failed"

    return None


async def _skip_blocked(
    registry: RegistryABC, build_id: UUID, summary: TickSummary
) -> None:
    """Mark tasks transitively blocked by failures as skipped (best-effort).

    Cosmetic-but-important: without it, blocked tasks dangle PENDING in the
    registry/UI forever while the build shows failed. Old servers without
    the endpoint are tolerated (missing-route 404 → skip silently omitted);
    app-level 404s (e.g. the build no longer exists) are re-raised — they
    signal a registry inconsistency the tick must not paper over.
    """
    try:
        skipped = await registry.build_skip_blocked_aio(build_id)
        summary.skipped += len(skipped)
    except NotFoundError as e:
        if not is_missing_route_error(e):
            raise
        logger.warning(
            "Registry server does not support skip-blocked; tasks blocked "
            "by the failure will remain pending."
        )
    except Exception as e:
        # ERROR, not WARNING: the build is already terminal by the time
        # this runs (see ``_handle_terminal``), so nothing will retry it —
        # a lost skip leaves blocked descendants PENDING for good.
        logger.error(f"Failed to skip blocked tasks for build {build_id}: {e}")
