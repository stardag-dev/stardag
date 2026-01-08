"""Test configuration that imports shared fixtures.

This file re-exports all fixtures from the stardag_integration_tests package
so they are available to all tests in this directory.
"""

import pytest

# Re-export all fixtures from the package
from stardag_integration_tests.conftest import *  # noqa: F401, F403
from stardag_integration_tests.docker_fixtures import *  # noqa: F401, F403


# Playwright timeout configuration (10s instead of default 30s)
PLAYWRIGHT_TIMEOUT_MS = 10_000


@pytest.fixture(scope="session")
def browser_context_args() -> dict:
    """Configure browser context for tests."""
    return {
        "viewport": {"width": 1280, "height": 720},
        "ignore_https_errors": True,
    }


def pytest_configure(config: pytest.Config) -> None:
    """Configure Playwright with shorter timeouts for faster test failures."""
    try:
        from playwright.sync_api import expect

        expect.set_options(timeout=PLAYWRIGHT_TIMEOUT_MS)
    except ImportError:
        # Playwright not installed, skip configuration
        pass
