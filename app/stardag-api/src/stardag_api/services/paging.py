"""Keyset paging for the v2 list reads: an opaque cursor over the sort key.

A listing sorted by ``(at DESC, id DESC)`` returns ``next_cursor`` when the
page was full; the next call passes it back and gets the rows strictly
after the last one returned. Keyset, not offset: a page is one index range
scan however deep it is, and a row inserted ahead of the cursor does not
shift the rest. A row whose sort key moves while a caller pages (a build
resumed, a task changing status) can be seen twice or not at all; a
listing is a snapshot per page, not a consistent read across pages.

The cursor is ``base64url(json([at_iso, id]))``: opaque to the client, and
400 ``invalid_cursor`` when it does not decode.
"""

from __future__ import annotations

import base64
import binascii
import json
from datetime import datetime
from uuid import UUID

from stardag_api.services.errors import BadRequest


def encode_cursor(at: datetime, row_id: UUID) -> str:
    raw = json.dumps([at.isoformat(), str(row_id)], separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        at, row_id = json.loads(base64.urlsafe_b64decode(padded.encode()))
        return datetime.fromisoformat(at), UUID(row_id)
    except (binascii.Error, ValueError, TypeError, UnicodeDecodeError) as exc:
        raise BadRequest("invalid_cursor", "the cursor does not decode") from exc


__all__ = ["decode_cursor", "encode_cursor"]
