"""Service-level refusals of the v2 registry.

A service raises one of these; a route maps it to its status code with the
``code`` as the machine-readable reason, and nothing else. Codes are the
ones ``docs/design/registry-v2/design.md`` names (``instance_conflict``,
``plan_superseded``, ``upstream_incomplete``, ...), so a client, a log line
and the design read the same.

Some refusals are **recorded**: a late or second report is written to the
event log (``report_applied = false``) and to the execution ledger before
it is refused. The service commits that record first and raises after, so
the refusal never rolls back the history of having received it.
"""

from __future__ import annotations

from typing import Any


class RegistryError(Exception):
    """A refusal with a stable ``code`` and structured ``detail``."""

    status_code: int = 400

    def __init__(self, code: str, message: str, **detail: Any) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.detail = detail

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, **self.detail}


class BadRequest(RegistryError):
    """The request is wrong on its own terms (400)."""

    status_code = 400


class NotFound(RegistryError):
    """A named row does not exist in the caller's environment (404)."""

    status_code = 404


class Conflict(RegistryError):
    """The request is well-formed but contradicts recorded state (409)."""

    status_code = 409
