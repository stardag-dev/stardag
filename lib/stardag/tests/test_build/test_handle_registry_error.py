"""``handle_registry_error``: ``"warn"`` tolerates outages, never refusals."""

from __future__ import annotations

import logging

import pytest

from stardag.build._base import handle_registry_error, is_refusal
from stardag.exceptions import APIError


def test_warn_logs_an_outage_and_continues(caplog: pytest.LogCaptureFixture):
    with caplog.at_level(logging.WARNING):
        handle_registry_error(ConnectionError("down"), "Failed to X", "warn")
    assert "Failed to X: down" in caplog.text


def test_raise_propagates_an_outage():
    with pytest.raises(ConnectionError):
        handle_registry_error(ConnectionError("down"), "Failed to X", "raise")


def test_a_server_error_is_an_outage(caplog: pytest.LogCaptureFixture):
    error = APIError("boom", status_code=503)
    assert not is_refusal(error)
    with caplog.at_level(logging.WARNING):
        handle_registry_error(error, "Failed to X", "warn")


@pytest.mark.parametrize(
    "error",
    [
        APIError("conflict", status_code=409, payload={"code": "instance_conflict"}),
        APIError("bad", status_code=400, payload={"code": "reserved_settings_key"}),
        APIError("invalid", status_code=422),
    ],
)
def test_a_refusal_propagates_even_under_warn(error: Exception):
    """The registry understood the call and said no: carrying on would run
    the build under a different rule than the one it asked for."""
    assert is_refusal(error)
    with pytest.raises(APIError):
        handle_registry_error(error, "Failed to register", "warn")
