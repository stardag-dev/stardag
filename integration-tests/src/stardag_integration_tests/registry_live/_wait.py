"""Polling helpers, with failure messages that say what was actually seen.

A scenario that times out is the normal failure mode here, and the default
version of it -- ``assert time.time() < deadline`` -- says nothing about
why. Every wait below reports the build's status, its tick trail and how
long it waited, because that is the difference between "reactive scheduling
regressed" and "the Modal worker never started".
"""

from __future__ import annotations

import time
from typing import Any, Callable
from uuid import UUID

TERMINAL = ("completed", "failed", "cancelled")


def tick_summaries(build_id: UUID, limit: int = 50) -> list[dict[str, Any]]:
    """The build's retained tick summaries, oldest first.

    The registry returns newest first; reversed here because these read as
    a story and a story runs forwards.
    """
    from stardag.registry import registry_provider

    records = registry_provider.get().build_list_tick_summaries(build_id, limit=limit)
    return [record.summary for record in reversed(records)]


def find_task(task_id: str, *, task_name: str):
    """The registry's row for one task, by its stardag task id.

    ``task_list`` filters by name, not by id, so the name narrows the page
    and the id picks the row out of it. Worth the round trip because the
    row carries ``latest_status_build_id`` -- the answer to "which build
    holds, or held, this task's execution claim", which no tick summary
    can give.
    """
    from stardag.registry import registry_provider

    page = registry_provider.get().task_list(page_size=100, task_name=task_name)
    for task in page.tasks:
        if task.task_id == task_id:
            return task
    raise AssertionError(
        f"No {task_name!r} task with id {task_id!r} in the registry "
        f"(saw {[t.task_id for t in page.tasks]})."
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
) -> str:
    """Block until the build reaches a terminal status; return it."""
    return wait_until(
        lambda: _terminal_status(build_id),
        build_id=build_id,
        timeout=timeout,
        poll_interval=poll_interval,
        what="a terminal status",
    )


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
