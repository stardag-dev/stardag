"""Fixtures for build tests."""

import pytest

from stardag.registry import NoOpRegistry


@pytest.fixture
def noop_registry():
    """Provide a NoOpRegistry for tests.

    TODO remove? Could be wrapped by Mock to track called methods.
    """
    return NoOpRegistry()
