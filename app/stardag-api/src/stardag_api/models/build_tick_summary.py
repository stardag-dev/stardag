"""Reactive scheduler tick summaries retained per build."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import ForeignKeyConstraint, Index, String, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from stardag_api.models.base import Base, EnvironmentScopedMixin, generate_uuid7


class BuildTickSummary(EnvironmentScopedMixin, Base):
    """Outcome of one reactive scheduler tick, kept as a per-build trail.

    A tick's ``TickSummary`` is otherwise written only to the log of the
    (short-lived, one-per-tick) container that produced it, so answering
    "why is this build not progressing?" means correlating logs across
    dozens of containers. Persisting the last N summaries against the
    build turns that into a single query.

    Shape: one promoted column plus an opaque blob.

    - ``summary`` is the whole summary dict *verbatim*. The dataclass is
      SDK-owned and still growing, and this table is a write-mostly
      observability trail, not a queried entity — so new fields must not
      cost a migration, and unknown keys are stored rather than rejected.
    - ``outcome`` is lifted out of the blob into a typed column because
      it is the one field worth filtering on ("show me the ticks where this
      build did nothing"), and the UI reads it on every row. It is *also*
      still present inside ``summary``; the blob stays a faithful copy of
      what the SDK sent.

    Rows are pruned to the newest N per build on insert, so this table's
    growth is bounded by the number of builds, not by tick rate.
    """

    __tablename__ = "build_tick_summary"
    __table_args__ = (
        ForeignKeyConstraint(
            ["environment_id", "build_id"],
            ["build.environment_id", "build.id"],
            name="fk_build_tick_summary_build",
            ondelete="CASCADE",
        ),
        # The only query is "newest N summaries for this build", for both
        # the read endpoint and the retention prune; it also serves the
        # cascade from ``build``.
        Index("ix_build_tick_summary_build_created", "build_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=generate_uuid7)
    build_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)

    # "not_reactive" | "lease_held" | "terminal" | "lingered_out" | ... —
    # SDK-owned and open-ended (the Modal wrapper adds its own), hence a
    # plain string rather than an enum: an unrecognised outcome must
    # round-trip, not 500.
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)

    # The tick summary as sent, unknown keys included.
    summary: Mapped[dict] = mapped_column(JSONB, nullable=False)
