"""``build_wake``: a build's wake-up flags, on a row of their own.

One row per build, created with it and cascaded with it. It holds the two
columns cross-build flagging writes — ``needs_tick_at`` (a tick is wanted)
and ``tick_requested_at`` (someone was last told to spawn one) — so that
flagging never touches the ``build`` row.

Why a table of its own: a claiming start holds its build row ``FOR SHARE``
while it holds the task row (lock order build → task), and flagging runs
inside a task transition, holding another task row. On the build row a
flagger could only ``SKIP LOCKED`` (waiting would invert the lock order and
deadlock against the claim's task lock), so every build with a claim in
flight silently lost its wake-up until the watchdog. ``build_wake`` rows are
locked only by flaggers, ``notify`` and ``wake-candidates``, none of which
holds a lock anything here waits on, so ``SKIP LOCKED`` on them skips only
a build whose flag another writer is setting at that moment.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, ForeignKeyConstraint, Index, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from stardag_api.models.base import Base, EnvironmentScopedMixin


class BuildWake(EnvironmentScopedMixin, Base):
    """The wake-up state of one build (design.md, "Wake-ups, limits,
    locks")."""

    __tablename__ = "build_wake"
    __table_args__ = (
        ForeignKeyConstraint(
            ["environment_id", "build_id"],
            ["build.environment_id", "build.id"],
            name="fk_build_wake_build",
            ondelete="CASCADE",
        ),
        # wake-candidates: flagged builds of one environment, oldest first.
        Index(
            "ix_build_wake_flagged",
            "environment_id",
            "needs_tick_at",
            postgresql_where=text("needs_tick_at IS NOT NULL"),
        ),
    )

    build_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)

    # The dirty flag: set by a transition that is news for the build, by
    # ``POST /builds/{id}/notify`` and by a limit slot freeing; cleared by
    # the tick right before it computes the frontier (``DELETE
    # /builds/{id}/notify``), so a flag landing mid-tick survives it. NULL =
    # no pending wake-up.
    needs_tick_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # When a caller was last told to spawn a tick for this build (a
    # wake-candidates hand-out, or a notify that reported no live
    # scheduler). A flagged build is handed out at most once per
    # ``services.wakeups.WAKE_HANDOUT_WINDOW``. Not a liveness signal and
    # not cleared: it ages out.
    tick_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
