"""The task event log and the execution ledger: the durable records.

Two scenarios here turn on whether a *particular build* reset a task it did
not own. No status column can answer that. A ``task`` row is one row per
completion, overwritten by whoever wrote last, so a build that resets a
task and then watches someone else complete it leaves no trace in it at all
-- the reset happened, and the row shows the completion. The append-only
event log is where the reset is still visible, attributed to the build (and
plan, and execution) that made it.

The other record is the **execution ledger** (``GET
/builds/{id}/executions?include_ended=true``): one row per execution a
claiming start granted, carrying the backend's ref once the spawn
succeeded. It is what "how many containers did this build submit" is
counted from.

The registry client has no method for the event log, so these talk to the
API directly with the deployment's own API key -- the same credential the
workers use, and the same shape as the harness's other direct calls.
"""

from __future__ import annotations

import collections
import logging
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
from stardag.registry._api_http import (
    _MAX_TRANSIENT_RETRIES,
    _TRANSIENT_EXCEPTIONS,
    _transient_cause,
    _transient_delay,
)

from ._harness import Deployment

logger = logging.getLogger(__name__)

# The harness's own reads retried the way the SDK retries its calls, and
# counted beside the SDK's tally (``retry_counts``): these are calls to the
# same registry through the same proxy, and they meet the same lost
# answers. A read is safe to send twice by construction.
_retry_counts: collections.Counter[str] = collections.Counter()


def retry_counts() -> dict[str, int]:
    """How many of the harness's own reads this process has retried, by cause."""
    return dict(_retry_counts)


# A reset is recorded as this. It is what a build does to a failed,
# cancelled or skipped task to make it its own to run again -- correct
# within a build, and the thing a build must not do to a task another build
# is legitimately mid-flight on.
RESET_EVENT = "task_retried"


def task_events(
    deployment: Deployment, task_id: Any, *, missing_ok: bool = False
) -> list[dict[str, Any]]:
    """Every event recorded against one task, across all builds, oldest first.

    ``missing_ok`` decides what an unregistered task means, and the default
    is the strict reading on purpose. At assertion time a task the registry
    has never heard of is a real failure and must not read as "no resets
    happened" -- an empty list is the answer this function's callers treat
    as *proof of correct behaviour*. Only a caller that is polling *for*
    the registration should pass True, where 404 is the expected first
    answer rather than a problem; that mirrors the None-for-missing
    contract ``_wait.task_status`` documents for the same situation.
    """
    response = _get(deployment, f"tasks/{task_id}/events", raise_for_status=False)
    if missing_ok and response.status_code == 404:
        return []
    response.raise_for_status()
    return list(response.json()["events"])


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


def spawned_executions(deployment: Deployment, build_id: Any) -> dict[str, int]:
    """Executions this build actually submitted, per task id.

    **The durable form of a tick's ``spawned`` counter**, read off the
    execution ledger. A claiming start mints one execution row; the tick's
    non-claiming start after ``submit_detached`` has returned writes the
    backend's ref onto it. That row is the registry's evidence that a
    container was submitted, it is written before the container reports
    anything, and it survives the tick dying on the way home.

    **Ref-bearing only**, because the granted **claim** is not a spawn. The
    claim is taken first and the submission can still fail:
    ``submit_detached`` raises, the tick reports a task failure, and
    ``summary.spawned`` is never incremented -- but the execution row
    exists. Counting executions would read that as a container that never
    ran. The ref only exists once there is a call to name.

    One row per execution, so a retried report cannot double-count (v1
    appended an event per retry and had to count distinct refs).
    """
    counts: dict[str, int] = {}
    for execution in ledger(deployment, build_id):
        if execution.get("executor_ref") is None:
            continue
        task_id = str(execution["task_id"])
        counts[task_id] = counts.get(task_id, 0) + 1
    return counts


def ledger(deployment: Deployment, build_id: Any) -> list[dict[str, Any]]:
    """Every execution the build's plans granted, ended or not, oldest first."""
    response = _get(
        deployment, f"builds/{build_id}/executions", params={"include_ended": "true"}
    )
    return list(response.json()["executions"])


def executions_of(
    deployment: Deployment, task_id: Any, *build_ids: Any
) -> list[dict[str, Any]]:
    """Every execution of one task across the given builds' ledgers.

    Each row is tagged ``"build_id"`` with the build whose ledger listed it,
    so a scenario can say *whose* execution ran a shared completion -- the
    question every cross-build scenario turns on.
    """
    wanted = str(task_id)
    rows: list[dict[str, Any]] = []
    for build_id in build_ids:
        for execution in ledger(deployment, build_id):
            if str(execution.get("task_id")) == wanted:
                rows.append({**execution, "build_id": str(build_id)})
    return rows


def spawned_of(
    deployment: Deployment, task_id: Any, *build_ids: Any
) -> list[dict[str, Any]]:
    """``executions_of``, ref-bearing only: the containers actually submitted.

    See ``spawned_executions`` for why a claim without a ref is not a spawn.
    """
    return [
        e
        for e in executions_of(deployment, task_id, *build_ids)
        if e.get("executor_ref") is not None
    ]


def describe_ledger(rows: Iterable[dict[str, Any]], **labels: Any) -> str:
    """Ledger rows as lines, for an assertion message; ``labels`` as above."""
    named = {str(value): name for name, value in labels.items()}
    lines = []
    for row in rows:
        build = str(row.get("build_id"))
        lines.append(
            f"  {str(row.get('task_id'))[:8]} exec {str(row.get('id'))[:8]} "
            f"(build {named.get(build, build)}, plan {str(row.get('plan_id'))[:8]}) "
            f"ref={row.get('executor_ref')!r} claim={row.get('claim_outcome')!r} "
            f"outcome={row.get('outcome')!r}"
        )
    return "\n".join(lines) or "  (no executions)"


@dataclass(frozen=True)
class CurrentExecution:
    """The execution a task's claim names now, and whose it is.

    ``build_id`` answers "which build holds, or held, this task's claim" --
    the question v1 answered with ``latest_status_build_id``. v2 keeps no
    build on the task row; the claim names an execution, whose plan belongs
    to a build, and the event log attributes the execution's start to it.
    """

    execution_id: str
    build_id: str | None
    executor_ref: str | None
    status: str


def current_execution(deployment: Deployment, task_id: Any) -> CurrentExecution | None:
    """The task's current execution, or ``None`` if its claim names none."""
    task = _get(deployment, f"tasks/{task_id}").json()
    execution_id = task.get("execution_id")
    if execution_id is None:
        return None
    build_id = next(
        (
            str(event["build_id"])
            for event in task_events(deployment, task_id)
            if event.get("execution_id") == execution_id and event.get("build_id")
        ),
        None,
    )
    executor_ref = None
    if build_id is not None:
        executor_ref = next(
            (
                e.get("executor_ref")
                for e in ledger(deployment, build_id)
                if e["id"] == execution_id
            ),
            None,
        )
    return CurrentExecution(
        execution_id=str(execution_id),
        build_id=build_id,
        executor_ref=executor_ref,
        status=str(task["status"]),
    )


# The registry answers in well under a second; a read that has waited this
# long is lost, not slow, and is better sent again than waited on.
_READ_TIMEOUT_SECONDS = 10.0


def _get(
    deployment: Deployment,
    path: str,
    *,
    params: dict[str, str] | None = None,
    raise_for_status: bool = True,
) -> httpx.Response:
    """One authenticated GET against the registry's SDK routes (``/api/v2``).

    An exchange that got no complete answer -- a timeout, a dropped
    connection, a body cut short, a proxy error -- is sent again, with the
    SDK's bounds and backoff; an answer the registry wrote is returned as
    it is.
    """
    url = f"{deployment.api_url.rstrip('/')}/api/v2/{path.lstrip('/')}"
    retries = 0
    started = time.monotonic()

    def may_retry() -> bool:
        # The SDK's bounds: a number of retries, and no retry started once
        # the read has taken two timeouts (a timed-out attempt has taken one).
        return (
            retries < _MAX_TRANSIENT_RETRIES
            and time.monotonic() - started < 2 * _READ_TIMEOUT_SECONDS
        )

    with httpx.Client(timeout=_READ_TIMEOUT_SECONDS) as client:
        while True:
            try:
                response = client.get(
                    url, headers={"X-API-Key": deployment.api_key}, params=params
                )
            except _TRANSIENT_EXCEPTIONS as error:
                if not may_retry():
                    raise
                cause, detail = type(error).__name__, str(error)
            else:
                cause = _transient_cause(response)
                if cause is None or not may_retry():
                    break
                detail = response.text[:120]
            retries += 1
            _retry_counts[cause] += 1
            delay = _transient_delay(retries)
            logger.warning(
                "Harness GET %s got no complete answer (%s: %s); retrying in "
                "%.1fs (retry %d of %d).",
                path,
                cause,
                detail,
                delay,
                retries,
                _MAX_TRANSIENT_RETRIES,
            )
            time.sleep(delay)
    if raise_for_status:
        response.raise_for_status()
    return response


def earliest_start_and_server_now(
    deployment: Deployment, task_id: Any
) -> tuple[datetime, datetime] | None:
    """When this task first started, and what the registry's clock says now.

    Both ends from the server, and both chosen to err the same way.

    The *earliest* applied ``task_started`` rather than the task row's
    ``started_at``: the row holds the latest start, and the reactive
    engine writes a second one after ``submit_detached``, so reading it
    understates how long the task has been running -- which overstates
    the work remaining, in the direction that lets a precondition pass
    when it should not.

    ``now`` from the response's ``Date`` header rather than the runner's
    clock, so the subtraction is between two readings of one clock. A
    local ``now`` against a server timestamp is off by whatever the skew
    is, in an unknown direction.

    ``None`` only when the task has no start recorded yet. A response
    without a ``Date`` header raises instead, because that is a different
    problem with a different fix and the caller cannot tell them apart
    from a bare ``None``.
    """
    response = _get(deployment, f"tasks/{task_id}/events")
    starts = [
        event["created_at"]
        for event in response.json()["events"]
        if event.get("event_type") == "task_started"
        and event.get("report_applied")
        and event.get("created_at")
    ]
    if not starts:
        return None
    served_at = response.headers.get("date")
    if not served_at:
        raise AssertionError(
            f"The registry's response for task {task_id} carried no Date "
            f"header, so there is no server-side 'now' to measure the "
            f"remaining work against. Measuring it against this machine's "
            f"clock instead would add an unknown skew in an unknown "
            f"direction, which is what reading the header avoids."
        )
    return (
        min(datetime.fromisoformat(value) for value in starts),
        parsedate_to_datetime(served_at),
    )
