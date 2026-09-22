"""Polling helpers, with failure messages that say what was actually seen.

A scenario that times out is the normal failure mode here, and the default
version of it -- ``assert time.time() < deadline`` -- says nothing about
why. Every wait below reports the build's status, its tick trail and how
long it waited, because that is the difference between "reactive scheduling
regressed" and "the Modal worker never started".
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

TERMINAL = ("completed", "failed", "cancelled")

TASK_PAGE_SIZE = 100
# A provisioned stack is meant to be kept and re-run against, so tasks of
# one name accumulate across runs and the one being looked for is not
# necessarily on the first page. Bounded so a lookup for a task that does
# not exist fails in seconds rather than paging a large registry forever.
MAX_TASK_PAGES = 20


# The server retains this many summaries per build and prunes older ones on
# insert. Asking for exactly that many means a full page is indistinguishable
# from a truncated one, which matters because assertions here read
# `summaries[0]` as "the first tick" -- past the cap it silently becomes "the
# oldest one still kept", and a `lingered_out` assertion quietly changes
# meaning without failing. `assert_trail_complete` below turns that into a
# failure instead.
SERVER_TRAIL_RETENTION = 50


def tick_summaries(
    build_id: UUID, limit: int = SERVER_TRAIL_RETENTION
) -> list[dict[str, Any]]:
    """The build's retained tick summaries, oldest first.

    The registry returns newest first; reversed here because these read as
    a story and a story runs forwards.
    """
    from stardag.registry import registry_provider

    records = registry_provider.get().build_list_tick_summaries(build_id, limit=limit)
    return [record.summary for record in reversed(records)]


def find_task(task_id: str, *, task_name: str):
    """The registry's row for one task, by its stardag task id.

    ``task_list`` filters by name, not by id, so the name narrows the
    search and the id picks the row out of it. Worth the round trips
    because the row carries ``latest_status_build_id`` -- the answer to
    "which build holds, or held, this task's execution claim", which no
    tick summary can give.

    Paging is by offset, so a row can in principle be skipped if another
    scenario inserts tasks of the same name *while* this walks the pages.
    Reachable only on a long-lived stack that has accumulated more than one
    page of them -- which is the mode the docs recommend -- so the failure
    is a spurious "no such task" rather than a wrong answer, and re-running
    clears it.
    """
    from stardag.registry import registry_provider

    registry = registry_provider.get()
    seen = 0
    for page_number in range(1, MAX_TASK_PAGES + 1):
        page = registry.task_list(
            page_size=TASK_PAGE_SIZE, page=page_number, task_name=task_name
        )
        for task in page.tasks:
            if task.task_id == task_id:
                return task
        seen += len(page.tasks)
        if len(page.tasks) < TASK_PAGE_SIZE:
            break
    raise AssertionError(
        f"No {task_name!r} task with id {task_id!r} among {seen} "
        f"{task_name!r} tasks in the registry."
    )


def assert_trail_complete(build_id: UUID, summaries: list[dict[str, Any]]) -> None:
    """Refuse to reason about a trail that may have been pruned.

    Call before treating ``summaries[0]`` as the build's *first* tick.
    """
    if len(summaries) >= SERVER_TRAIL_RETENTION:
        raise AssertionError(
            f"This build has at least {SERVER_TRAIL_RETENTION} tick "
            "summaries, which is the server's retention cap -- so the "
            "oldest ones have been pruned and the first entry is no longer "
            "the build's first tick. Any assertion about how the build "
            "started is unsound here.\n" + describe(build_id)
        )


def describe(build_id: UUID) -> str:
    """A one-block account of a build, for putting in an assertion message."""
    from stardag.registry import registry_provider

    registry = registry_provider.get()
    try:
        info = registry.build_get(build_id)
        status = info.status
        reactive = info.reactive_app_name
    except Exception as error:  # pragma: no cover - diagnostics only
        return f"build {build_id}: could not be read back ({error!r})"

    lines = [
        f"build {build_id}: status={status!r} reactive_app_name={reactive!r}",
    ]
    summaries = tick_summaries(build_id)
    if not summaries:
        lines.append(
            "  no tick summaries. No scheduler tick ever reported, so either "
            "the bootstrap never ran or every tick died before reporting."
        )
    for index, summary in enumerate(summaries, start=1):
        rendered = " ".join(
            f"{key}={value!r}"
            for key, value in sorted(summary.items())
            # Zero counters are dropped: a tick reports every counter it
            # knows, and the interesting ones are the non-zero ones.
            if value not in (0, None, "")
        )
        lines.append(f"  tick {index}: {rendered}")
    return "\n".join(lines)


# Builds whose terminal tick never reported. Read by the scenarios' failure
# messages, so a counter that came out low says why it might have.
_TRUNCATED_TRAILS: set[UUID] = set()


def trail_may_be_truncated(build_id: UUID) -> bool:
    """Whether this build's terminal tick failed to report in time."""
    return build_id in _TRUNCATED_TRAILS


def assert_dormancy_is_forced(
    *, work_seconds: float, linger_seconds: float, what: str
) -> None:
    """The wake-up precondition, taken from the constants, not from a report.

    Every wake-up scenario needs its build to be *dormant* when the thing
    it is waiting for happens -- otherwise the tick was still resident,
    saw it on its own poll, and the wake-up path was never exercised. The
    scenario then passes while testing something weaker, which is the
    failure the timing rules here exist to prevent.

    It used to be checked by reading the trail: the first summary says
    ``lingered_out``, and there is more than one summary. Both are
    assertions that a *report* exists, so a preempted tick made a healthy
    run look like a regression -- and, worse, made a real regression look
    like that preemption (STA-87, STA-89).

    This is the same precondition established from the scenario's own
    constants instead, and it is strictly stronger. A tick lingers
    ``linger_seconds`` once it has nothing left to do, and it cannot have
    started the work later than it spawned it; so if the work outlasts the
    linger, the tick *must* have gone before the work finished. That is
    arithmetic, not observation: no container is slow enough to break it,
    and slowness pushes it the safe way, since a late start delays the
    work and not the linger.

    The four scenarios using this sit at four to five times the linger.
    Only the inequality is enforced -- a margin would be a policy invented
    here -- but the ratio is reported so that an edit narrowing it is
    visible in the failure.
    """
    if work_seconds <= linger_seconds:
        raise AssertionError(
            f"{what}: the work ({work_seconds:g}s) does not outlast the "
            f"linger ({linger_seconds:g}s), so the build is not guaranteed "
            f"to be dormant when the wake-up arrives and the scenario may "
            f"be testing a resident tick noticing on its own poll. Raise "
            f"the work or lower the linger."
        )


INCONCLUSIVE_MARKER_NAME = "inconclusive-trails"


def require_complete_trail(build_id: UUID, *, what: str) -> None:
    """Refuse to answer a counting question the trail cannot answer.

    For the few counters with no durable record -- a tick self-healing a
    completion, a concurrency-limit denial, a tick finding the lease
    held. None of those writes a row, so the trail is the only witness,
    and a trail missing its terminal entry is short by that tick's
    contribution.

    Both directions are unsound, which is why this skips rather than
    relaxes. An exact count fails for a reason that is not the code's; a
    `<=` passes because the evidence is gone. A skip is the honest third
    answer, and it is counted, so a tier skipping its way to green is
    visible rather than reassuring.

    Spawn counts do *not* come here: a granted claim is a row, so they
    are asserted against the event log instead (`_events`).
    """
    if not trail_may_be_truncated(build_id):
        return
    from ._diagnostics import DIAGNOSTICS_DIR_ENV

    directory = os.environ.get(DIAGNOSTICS_DIR_ENV, "").strip()
    if directory:
        try:
            target = Path(directory)
            target.mkdir(parents=True, exist_ok=True)
            with (target / INCONCLUSIVE_MARKER_NAME).open("a") as marker:
                marker.write(str(build_id) + " -- " + what + "\n")
        except OSError as error:  # pragma: no cover - diagnostics only
            print(
                "Could not record the inconclusive trail: " + repr(error),
                file=sys.stderr,
            )
    pytest.skip(
        "Inconclusive: "
        + what
        + " is read off build "
        + str(build_id)
        + "'s tick trail, and the tick that ended that build never reported "
        "its summary, so the trail is short by an entry and the count is "
        "short by its contribution. Not a pass and not a failure; the "
        "durable assertions above this one ran."
    )


def assert_remaining_work_outlasts_linger(
    task_id: UUID, *, total_seconds: float, linger_seconds: float, what: str
) -> None:
    """The dormancy precondition when another build started the work already.

    Measured, because a constant cannot answer it. The task is running
    before this build is triggered, so what decides whether this build
    goes dormant is the work *remaining* when its tick starts -- and a
    slow bootstrap eats that margin while the constants still compare
    favourably. Read the task's start from the registry, subtract, and
    require the remainder to outlast the linger.

    Not a clock race: the start time is a recorded fact and the
    comparison is made once, at the moment the waiting build is
    triggered. The margin it reports is the real one.
    """
    from stardag.registry import registry_provider

    started_at = registry_provider.get().task_get_metadata(task_id).started_at
    if started_at is None:
        raise AssertionError(
            what
            + ": the registry has no start time for task "
            + str(task_id)
            + ", so the remaining window cannot be established."
        )
    elapsed = (
        datetime.now(timezone.utc) - started_at.astimezone(timezone.utc)
    ).total_seconds()
    remaining = total_seconds - elapsed
    if remaining <= linger_seconds:
        raise AssertionError(
            f"{what}: the task started {elapsed:.0f}s ago and runs for {total_seconds:g}s, so only "
            f"{remaining:.0f}s remain -- not more than this build's linger ({linger_seconds:g}s). "
            "The build is not guaranteed to be dormant when the task "
            "finishes, so it may see the completion on its own poll and the "
            "wake-up path would not be exercised. A slow bootstrap eats this "
            "margin; raise the task's duration."
        )


def wait_for_terminal(
    build_id: UUID,
    *,
    timeout: float,
    poll_interval: float = 5.0,
    trail_timeout: float = 90.0,
) -> str:
    """Block until the build is terminal, then give its last tick time to report.

    **The first half is an assertion; the second is a courtesy.** A build
    that never reaches a terminal status is a real failure and still fails
    here. A terminal build whose final tick did not report is not: the
    build is done, the work is recorded on the build and task rows, and
    the only thing missing is a tick's account of itself.

    The wait exists for a real race and stays. A tick writes the build's
    terminal status *first* and reports its summary *after*, so a caller
    that sees "completed" and immediately reads the trail gets a trail
    missing its final entry -- which silently subtracts a tick's worth of
    counters from every assertion below it. That was found the honest way:
    it passed three local runs and failed on the first CI run.

    What changed (STA-89) is what happens when the wait runs out. It used
    to raise, which made **every** scenario that waits for a build fail if
    the tick that ended it was preempted between those two calls -- a
    ninety-second wait and then a message reading like a scheduling
    defect, attributed to whichever scenario was unlucky. 15 of the 18
    scenarios call this. A preempted reporter is not evidence about the
    code under test, so it now warns and returns, and the trail is
    recorded as possibly truncated.

    **What that costs.** The trail may now be short by its last entry, so
    nothing may be *counted* off it without saying what a missing entry
    would do. Counts that have a durable substitute take it -- spawns are
    read from the event log (``_events.granted_claim_starts``). The few
    that do not go through ``require_complete_trail``, which declines to
    answer rather than guessing.

    """
    status = wait_until(
        lambda: _terminal_status(build_id),
        build_id=build_id,
        timeout=timeout,
        poll_interval=poll_interval,
        what="a terminal status",
    )
    deadline = time.monotonic() + trail_timeout
    while time.monotonic() < deadline:
        if any(s.get("terminal_status") for s in tick_summaries(build_id)):
            return status
        time.sleep(2.0)

    _TRUNCATED_TRAILS.add(build_id)
    print(
        f"[harness] build {build_id} is {status} but the tick that ended it "
        f"did not report within {trail_timeout:.0f}s. Either it died between "
        f"writing the status and reporting -- a preemption does exactly that "
        f"-- or something that is not a tick ended the build. Not a failure: "
        f"the build and task rows carry the result. The tick trail below may "
        f"be missing its last entry, so counts read from it are lower bounds.",
        file=sys.stderr,
    )
    return status


def wait_until(
    condition: Callable[[], Any],
    *,
    build_id: UUID,
    timeout: float,
    poll_interval: float = 5.0,
    what: str,
) -> Any:
    """Poll ``condition`` until it returns something truthy, or fail loudly."""
    deadline = time.monotonic() + timeout
    started = time.monotonic()
    while time.monotonic() < deadline:
        result = condition()
        if result:
            return result
        time.sleep(poll_interval)
    waited = time.monotonic() - started
    raise AssertionError(
        f"Waited {waited:.0f}s for {what} and it did not happen.\n{describe(build_id)}"
    )


def _terminal_status(build_id: UUID) -> str | None:
    from stardag.registry import registry_provider

    status = registry_provider.get().build_get(build_id).status
    return status if status in TERMINAL else None


def task_status(task_id: UUID) -> str | None:
    """One task's current status, or None if the registry has no row yet.

    A single request against the task's own row, which is what makes it
    cheap enough to poll. ``find_task`` answers a richer question and pages
    through every task of a name to do it -- far too expensive to sit in a
    loop.

    None rather than an exception for the unregistered case, because it is
    the *normal* first answer: a scenario starts polling as soon as it has
    triggered a build, and the plan reaches the registry a moment later.
    """
    from stardag.exceptions import NotFoundError
    from stardag.registry import registry_provider

    try:
        return registry_provider.get().task_get_metadata(task_id).status
    except NotFoundError:
        return None


def wait_for_task_status(
    task_id: UUID,
    *,
    expected: str | Sequence[str],
    build_id: UUID,
    timeout: float,
    poll_interval: float = 3.0,
) -> str:
    """Block until ``task_id`` reaches one of ``expected``. Returns which.

    **This is what a scenario should wait on, rather than sleeping.** Every
    cross-build scenario here is built on an ordering -- a second build has
    to register while a shared task is still RUNNING, a tick has to be
    driven once a task is SUSPENDED -- and the ordering is the scenario. A
    fixed sleep sized for that window is wrong in both directions: it is
    longer than needed on a warm run, which is pure wall clock across a
    tier of these, and too short whenever Modal takes an unusual time to
    start a container, which silently converts the scenario into a
    different and much weaker one that still passes.

    Waiting for the state itself removes both. The timeout is then a real
    timeout -- "this never happened" -- rather than a guess at how long it
    ought to take.
    """
    wanted = (expected,) if isinstance(expected, str) else tuple(expected)

    def reached() -> str | None:
        status = task_status(task_id)
        return status if status in wanted else None

    return wait_until(
        reached,
        build_id=build_id,
        timeout=timeout,
        poll_interval=poll_interval,
        what=f"task {task_id} to reach {' or '.join(wanted)}",
    )


def build_status(build_id: UUID) -> str | None:
    """One build's current status, as the registry currently reports it.

    ``None`` where the registry has not derived one yet, which a caller
    comparing against a named status handles for free.
    """
    from stardag.registry import registry_provider

    return registry_provider.get().build_get(build_id).status
