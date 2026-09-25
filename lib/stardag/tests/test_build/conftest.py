"""Fixtures for build tests."""

from __future__ import annotations

import pytest

from stardag.registry import NoOpRegistry
from stardag.testing import InMemoryRegistry


@pytest.fixture
def noop_registry():
    """No registry: the engines make no registry call at all."""
    return NoOpRegistry()


@pytest.fixture
def recording_registry() -> InMemoryRegistry:
    """The in-memory v2 registry; records every call for assertions
    (``calls_to``) and keeps the server's state (``tasks``, ``plans``,
    ``executions``, ...)."""
    return InMemoryRegistry()


@pytest.fixture(autouse=True)
def _pinned_code_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stable local code id, so a local deployment is one row per test
    (the working tree may be dirty)."""
    monkeypatch.setenv("STARDAG_CODE_ID", "test-code")
