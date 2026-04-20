"""Unit tests for APIRegistry helpers."""

from __future__ import annotations


from stardag.exceptions import APIError, NotFoundError
from stardag.registry._api_registry import _is_route_not_found


class TestIsRouteNotFound:
    """Narrow-404 detection used by task_add_dependencies for backward compat."""

    def test_fastapi_default_is_route_not_found(self):
        """FastAPI's default unknown-path response: detail == 'Not Found'."""
        err = NotFoundError("op: resource not found", detail="Not Found")
        assert _is_route_not_found(err) is True

    def test_build_not_found_is_not_route(self):
        """An app-level 'Build not found' must NOT be treated as route-missing."""
        err = NotFoundError("op: resource not found", detail="Build not found")
        assert _is_route_not_found(err) is False

    def test_task_not_registered_is_not_route(self):
        err = NotFoundError(
            "op: resource not found",
            detail="Task abc not registered in this environment",
        )
        assert _is_route_not_found(err) is False

    def test_none_detail_is_not_route(self):
        err = NotFoundError("op: resource not found", detail=None)
        assert _is_route_not_found(err) is False

    def test_empty_detail_is_not_route(self):
        err = NotFoundError("op: resource not found", detail="")
        assert _is_route_not_found(err) is False

    def test_structured_detail_is_not_route(self):
        # When detail is a dict stringified by _handle_response_error
        err = NotFoundError(
            "op: resource not found", detail="{'error_code': 'X', 'message': 'Y'}"
        )
        assert _is_route_not_found(err) is False

    def test_accepts_notfounderror_only(self):
        # Signature sanity: helper takes NotFoundError; APIError with a 404 status
        # is unusual but we don't check status_code, only detail.
        err = APIError("misc", status_code=500, detail="Not Found")
        # The helper reads .detail directly, so a non-NotFoundError would still
        # return True if detail matches — not our concern; call sites only pass
        # NotFoundError. This test documents the contract.
        assert _is_route_not_found(err) is True  # type: ignore[arg-type]
