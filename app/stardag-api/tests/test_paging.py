"""Tests for the v2 keyset cursor codec."""

import base64
import json
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from stardag_api.services.errors import BadRequest
from stardag_api.services.paging import decode_cursor, encode_cursor


def _b64(payload: object) -> str:
    raw = json.dumps(payload).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


class TestRoundTrip:
    def test_encode_decode(self):
        at = datetime(2024, 1, 1, tzinfo=timezone.utc)
        row_id = uuid4()
        cursor = encode_cursor(at, row_id)
        decoded_at, decoded_id = decode_cursor(cursor)
        assert decoded_at == at
        assert decoded_id == row_id


class TestInvalidCursor:
    def test_not_base64(self):
        with pytest.raises(BadRequest) as exc_info:
            decode_cursor("not-valid-base64!!!")
        assert exc_info.value.code == "invalid_cursor"

    def test_not_json(self):
        raw = base64.urlsafe_b64encode(b"not json").decode().rstrip("=")
        with pytest.raises(BadRequest) as exc_info:
            decode_cursor(raw)
        assert exc_info.value.code == "invalid_cursor"

    def test_wrong_shape_not_a_list(self):
        """A base64-valid cursor decoding to a JSON object, not a 2-list."""
        cursor = _b64({"at": "2024-01-01T00:00:00", "id": str(uuid4())})
        with pytest.raises(BadRequest) as exc_info:
            decode_cursor(cursor)
        assert exc_info.value.code == "invalid_cursor"

    def test_wrong_shape_wrong_length(self):
        cursor = _b64(["2024-01-01T00:00:00"])
        with pytest.raises(BadRequest) as exc_info:
            decode_cursor(cursor)
        assert exc_info.value.code == "invalid_cursor"

    def test_wrong_type_integer_id(self):
        """An integer where the UUID string is expected: previously an
        uncaught AttributeError from UUID(int) rather than a 400."""
        cursor = _b64(["2024-01-01T00:00:00", 123])
        with pytest.raises(BadRequest) as exc_info:
            decode_cursor(cursor)
        assert exc_info.value.code == "invalid_cursor"

    def test_wrong_type_integer_at(self):
        cursor = _b64([1704067200, str(uuid4())])
        with pytest.raises(BadRequest) as exc_info:
            decode_cursor(cursor)
        assert exc_info.value.code == "invalid_cursor"

    def test_id_not_a_valid_uuid(self):
        cursor = _b64(["2024-01-01T00:00:00+00:00", "not-a-uuid"])
        with pytest.raises(BadRequest) as exc_info:
            decode_cursor(cursor)
        assert exc_info.value.code == "invalid_cursor"

    def test_at_not_a_valid_timestamp(self):
        cursor = _b64(["not-a-timestamp", str(uuid4())])
        with pytest.raises(BadRequest) as exc_info:
            decode_cursor(cursor)
        assert exc_info.value.code == "invalid_cursor"

    def test_at_is_a_naive_timestamp(self):
        """A timestamp with no timezone offset would otherwise reach
        asyncpg and fail comparison against the ``timestamptz`` sort key
        instead of decoding to a clean 400."""
        cursor = _b64(["2024-01-01T00:00:00", str(uuid4())])
        with pytest.raises(BadRequest) as exc_info:
            decode_cursor(cursor)
        assert exc_info.value.code == "invalid_cursor"
