"""Test configuration that imports shared fixtures.

This file re-exports all fixtures from the stardag_integration_tests package
so they are available to all tests in this directory.
"""

import os
import re
import tempfile
import typing
from pathlib import Path

import httpx
import pytest

# Re-export all fixtures from the package
from stardag_integration_tests.conftest import *  # noqa: F401, F403
from stardag_integration_tests.docker_fixtures import *  # noqa: F401, F403
from stardag_integration_tests.docker_fixtures import (
    TEST_USER_EMAIL,
    TEST_USER_PASSWORD,
    ServiceEndpoints,
)

# --- v2 SDK build helpers -----------------------------------------------
#
# The v2 registry ties task registration to the plan protocol (design.md,
# "Registration"): there is no ad-hoc "register one task" route left, the
# way v1's ``POST /builds/{id}/tasks`` was. The realistic way to get a real,
# COMPLETED task to query is to run one through the actual SDK build flow
# (``sd.build_sequential`` against ``APIRegistry``), exactly as a real SDK
# user would. These fixtures do that once so individual tests can just ask
# for a task id.


@pytest.fixture
def sdk_api_key(
    internal_authenticated_client: httpx.Client,
    test_workspace_id: str,
    test_environment_id: str,
) -> str:
    """A fresh API key scoped to the test environment, for SDK/API-key flows."""
    response = internal_authenticated_client.post(
        f"/api/v1/ui/workspaces/{test_workspace_id}"
        f"/environments/{test_environment_id}/api-keys",
        json={"name": "Integration Test SDK Key"},
    )
    assert response.status_code == 201, response.text
    return response.json()["key"]


@pytest.fixture
def temporary_default_target_root(
    tmp_path: Path,
) -> typing.Generator[Path, None, None]:
    """Point the SDK's default target root at a throwaway directory, so a
    task built in a test can determine its own completion."""
    from stardag.testing import target_roots_override

    target_roots = {"default": str(tmp_path)}
    with target_roots_override(target_roots):
        yield tmp_path


@pytest.fixture
def built_task_id(
    docker_services: ServiceEndpoints,  # noqa: F811
    sdk_api_key: str,
    temporary_default_target_root: Path,
) -> str:
    """Build one trivial task through the real SDK build flow (API-key
    auth) and return its task id (the completion hash) -- a real, COMPLETED
    task for tests that just need one to query.

    ``marker`` is a fresh uuid4 per call and is the task's only significant
    field, so every call gets its own task id -- reusing one across test
    functions would otherwise hit ``task_identity_conflict`` (409): the
    tests share one long-lived environment, and ``output_uri`` (derived
    from ``temporary_default_target_root``, unique per test) is
    identity-level, so the *same* task id re-registered under a *different*
    output_uri is correctly refused.
    """
    import uuid

    import stardag as sd
    from stardag.registry import APIRegistry

    @sd.task
    def one_task(marker: str) -> str:
        return marker

    task = one_task(marker=str(uuid.uuid4()))
    registry = APIRegistry(api_url=docker_services.api, api_key=sdk_api_key)
    try:
        sd.build_sequential([task], registry=registry)
    finally:
        registry.close()
    return str(task.id)


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

    # Create a temporary file for storage state. mkstemp rather than mktemp:
    # this file holds session cookies, and mktemp only *predicts* a free name,
    # leaving a window in which another process can create that path first
    # (as a symlink, say) between the check and Playwright's write. mkstemp
    # creates it atomically with 0600 and hands back the descriptor, which we
    # close immediately since Playwright writes the path by name.
    handle, storage_path = tempfile.mkstemp(suffix=".json")
    os.close(handle)
    storage_file = Path(storage_path)
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
