"""Test configuration that imports shared fixtures.

This file re-exports all fixtures from the stardag_integration_tests package
so they are available to all tests in this directory.
"""

import re
import tempfile
from pathlib import Path

import pytest

# Re-export all fixtures from the package
from stardag_integration_tests.conftest import *  # noqa: F401, F403
from stardag_integration_tests.docker_fixtures import *  # noqa: F401, F403
from stardag_integration_tests.docker_fixtures import (
    TEST_USER_EMAIL,
    TEST_USER_PASSWORD,
    ServiceEndpoints,
)


# Playwright timeout configuration (10s instead of default 30s)
PLAYWRIGHT_TIMEOUT_MS = 10_000

# Storage state file for authenticated session (session-scoped)
_AUTH_STORAGE_STATE_FILE: Path | None = None


def pytest_configure(config: pytest.Config) -> None:
    """Configure Playwright with shorter timeouts for faster test failures."""
    try:
        from playwright.sync_api import expect

        expect.set_options(timeout=PLAYWRIGHT_TIMEOUT_MS)
    except ImportError:
        # Playwright not installed, skip configuration
        pass


@pytest.fixture(scope="session")
def browser_context_args() -> dict:
    """Configure browser context for tests."""
    return {
        "viewport": {"width": 1280, "height": 720},
        "ignore_https_errors": True,
    }


@pytest.fixture(scope="session")
def auth_storage_state(
    browser,  # noqa: ANN001 - Browser type from pytest-playwright
    docker_services: ServiceEndpoints,  # noqa: F811
) -> Path:
    """Create and cache authenticated storage state for the session.

    This logs in once at the start of the test session and saves the
    authentication cookies/localStorage to a file that can be reused
    by all subsequent tests.

    Args:
        browser: Playwright Browser instance (from pytest-playwright)
        docker_services: Service endpoints fixture
    """
    global _AUTH_STORAGE_STATE_FILE

    # Create a temporary file for storage state
    storage_file = Path(tempfile.mktemp(suffix=".json"))
    _AUTH_STORAGE_STATE_FILE = storage_file

    # Create a fresh context for login
    context = browser.new_context(
        viewport={"width": 1280, "height": 720},
        ignore_https_errors=True,
    )
    page = context.new_page()

    try:
        # Navigate to UI and login
        page.goto(docker_services.ui)

        # Wait for either Keycloak login form OR sidebar (already logged in)
        keycloak_form = page.locator("input[name='username']")
        sidebar_btn = page.locator("button[title='Collapse sidebar']")

        try:
            page.wait_for_selector(
                "input[name='username'], button[title='Collapse sidebar']",
                timeout=15000,
            )
        except Exception:
            # If neither appears, click login button
            login_btn = page.get_by_text("Login").or_(page.get_by_text("Sign in"))
            if login_btn.first.is_visible():
                login_btn.first.click()
                page.wait_for_selector("input[name='username']", timeout=10000)

        # If on Keycloak, fill in credentials
        if keycloak_form.is_visible():
            keycloak_form.fill(TEST_USER_EMAIL)
            page.locator("input[name='password']").fill(TEST_USER_PASSWORD)
            page.locator(
                "input[type='submit'], button[type='submit'], #kc-login"
            ).first.click()
            page.wait_for_url(re.compile(r".*localhost:3000.*"), timeout=10000)
            page.wait_for_load_state("networkidle")

        # Wait for sidebar to confirm login succeeded
        sidebar_btn.wait_for(state="visible", timeout=10000)

        # Save storage state (cookies, localStorage)
        context.storage_state(path=str(storage_file))

    finally:
        context.close()

    return storage_file


@pytest.fixture(scope="class")
def logged_in_context(
    browser,  # noqa: ANN001 - Browser type from pytest-playwright
    auth_storage_state: Path,
):  # noqa: ANN201 - returns BrowserContext from playwright
    """Create a browser context with pre-authenticated session.

    This is class-scoped so each test class gets its own context,
    but shares the authentication state from the session-scoped login.

    Args:
        browser: Playwright Browser instance (from pytest-playwright)
        auth_storage_state: Path to saved authentication state
    """
    context = browser.new_context(
        storage_state=str(auth_storage_state),
        viewport={"width": 1280, "height": 720},
        ignore_https_errors=True,
    )
    # Auto-dismiss the onboarding modal by setting sessionStorage before page loads
    # sessionStorage is not persisted by Playwright's storage_state(), so we use
    # add_init_script to set it on every page load
    context.add_init_script(
        "sessionStorage.setItem('stardag_onboarding_dismissed', 'true');"
    )
    yield context
    context.close()


@pytest.fixture
def logged_in_page(
    logged_in_context,  # noqa: ANN001 - BrowserContext from playwright
):  # noqa: ANN201 - returns Page from playwright
    """Create a new page in the authenticated context.

    Each test gets a fresh page but with authentication already done.

    Args:
        logged_in_context: Playwright BrowserContext with auth state loaded
    """
    page = logged_in_context.new_page()
    yield page
    page.close()
