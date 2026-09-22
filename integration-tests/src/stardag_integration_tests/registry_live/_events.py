"""The task event log, which is the only place some questions are answered.

Two scenarios here turn on whether a *particular build* reset a task it did
not own. No status column can answer that. ``latest_status`` and
``latest_status_build_id`` are one row per task, overwritten by whoever
wrote last, so a build that resets a task and then watches someone else
complete it leaves no trace in them at all -- the reset happened, and the
columns show the completion. The append-only event log is where the reset
is still visible, attributed to the build that made it.

The registry client has no method for this endpoint, so these talk to the
API directly with the deployment's own API key -- the same credential the
workers use, and the same shape as the harness's other direct calls.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from ._harness import Deployment

# A reset is recorded as this. It is what a build does to a failed,
# cancelled or skipped task to make it its own to run again -- correct
# within a build, and the thing a build must not do to a task another build
# is legitimately mid-flight on.
RESET_EVENT = "task_retried"


def task_events(
    deployment: Deployment, task_id: Any, *, missing_ok: bool = False
) -> list[dict[str, Any]]:
    """Every event recorded against one task, across all builds, oldest first.

    The API answers newest first; reversed here because these read as a
    story and a story runs forwards.

    ``missing_ok`` decides what an unregistered task means, and the default
    is the strict reading on purpose. At assertion time a task the registry
    has never heard of is a real failure and must not read as "no resets
    happened" -- an empty list is the answer this function's callers treat
    as *proof of correct behaviour*. Only a caller that is polling *for*
    the registration should pass True, where 404 is the expected first
    answer rather than a problem; that mirrors the None-for-missing
    contract ``_wait.task_status`` documents for the same situation.
    """
    with httpx.Client(timeout=60.0) as client:
        response = client.get(
            f"{deployment.api_url.rstrip('/')}/api/v1/tasks/{task_id}/events",
            headers={"X-API-Key": deployment.api_key},
        )
        if missing_ok and response.status_code == 404:
            return []
        response.raise_for_status()
        return list(reversed(response.json()))


def events_by(events: Iterable[dict[str, Any]], build_id: Any) -> list[dict[str, Any]]:
    """The subset of ``events`` this build wrote."""
    wanted = str(build_id)
    return [event for event in events if str(event.get("build_id")) == wanted]


def resets_by(events: Iterable[dict[str, Any]], build_id: Any) -> list[dict[str, Any]]:
    """The resets this build performed on the task -- normally none.

    The decisive observable for both cross-build blocker scenarios: a
    build that waited correctly has an empty list here, and a build that
    reset a task another build was mid-flight on does not.
    """
    return [
        event
        for event in events_by(events, build_id)
        if event.get("event_type") == RESET_EVENT
    ]


def describe_events(events: Iterable[dict[str, Any]], **labels: Any) -> str:
    """The event log as lines, for putting in an assertion message.

    ``labels`` names the builds -- ``describe_events(events, A=id_a, B=id_b)``
    prints ``A`` and ``B`` rather than two UUIDs the reader has to match up
    by eye, which is most of the work of reading one of these failures.
    """
    named = {str(value): name for name, value in labels.items()}
    lines = []
    for event in events:
        build = str(event.get("build_id"))
        lines.append(
            f"  {str(event.get('created_at'))[11:19]} "
            f"{event.get('event_type')} (build {named.get(build, build)})"
        )
    return "\n".join(lines) or "  (no events recorded against this task)"


def wait_until_registered(
    deployment: Deployment,
    *,
    task_id: Any,
    build_id: Any,
    timeout: float = 300.0,
) -> None:
    """Block until ``build_id`` has an event of its own on this task.

    Build membership comes from the event log, not from the task's status
    columns: a task with no task-level event for a build is not in that
    build, however its columns read. A seeded build showing an empty task
    list and an empty DAG is the correct rendering of that, and has been
    mistaken for a bug.

    The cross-build scenarios need this because "B has been triggered" is
    not the precondition any of them actually require -- "B has registered
    against the shared task, and the task was still in the right status
    when it did" is.
    """
    from ._wait import wait_until

    wait_until(
        lambda: bool(
            events_by(task_events(deployment, task_id, missing_ok=True), build_id)
        ),
        build_id=build_id,
        timeout=timeout,
        poll_interval=3.0,
        what=f"build {build_id} to register against task {task_id}",
    )


def first_event_at(events: Iterable[dict[str, Any]], build_id: Any) -> str | None:
    """When this build first touched the task, by the registry's clock.

    Server-side timestamps on both ends is the point: a margin computed
    from when a *client poll happened to notice* a transition is
    systematically optimistic by up to one poll interval plus a round trip,
    and a diagnostic that flatters itself is worse than none -- it reads
    healthy right up to the moment the thing it is warning about starts
    failing.
    """
    mine = events_by(events, build_id)
    return str(mine[0]["created_at"]) if mine else None


# A report the registry kept as audit but refused to apply. Excluded,
# because a refused claim-start changed nothing and did not license a
# spawn (`services.status.REPORT_APPLIED_KEY`).
REPORT_APPLIED_KEY = "report_applied"


def spawned_executions(deployment: Deployment, build_id: Any) -> dict[str, int]:
    """Executions this build actually submitted, per task id.

    **The durable form of a tick's ``spawned`` counter.** A tick records a
    second ``task_started`` once ``submit_detached`` has returned, carrying
    the backend's reference for the call it just created. That row is the
    registry's evidence that a container was submitted, it is written
    before the container reports anything, and it survives the tick dying
    on the way home.

    **Counted by distinct ``executor_ref``, and both halves of that matter.**

    *Distinct*, because the registry client retries a POST whose response
    was lost and the API deliberately appends a second row for it -- the
    code that writes it warns any new reader that counts rows. Two rows
    naming one call are one execution.

    *Ref-bearing*, because the granted **claim** is not a spawn. The claim
    is taken first and the submission can still fail: ``submit_detached``
    raises, the tick records a task failure, and ``summary.spawned`` is
    never incremented -- but the claim row exists. Counting claims would
    read that as an execution that never happened and fail a scenario for
    a submission error. The ref only exists once there is a call to name.

    Refused reports are excluded: a start the registry kept as audit but
    did not apply changed nothing.
    """
    refs: dict[str, set[str]] = {}
    for event in _build_events(deployment, build_id):
        metadata = event.get("event_metadata") or {}
        ref = metadata.get("executor_ref")
        if (
            event.get("event_type") != "task_started"
            or ref is None
            or metadata.get(REPORT_APPLIED_KEY) is False
        ):
            continue
        refs.setdefault(str(event.get("task_id")), set()).add(str(ref))
    return {task_id: len(seen) for task_id, seen in refs.items()}


def earliest_start_and_server_now(
    deployment: Deployment, task_id: Any
) -> tuple[datetime, datetime] | None:
    """When this task first started, and what the registry's clock says now.

    Both ends from the server, and both chosen to err the same way.

    The *earliest* ``task_started`` rather than the task row's
    ``started_at``: the row holds the latest start, and the reactive
    engine writes a second one after ``submit_detached``, so reading it
    understates how long the task has been running -- which overstates
    the work remaining, in the direction that lets a precondition pass
    when it should not.

    ``now`` from the response's ``Date`` header rather than the runner's
    clock, so the subtraction is between two readings of one clock. A
    local ``now`` compared against a server timestamp is off by whatever
    the skew is, in an unknown direction.

    ``None`` when the task has no start recorded yet.
    """
    with httpx.Client(timeout=60.0) as client:
        response = client.get(
            f"{deployment.api_url.rstrip('/')}/api/v1/tasks/{task_id}/events",
            headers={"X-API-Key": deployment.api_key},
        )
        response.raise_for_status()
    starts = [
        event["created_at"]
        for event in response.json()
        if event.get("event_type") == "task_started" and event.get("created_at")
    ]
    if not starts:
        return None
    served_at = response.headers.get("date")
    if not served_at:
        return None
    return (
        min(datetime.fromisoformat(value) for value in starts),
        parsedate_to_datetime(served_at),
    )


def _build_events(deployment: Deployment, build_id: Any) -> list[dict[str, Any]]:
    with httpx.Client(timeout=60.0) as client:
        response = client.get(
            f"{deployment.api_url.rstrip('/')}/api/v1/builds/{build_id}/events",
            headers={"X-API-Key": deployment.api_key},
        )
        response.raise_for_status()
    return list(response.json())


def describe_claims(counts: dict[str, int]) -> str:
    """The per-task breakdown, for an assertion message."""
    if not counts:
        return "  no granted claims recorded under this build"
    return "\n".join(
        f"  task {task_id}: {count} granted claim(s)"
        for task_id, count in sorted(counts.items())
    )
