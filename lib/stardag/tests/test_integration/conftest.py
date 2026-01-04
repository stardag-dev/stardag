"""Conftest for integration tests.

All tests in this directory are automatically marked as integration tests.
To skip them, run: pytest -m "not integration"
"""

import pytest


def pytest_collection_modifyitems(items):
    """Automatically mark all tests in this directory as integration tests."""
    for item in items:
        # Check if the test is in the test_integration directory
        if "test_integration" in str(item.fspath):
            item.add_marker(pytest.mark.integration)
