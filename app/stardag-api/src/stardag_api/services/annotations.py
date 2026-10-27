"""Annotations of a completion: ``task.tags`` and ``task.description``
(``PATCH /tasks/{task_id}``, STA-149).

They describe the asset a completion stands for, so they live on the
``task`` row (the task-id level), not on an instance: on instances they
would fragment across deployments. They are mutable, and outside every
hash and every registration payload — registration never names them, so it
can neither set nor reset them, and they do not move ``status_at`` (the
listing's order).

Tags are for discovery, never for addressing: code references a task by its
id. They are stored normalised — each trimmed, non-empty, at most
:data:`MAX_TAG_LENGTH` characters, free of control characters; the set
deduplicated, sorted and at most :data:`MAX_TAGS` long — so containment
filters compare like with like. Case is kept: ``Prod`` and ``prod`` are two
tags.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable
from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from stardag_api.models import Task
from stardag_api.services.errors import NotFound
from stardag_api.services.reads import TaskView, _with_claim_build
from stardag_api.services.tx import retry_on_deadlock, transaction

#: Most tags one task may carry.
MAX_TAGS = 32
#: Longest tag, in characters, after trimming.
MAX_TAG_LENGTH = 64
#: Longest description, in characters, after trimming.
MAX_DESCRIPTION_LENGTH = 4096


def _has_control_characters(value: str) -> bool:
    return any(unicodedata.category(c) == "Cc" for c in value)


def normalise_tag(tag: str) -> str:
    """One tag, trimmed; ``ValueError`` if empty, too long or carrying a
    control character."""
    tag = tag.strip()
    if not tag:
        raise ValueError("a tag must not be empty")
    if len(tag) > MAX_TAG_LENGTH:
        raise ValueError(f"a tag is at most {MAX_TAG_LENGTH} characters: {tag[:20]!r}…")
    if _has_control_characters(tag):
        raise ValueError(f"a tag must not contain control characters: {tag!r}")
    return tag


def normalise_tags(tags: Iterable[str]) -> list[str]:
    """Each tag normalised, then deduplicated and sorted; ``ValueError`` on
    an invalid tag or more than :data:`MAX_TAGS` distinct ones."""
    normalised = sorted({normalise_tag(t) for t in tags})
    if len(normalised) > MAX_TAGS:
        raise ValueError(f"a task carries at most {MAX_TAGS} tags")
    return normalised


def normalise_description(description: str | None) -> str | None:
    """Trimmed; an empty description is ``None`` (cleared). ``ValueError``
    if longer than :data:`MAX_DESCRIPTION_LENGTH`."""
    if description is None:
        return None
    description = description.strip()
    if len(description) > MAX_DESCRIPTION_LENGTH:
        raise ValueError(
            f"a description is at most {MAX_DESCRIPTION_LENGTH} characters"
        )
    return description or None


@retry_on_deadlock
async def annotate_task(
    session: AsyncSession,
    environment_id: UUID,
    task_id: str,
    changes: dict[str, Any],
) -> TaskView:
    """Set the given annotations of a completion in the caller's
    environment and return it (without instances). ``changes`` holds only
    the fields to change — ``tags`` (already normalised) and/or
    ``description`` — so an omitted field is left as it is. 404
    ``unknown_task`` if there is no such task.

    One ``UPDATE`` of non-key columns: it takes the row's ``FOR NO KEY
    UPDATE`` lock, as claim arbitration does, for the length of one
    statement, and touches neither the status nor ``status_at``."""
    unknown = NotFound("unknown_task", f"no task {task_id}", task_id=task_id)
    where = (Task.environment_id == environment_id, Task.task_id == task_id)
    async with transaction(session):
        if changes:
            updated = await session.scalar(
                update(Task).where(*where).values(**changes).returning(Task.id)
            )
            if updated is None:
                raise unknown
        row = (
            await session.execute(
                _with_claim_build(select(Task))
                .where(*where)
                .execution_options(populate_existing=True)
            )
        ).one_or_none()
        if row is None:
            raise unknown
        task, claim_build_id = row._tuple()
        return TaskView.of(task, claim_build_id)


__all__ = [
    "MAX_DESCRIPTION_LENGTH",
    "MAX_TAGS",
    "MAX_TAG_LENGTH",
    "annotate_task",
    "normalise_description",
    "normalise_tag",
    "normalise_tags",
]
