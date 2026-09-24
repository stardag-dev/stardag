"""Appending to the v2 event log.

Events are written in bulk and never updated. One transaction's events get
strictly increasing ``created_at`` values (an :class:`EventClock`), so the
log reads in the order the transaction decided things even though every
row shares the transaction's instant.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession

from stardag_api.models import Event, EventType
from stardag_api.models.base import generate_uuid7


@dataclass
class EventClock:
    """Hands out ``now``, ``now + 1µs``, ... for one transaction."""

    now: datetime
    _ticks: int = field(default=0, init=False)

    def tick(self) -> datetime:
        at = self.now + timedelta(microseconds=self._ticks)
        self._ticks += 1
        return at


def event_row(
    environment_id: UUID,
    event_type: EventType,
    *,
    at: datetime,
    build_id: UUID | None = None,
    task_pk: UUID | None = None,
    plan_id: UUID | None = None,
    execution_id: UUID | None = None,
    report_applied: bool = True,
    error_message: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": generate_uuid7(),
        "environment_id": environment_id,
        "event_type": event_type,
        "created_at": at,
        "build_id": build_id,
        "task_pk": task_pk,
        "plan_id": plan_id,
        "execution_id": execution_id,
        "report_applied": report_applied,
        "error_message": error_message,
        "event_metadata": metadata,
    }


async def append(session: AsyncSession, rows: Sequence[dict[str, Any]]) -> None:
    """Insert event rows (a no-op for none)."""
    if rows:
        await session.execute(insert(Event).values(list(rows)))
