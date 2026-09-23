"""Wire schemas of the ``/api/v2`` registry routes.

The registration item is shared by the services and the routes: it is the
one shape every registration route carries (design.md, "Registration").
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class RegistrationItem(BaseModel):
    """One task instance, as the static phase (or a yield) states it.

    ``declared_upstreams`` names upstream **instances** by ``instance_hash``
    (a scope may hold several instances of one completion); ``None`` means
    "not expanded" — the driver did not evaluate ``requires()``, typically
    because the target already existed. ``observed_complete`` and
    ``observed_at`` are what the driver saw when it checked the target, on
    its own clock.
    """

    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(min_length=1, max_length=64)
    task_namespace: str = Field(default="", max_length=255)
    task_name: str = Field(min_length=1, max_length=255)
    version: str | None = Field(default=None, max_length=64)
    output_uri: str | None = Field(default=None, max_length=2048)
    instance_hash: str = Field(min_length=1, max_length=64)
    body: dict[str, Any]
    declared_upstreams: list[str] | None = None
    observed_complete: bool = False
    observed_at: datetime
    limit_keys: list[str] | None = None
