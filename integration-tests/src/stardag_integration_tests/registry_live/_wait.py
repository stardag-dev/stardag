"""Polling helpers, with failure messages that say what was actually seen.

A scenario that times out is the normal failure mode here, and the default
version of it -- ``assert time.time() < deadline`` -- says nothing about
why. Every wait below reports the build's status, its tick trail and how
long it waited, because that is the difference between "reactive scheduling
regressed" and "the Modal worker never started".
"""

from __future__ import annotations

import time
from typing import Any, Callable, Sequence
from uuid import UUID

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


def wait_for_terminal(
    build_id: UUID,
    *,
    timeout: float,
    poll_interval: float = 5.0,
    trail_timeout: float = 90.0,
) -> str:
    """Block until the build is terminal *and* its last tick has reported.

    Both halves are needed, and the second is not obvious. A tick that
    drives a build to completion writes the build's terminal status first
    and reports its own summary afterwards -- two separate calls, in that
    order. So a caller that sees "completed" and immediately reads the tick
    trail can get a trail that is missing its final entry.

    Every assertion in this tier reads that trail, so the window is not
    academic: it silently subtracts a tick's worth of counters. It passed
    three local runs and failed on the first CI run, which is exactly the
    signature of a race whose width depends on latency and load.

    Waiting for a summary carrying ``terminal_status`` is the precise form
    of "the tick that ended this build has finished talking".
    """
    status = wait_until(
        lambda: _terminal_status(build_id),
        build_id=build_id,
        timeout=timeout,
        poll_interval=poll_interval,
        what="a terminal status",
    )
    wait_until(
        lambda: any(s.get("terminal_status") for s in tick_summaries(build_id)),
        build_id=build_id,
        timeout=trail_timeout,
        poll_interval=2.0,
        what=(
            f"the tick that ended this build ({status}) to report its "
            "summary. The build is terminal, so either that tick died "
            "between writing the status and reporting, or the build was "
            "ended by something that is not a tick"
        ),
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
    from stardag.registry import registry_provider
    from stardag.exceptions import NotFoundError

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
