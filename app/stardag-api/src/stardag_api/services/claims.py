"""Liveness of the per-task execution claim.

``Task.latest_status == RUNNING`` *is* the execution claim — arbitrated in
``routes/builds.py::_create_task_event`` under ``SELECT … FOR UPDATE``, in
the same transaction as the event and as the concurrency-limit slot rows.
That single-row design is deliberate and its advantages are load-bearing
(one transaction so claim, status, completion and slot occupancy cannot
drift; no join on a frontier that is re-read every few seconds; zero
liveness traffic). See ``docs/design/execution-claims-and-liveness.md``
before changing any of it.

Its one defect is what this module addresses: the claim recorded no
liveness evidence a **third party** could evaluate. This adds an expiry —
one nullable column, written once, no heartbeats — and the two predicates
that honour it. Both must, and that is the easy half to forget: if the
claim check honours the expiry but the concurrency-limit count does not,
an abandoned task stops blocking re-execution yet keeps occupying its
slots, and half the healing is lost.

The two predicates are the same rule expressed twice, once in Python (for
the FOR-UPDATE-locked row already in hand) and once in SQL (for counting
rows we do not want to load):

    RUNNING AND (expires_at IS NULL OR expires_at > now())
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import ColumnElement, or_

from stardag_api.config import claim_settings
from stardag_api.models import Task, TaskStatus
from stardag_api.models.base import as_utc, utc_now


def claim_ttl(ttl_seconds: int | None) -> int:
    """Resolve a claim TTL: the caller's, else the server default.

    Client-supplied wins because the caller is the only party that knows how
    long the execution it is about to spawn may legitimately take — a claim
    should outlive its execution by a small grace and no more. The server
    default (``ClaimSettings.default_ttl_seconds``) is the fallback for
    callers that say nothing, and is generous on purpose.
    """
    return claim_settings.default_ttl_seconds if ttl_seconds is None else ttl_seconds


def claim_expires_at(granted_at: datetime, ttl_seconds: int | None) -> datetime:
    """When a claim granted at ``granted_at`` stops being believable.

    Measured from the granting event's timestamp rather than from "now" so
    the stored expiry matches the event that produced it, and so replaying
    the event stream yields the same value it did the first time.
    """
    return as_utc(granted_at) + timedelta(seconds=claim_ttl(ttl_seconds))


def claim_is_live(task: Task, now: datetime | None = None) -> bool:
    """Whether ``task`` currently holds a believable execution claim.

    The Python half of the predicate, for a task row already loaded (and,
    at the one call site that matters, locked FOR UPDATE). An expired claim
    is *not* a distinct state a caller has to handle: it simply is not a
    claim, so the task is claimable again by whoever asks next.
    """
    if task.latest_status != TaskStatus.RUNNING:
        return False
    expires_at = task.latest_status_expires_at
    return expires_at is None or as_utc(expires_at) > (now or utc_now())


def live_claim_filter(now: datetime | None = None) -> ColumnElement[bool]:
    """SQL for :func:`claim_is_live`, for counting or listing claim holders.

    ``latest_status`` leads the expression so an index on it (or on
    ``(environment_id, latest_status, …)``) still drives the scan; the
    expiry comparison then filters the few rows that survive.
    """
    return (Task.latest_status == TaskStatus.RUNNING) & or_(
        Task.latest_status_expires_at.is_(None),
        Task.latest_status_expires_at > (now or utc_now()),
    )
