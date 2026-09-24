"""``settings``: build-wide environment variables, stored by content hash.

The other half of the deterministic scope. A flat ``dict[str, str]``
applied in every process of a build; it may change structure and
execution, never output. Created lazily by the first plan that needs it —
the empty settings are ``{}`` and get a row like any other.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import PrimaryKeyConstraint, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from stardag_api.models.base import Base, EnvironmentScopedMixin


class SettingsRecord(EnvironmentScopedMixin, Base):
    """One settings body. Named ``SettingsRecord`` in Python only so it
    cannot be confused with the service's pydantic ``Settings``."""

    __tablename__ = "settings"
    __table_args__ = (
        PrimaryKeyConstraint("environment_id", "hash", name="pk_settings"),
    )

    # uuid5 of the canonical JSON body (sorted keys, compact separators,
    # UTF-8) in a fixed namespace (``services.deployments``), computed by
    # the registry: stable across code versions, unlike task hashes.
    hash: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    body: Mapped[dict[str, str]] = mapped_column(JSONB, nullable=False)
