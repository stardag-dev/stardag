"""Reactive scheduler tick summaries retained per build."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import ForeignKey, Index, JSON, String, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from stardag_api.models.base import Base, TimestampMixin, generate_uuid7

if TYPE_CHECKING:
    from stardag_api.models.build import Build


class BuildTickSummary(Base, TimestampMixin):
    """Outcome of one reactive scheduler tick, kept as a per-build trail.

    A tick's ``TickSummary`` is otherwise written only to the log of the
    (short-lived, one-per-tick) container that produced it, so answering
    "why is this build not progressing?" means correlating logs across
    dozens of containers. Persisting the last N summaries against the
    build turns that into a single query.

    Shape: one promoted column plus an opaque blob.

    - ``summary`` is the whole summary dict *verbatim*. The dataclass is
      SDK-owned and still growing (e.g. counts/details for tasks blocked
      by another build), and this table is a write-mostly observability
      trail, not a queried entity — so new fields must not cost a
      migration, and unknown keys are stored rather than rejected.
    - ``outcome`` is lifted out of the blob into a typed column because
      it is the one field with a small closed-ish vocabulary that is
      worth filtering and indexing on ("show me the ticks where this
      build did nothing"), and the UI reads it on every row. It is
      *also* still present inside ``summary``; the duplication is
      deliberate — the blob stays a faithful copy of what the SDK sent.

    Rows are pruned to the newest N per build on insert (see
    ``routes/tick_summaries.py``), so this table's growth is bounded by
    the number of builds, not by tick rate.
    """

    __tablename__ = "build_tick_summaries"
    __table_args__ = (
        # The only query is "newest N summaries for this build", for both
        # the read endpoint and the retention prune. This composite also
        # serves the plain ``build_id`` lookups (leading column) and the
        # FK's cascade delete, so no separate index on ``build_id``: the
        # write path is one insert + one delete per tick, and every extra
        # index is paid on both.
        Index(
            "ix_build_tick_summaries_build_created",
            "build_id",
            "created_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid,
        primary_key=True,
        default=generate_uuid7,
    )
    build_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("builds.id", ondelete="CASCADE"),
        nullable=False,
    )

    # "not_reactive" | "lease_held" | "terminal" | "lingered_out" | ... —
    # SDK-owned and open-ended (the Modal wrapper adds its own), hence a
    # plain string rather than an enum: an unrecognised outcome must
    # round-trip, not 500.
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)

    # The tick summary as sent, unknown keys included. JSONB on Postgres
    # for consistency with the other JSON columns; SQLite keeps JSON.
    summary: Mapped[dict] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"),
        nullable=False,
    )

    # Relationships
    build: Mapped[Build] = relationship()
