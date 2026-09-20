from __future__ import annotations

import logging
import typing
from uuid import UUID

from stardag.build._base import (
    FailMode,
    TaskExecutorABC,
)
from stardag.exceptions import NotFoundError, is_missing_route_error
from stardag.registry import (
    BuildExecution,
    BuildFrontier,
    RegistryABC,
)

from stardag.build._reactive._budgets import _retry_allowed
from stardag.build._reactive._frontier_actions import (
    _REVOKED_STATUSES,
    _INTERRUPTED_STATUS,
    _RUNNING_STATUSES,
    _TERMINAL_BUILD_STATUSES,
    _load_task,
)

if typing.TYPE_CHECKING:
    from stardag.build._reactive._tick import TickConfig, TickSummary

logger = logging.getLogger(__name__)

# A backstop on the executions drain, not a policy. The loop's real stop
# condition is the cursor: it is keyed on the task, so every page strictly
# advances through a finite set and the drain terminates on its own. This
# only bounds a server that answers with a cursor going nowhere, which would
# otherwise spin inside a tick and cost the build its last chance to stop
# anything. Set high enough that reaching it means a bug rather than a wide
# build — at the server's page size, tens of thousands of live executions.
_MAX_EXECUTION_PAGES = 200

# How many times the cancel drain re-lists before giving up. Two is the
# meaningful number: one pass to stop what is there, one to confirm nothing
# arrived while it was working. More would only matter if something were
# racing the drain deliberately.
_CANCEL_RECONCILE_PASSES = 2


async def _handle_terminal(
    frontier: BuildFrontier,
    *,
    build_id: UUID,
    registry: RegistryABC,
    task_executor: TaskExecutorABC,
    config: TickConfig,
    summary: TickSummary,
    denied_this_round: int = 0,
) -> str | None:
    """Evaluate terminal conditions; emit build events. Returns terminal status.

    Returning ``None`` means "not terminal — keep waiting": the build has
    work in flight, or is waiting on a concurrency-limit slot.
    """
    if frontier.build_status in _TERMINAL_BUILD_STATUSES:
        if frontier.build_status == "cancelled":
            # Cancelled externally (e.g. UI): stop the running work.
            await _cancel_running(frontier, build_id, registry, task_executor, summary)
        return frontier.build_status

    counts = frontier.status_counts
    running = sum(counts.get(status, 0) for status in _RUNNING_STATUSES)
    failed = counts.get("failed", 0)

    if failed > 0 and config.fail_mode == FailMode.FAIL_FAST:
        await _cancel_running(frontier, build_id, registry, task_executor, summary)
        await _skip_blocked(registry, build_id, summary)
        await registry.build_fail_aio(
            build_id, f"{failed} task(s) failed (fail_mode=FAIL_FAST)"
        )
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
        await _skip_blocked(registry, build_id, summary)
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
        await registry.build_fail_aio(build_id, reason)
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
        logger.warning(f"Failed to skip blocked tasks for build {build_id}: {e}")


async def _cancel_running(
    frontier: BuildFrontier,
    build_id: UUID,
    registry: RegistryABC,
    task_executor: TaskExecutorABC,
    summary: TickSummary,
) -> None:
    """Stop the detached executions **this build** is responsible for.

    Authority to revoke is build-scoped (see the execution-claims design
    note). What this function may act on is therefore not "everything that
    looks alive in the frontier" but "the executions this build started and
    has not seen end" — which the registry answers from its event log
    (``GET .../executions``), with the backend and ref needed to stop each.

    Reading it from the frontier instead was wrong in both directions, and
    both cost real damage:

    - ``running`` is every RUNNING task in the build's *plan*, and after
      plan closure that includes tasks another build claimed and is
      executing. Cancelling one killed a live worker and released its
      claim, so the task was handed to a third build while the second was
      still writing its target. A cancelled build did this on *every* tick
      it received, for as long as neighbours kept touching its tasks.
    - A cascading build cancel writes TASK_CANCELLED for the claims this
      build held — releasing them — while the containers keep running. Such
      a task is in neither ``running`` nor ``actionable``, so nothing
      reached it, and ``cancel_detached`` has exactly one caller: this one.
      The claim was released and the execution was not stopped, which is
      how two builds came to run the same task at once.

    Asking about the task's *current* state does not fix the second one
    either, which a live run had to demonstrate: the whole point of
    releasing the claim is that the next build may take the task over, and
    it did so three seconds later — long before the cancelled build's tick
    ran. By then the task row named the new execution. The ref this build
    recorded when it started the task is the only thing that stays true,
    and cancelling it cannot touch anybody else's container.

    Each stopped execution is also recorded as TASK_CANCELLED, unless the
    registry already shows it cancelled — a worker killed by the backend
    cannot reliably self-report, and without the event the task dangles
    RUNNING, keeping its pending descendants out of the skip-blocked
    closure and holding its concurrency-limit slots forever.

    Cancelling is best-effort throughout and idempotent at the backend, so
    a ref stopped twice is harmless. What must not happen is stopping one
    this build does not own.
    """
    # Re-listed until it comes back with nothing new, rather than stopped in
    # one pass. An execution that appears *while* the drain is running would
    # otherwise be missed for good: a terminal build gets no second tick, and
    # nothing re-flags it. Two shapes can do that — a ref recorded between
    # the listing and the POST, where the conditional cancel correctly
    # declines to stamp it and correctly does not stop it either; and a first
    # ref-bearing start committed mid-paging, which can land behind the
    # cursor and be skipped by every later page.
    #
    # Neither is reachable through a writer that exists today: the scheduler
    # lease single-flights ticks, a terminal tick spawns nothing, and a
    # worker self-reporting its start carries no executor fields at all. The
    # loop is here because that argument is a chain of three facts about
    # other people's code, and re-asking costs one request on a path that
    # runs once, at build death.
    stopped: set[tuple[str, str]] = set()
    for _ in range(_CANCEL_RECONCILE_PASSES):
        listed = await _executions_to_stop(frontier, build_id, registry, summary)
        remaining = [
            item for item in listed if (item.executor, item.executor_ref) not in stopped
        ]
        if not remaining:
            break
        await _stop_each(remaining, build_id, registry, task_executor, summary, stopped)
    else:
        # Reached when the final pass still found work — which it then
        # stopped. So this is not "they are still running": it is "they
        # were still arriving when we ran out of passes", and anything that
        # appeared after that last stop has nothing left to stop it, since
        # a terminal build gets no further tick.
        logger.warning(
            f"Build {build_id}: executions were still arriving on the last "
            f"of {_CANCEL_RECONCILE_PASSES} cancel-drain passes. Those found "
            "were stopped; any that appeared after it keep running until "
            "their backend stops them."
        )


async def _stop_each(
    items: "list[BuildExecution]",
    build_id: UUID,
    registry: RegistryABC,
    task_executor: TaskExecutorABC,
    summary: TickSummary,
    stopped: "set[tuple[str, str]]",
) -> None:
    """Stop each listed execution and record the revocation. Best-effort."""
    for item in items:
        # Marked only once this execution is fully dealt with — the
        # container stopped *and* the revocation recorded.
        #
        # Marking on arrival filtered a failed load or a raised cancel out
        # of the next pass as though handled. Marking after the stop alone
        # is barely better and fails in the direction that matters: the
        # container is gone but the claim was never released, so the task
        # stays RUNNING forever holding a claim and a limit slot, which is
        # the leak this whole cascade exists to prevent. A terminal build
        # gets no third chance, so the pass has to be able to retry the
        # half that failed. Re-stopping an already-stopped execution is
        # cheap; a leaked claim is not.
        task = await _load_task(item.task_id, registry, quiet=True)
        if task is None:
            continue
        try:
            await task_executor.cancel_detached(task, item.executor, item.executor_ref)
            summary.cancelled_refs += 1
        except Exception as e:
            logger.warning(
                f"Failed to cancel detached execution "
                f"{item.executor_ref!r} for task {item.task_id}: {e}"
            )
            continue
        try:
            # The registry re-checks, on the locked row, that this build
            # still holds the task in a status with an execution to revoke
            # *and that the execution is the one just stopped*, recording
            # nothing otherwise. Three things need that, and none is visible
            # from the listing: a task the cascade already cancelled needs no
            # second event; another build may have reset this one and be
            # about to run it, where writing CANCELLED stamps a neighbour's
            # freshly scheduled task dead; and this build may have started it
            # again under a new ref, where writing CANCELLED would revoke the
            # claim of an execution nobody stopped.
            await registry.task_cancel_aio(
                build_id,
                task,
                if_executor=item.executor,
                if_executor_ref=item.executor_ref,
            )
            stopped.add((item.executor, item.executor_ref))
        except Exception as e:
            logger.warning(f"Failed to record cancellation of task {item.task_id}: {e}")


async def _executions_to_stop(
    frontier: BuildFrontier,
    build_id: UUID,
    registry: RegistryABC,
    summary: TickSummary,
) -> list[BuildExecution]:
    """Ask the registry what is this build's to stop; fall back if it can't.

    The fallback is for a server predating the route — a missing route or a
    backend that does not implement it, and **nothing else**. A transient
    failure must not land here: for a cascaded build the frontier sees
    CANCELLED tasks and therefore nothing at all, so degrading quietly would
    report "nothing to stop", let the tick exit, and leave the containers
    running with no second chance — a terminal build gets no further tick,
    and ``notify`` no longer re-flags one. Better to let that error out of
    the tick, where it is visible and the cancel can be re-issued.

    What the fallback is, when it does apply: the old frontier-derived list
    with the ownership filter the frontier can now support — ``latest_status_build_id``. A server old enough to lack
    *that* too reports None, and None cannot be read as "not mine": it
    means "this server cannot say", so the item is acted on exactly as
    before. That keeps an old server no worse off than it is today, which
    is the rule every version-skew decision here follows, while the two
    shapes only the route can see (a foreign claim on a server that does
    report the owner, and this build's own cascaded executions) are
    handled as soon as the server is new enough to know about them.
    """
    try:
        executions: list[BuildExecution] = []
        cursor: str | None = None
        # Drained, not sampled. Stopping an execution records nothing — a
        # cancel is a request, not an end — so the answer does not shrink as
        # this pass works through it, and one call would leave a wide
        # build's tail running with nothing to come back for: a terminal
        # tick does not run again, and a build that is no longer RUNNING is
        # not re-flagged.
        for _ in range(_MAX_EXECUTION_PAGES):
            listed = await registry.build_get_executions_aio(build_id, cursor=cursor)
            executions += listed.executions
            if not listed.truncated or not listed.next_cursor:
                break
            if listed.next_cursor == cursor:
                # The cursor is keyed on the task, so a page that does not
                # advance it cannot be the server making progress — it is a
                # server that would hand back the same page forever.
                logger.warning(
                    f"Build {build_id}: the executions cursor stopped "
                    "advancing; stopping the ones read so far."
                )
                break
            cursor = listed.next_cursor
        else:
            logger.warning(
                f"Build {build_id} has more executions to stop than "
                f"{_MAX_EXECUTION_PAGES} pages, which should not be "
                "reachable; stopping the ones read so far. The rest keep "
                "running until their backend times them out."
            )
        return executions
    except NotFoundError as e:
        if not is_missing_route_error(e):
            raise
        logger.warning(
            "Registry server does not support the build executions route; "
            "falling back to the frontier, which cannot see executions this "
            "build has already cancelled."
        )
    except NotImplementedError:
        pass

    cancellable = _RUNNING_STATUSES + (_INTERRUPTED_STATUS,)
    # Re-read, because the snapshot the caller holds is the PRE-action one.
    # ``_act_on_frontier`` has already run by the time terminal handling
    # decides to cancel, so that snapshot can be wrong in both directions:
    # a task it resumed or spawned this pass is live under a ref the
    # snapshot has never seen, and a task the snapshot lists as INTERRUPTED
    # may now be RUNNING under a *different* ref.
    try:
        frontier = await registry.build_get_frontier_aio(build_id)
    except Exception as e:
        logger.warning(
            f"Could not re-read the frontier of build {build_id} before "
            f"cancelling ({e}); falling back to the pre-action snapshot, "
            "which may miss executions started in this pass."
        )
    items = list(frontier.running or [])
    seen = {item.task_id for item in items}
    items += [
        item
        for item in frontier.actionable
        if item.latest_status in cancellable and item.task_id not in seen
    ]
    executions: list[BuildExecution] = []
    for item in items:
        if item.latest_status not in cancellable:
            continue
        if item.latest_executor is None or item.latest_executor_ref is None:
            continue
        owner = item.latest_status_build_id
        if owner is not None and owner != build_id:
            logger.info(
                f"Not stopping the execution of task {item.task_id}: it is "
                f"held by build {owner}, not this one."
            )
            continue
        executions.append(
            BuildExecution(
                task_id=item.task_id,
                latest_status=item.latest_status,
                executor=item.latest_executor,
                executor_ref=item.latest_executor_ref,
                executor_metadata=item.latest_executor_metadata,
                latest_status_at=item.latest_status_at,
            )
        )
    return executions
