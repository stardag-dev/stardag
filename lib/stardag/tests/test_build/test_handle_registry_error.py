"""``handle_registry_error``: ``"warn"`` tolerates outages, never refusals."""

from __future__ import annotations

import logging

import pytest

from stardag.build._base import handle_registry_error
from stardag.exceptions import (
    BuildConfigMismatchError,
    RegistryTooOldError,
    ScopeMismatchError,
)


def test_warn_logs_an_outage_and_continues(caplog: pytest.LogCaptureFixture):
    with caplog.at_level(logging.WARNING):
        handle_registry_error(ConnectionError("down"), "Failed to X", "warn")
    assert "Failed to X: down" in caplog.text


def test_raise_propagates_an_outage():
    with pytest.raises(ConnectionError):
        handle_registry_error(ConnectionError("down"), "Failed to X", "raise")


@pytest.mark.parametrize(
    "error",
    [
        RegistryTooOldError("too old", operation="POST /builds/x/resume"),
        ScopeMismatchError("scope refused"),
        BuildConfigMismatchError("config differs"),
    ],
)
def test_a_refusal_propagates_even_under_warn(error: Exception):
    """A server that predates scopes, or that refused a scope or config
    claim, must not be shrugged off as an outage: the build would run under
    a different rule than the one it asked for."""
    with pytest.raises(type(error)):
        handle_registry_error(error, "Failed to mark build resumed", "warn")
