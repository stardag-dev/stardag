from __future__ import annotations

import logging
import typing
from uuid import UUID

from stardag import (
    BaseTask,
)
from stardag.registry import (
    RegistryABC,
)

if typing.TYPE_CHECKING:
    from stardag.build._reactive._tick import TickConfig, TickSummary

logger = logging.getLogger(__name__)


# =============================================================================
# The attempt budget (TickConfig.max_attempts)
# =============================================================================


def _retry_allowed(attempts_spent: "int | None", max_attempts: int) -> bool:
    """Whether one more attempt fits inside a ``max_attempts`` budget.

    ``attempts_spent`` is the number of attempts this build has spent on
    the task *including* the one that just failed, or ``None`` when the
    registry does not report attempt counts at all (see
    ``FrontierTaskRef.attempt_count``).

    **``None`` refuses the retry**, and that is the one rule here worth
    arguing about, since everywhere else in this module a missing field
    means "no evidence, don't act on it". A retry is different from the
    other decisions taken on missing evidence: it is the only one that
    *creates more of the same decision*. Failing a task on no evidence
    costs one build; retrying on no evidence costs an unbounded loop —
    fail, respawn, fail, respawn — because the thing that would eventually
    stop it is the counter that is missing. So an unreported count degrades
    to precisely the pre-``max_attempts`` behaviour (record the failure,
    never respawn) rather than to an unbounded one.

    A budget of one or less is checked first and refuses regardless: it is
    the explicit "no retries" setting, and no count changes that.
    """
    if max_attempts <= 1:
        return False
    if attempts_spent is None:
        return False
    return attempts_spent < max_attempts


def _start_denied_by_budget(attempt_count: "int | None", max_attempts: int) -> bool:
    """Whether a task arriving PENDING has already spent its whole budget.

    The mirror image of :func:`_retry_allowed` on missing evidence, and for
    the same reason read the other way round: refusing a start is an act,
    and acting on an unreported (``None``) or zero count would stop builds
    that are perfectly healthy — a task nobody has counted is an ordinary
    spawn candidate, not an exhausted one. Only a positive, server-reported
    count that has reached the budget denies anything.
    """
    if attempt_count is None:
        return False
    return attempt_count >= max(1, max_attempts)


def _attempts_phrase(attempts_spent: int, max_attempts: int) -> str:
    """How the attempt count reads in a log line or a failure message."""
    return f"{attempts_spent} of {max_attempts} allowed attempt(s) spent"


async def _record_task_failure(
    task: BaseTask,
    reason: str,
    *,
    build_id: UUID,
    registry: RegistryABC,
    config: TickConfig,
    summary: TickSummary,
    retryable: bool,
    attempts_spent: "int | None" = None,
) -> None:
    """Record a task failure, and retry it when the budget allows.

    The failure is **always** recorded first, even when the task is about
    to be reset to pending: the TASK_FAILED event is what releases the
    execution claim and the concurrency-limit slots the attempt was
    holding, and it is the only trace a later reader has that the attempt
    happened at all. The retry is then exactly what the server's retry
    endpoint does for an operator (and what discovery does on re-trigger) —
    flip a terminal-but-retryable status back to PENDING — so a respawn
    needs no new mechanism and no new server route.

    **Why this does not fight ``_handle_terminal``'s FAIL_FAST check.**
    That check reads ``frontier.status_counts``, which is the snapshot
    taken *before* this pass acted. A failure recorded and retried inside
    one pass is therefore never counted as a build-killing failure: by the
    time the next frontier is fetched — which happens immediately, since
    the pass acted — the task is PENDING again. A failure left failed
    (budget spent, or not retryable) shows up in the very next snapshot and
    fails the build promptly, exactly as before.

    **``retryable`` is the caller's verdict on the failure's nature**, and
    is deliberately not derivable here. See :func:`_act_on_frontier` for
    the split; the short version is that a tick retries the failures no
    execution backend can retry for it (a spawn that never produced a
    container, an execution the backend killed or lost) and never retries
    one it can already see is deterministic (a task object that cannot be
    rehydrated will not rehydrate on the second reading either — retrying
    that spends the whole budget to arrive at the same failure, later).
    """
    await registry.task_fail_aio(build_id, task, reason)
    summary.failed_recorded += 1
    if not retryable:
        return

    if not _retry_allowed(attempts_spent, config.max_attempts):
        # Three different "no retry" situations, and conflating them is how
        # an operator ends up debugging the wrong one.
        if attempts_spent is None:
            # The registry cannot count attempts, so no budget can bound a
            # retry loop and none is attempted. Worth a line every time: it
            # is the only signal that a *configured* retry policy is inert.
            logger.warning(
                f"Task {task.id} of build {build_id} failed and will not be "
                "retried: this registry does not report per-round attempt "
                "counts, so TickConfig.max_attempts "
                f"({config.max_attempts}) cannot be enforced and retrying "
                "would be unbounded. Upgrade stardag-api to enable "
                f"scheduler retries. Failure: {reason}"
            )
        elif config.max_attempts <= 1:
            # Retries switched off deliberately. Not news; the recorded
            # failure is the event, and this line just says why nothing
            # followed it.
            logger.info(
                f"Task {task.id} of build {build_id} failed and will not be "
                "retried (TickConfig.max_attempts="
                f"{config.max_attempts} allows one attempt per task)."
            )
        else:
            summary.retry_exhausted += 1
            logger.error(
                f"Task {task.id} of build {build_id} failed and will NOT be "
                "retried: its attempt budget for this build round is spent "
                f"({_attempts_phrase(attempts_spent, config.max_attempts)}). "
                "To run it again, RE-TRIGGER THIS BUILD — "
                f"build_trigger(..., build_id={build_id}, reactive=True) — "
                "which starts a new round and resets every task's attempt "
                "count to zero. Retrying the task on its own does NOT reset "
                "the count: a bare retry (the UI's Retry, `stardag tasks "
                "retry`, the retry API route) makes the task pending again "
                "but leaves the budget spent, so this scheduler would "
                "decline to start it. If the task needs more attempts per "
                "round, re-trigger with a raised budget, e.g. "
                'tick_kwargs={"max_attempts": 4}. Last failure: '
                f"{reason}"
            )
        return

    try:
        await registry.task_retry_aio(build_id, task)
    except Exception as e:
        # Swallowed rather than raised: the failure is already recorded, so
        # the task is in a consistent terminal state and the build fails
        # the way it would have before this existed. Crashing the tick here
        # would trade a lost retry for a lost pass — and the pass's other
        # spawns with it.
        logger.error(
            f"Task {task.id} of build {build_id} failed and could not be "
            f"reset to pending for another attempt: {e}. It stays failed."
        )
        return
    summary.retried += 1
    logger.warning(
        f"Task {task.id} of build {build_id} failed and has been reset to "
        "pending for another attempt "
        f"({_attempts_phrase(attempts_spent or 0, config.max_attempts)}): "
        f"{reason}"
    )
