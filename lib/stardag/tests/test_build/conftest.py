"""Fixtures for build tests."""

import pytest

from stardag.registry import NoOpRegistry


@pytest.fixture
def noop_registry():
    """Provide a NoOpRegistry for tests."""
    return NoOpRegistry()
