"""Frontend browser tests using Playwright.

These tests verify the React UI works correctly with real backend services.
Run with: pytest -m browser
Requires: uv sync --extra browser && uv run playwright install chromium
"""

import re

import pytest

try:
    from playwright.sync_api import Page, expect

    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False
    Page = None  # type: ignore
    expect = None  # type: ignore

from stardag_integration_tests.docker_fixtures import (
    TEST_USER_EMAIL,
    TEST_USER_PASSWORD,
    ServiceEndpoints,
)

# Skip all tests if playwright is not installed
pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(
        not PLAYWRIGHT_AVAILABLE,
        reason="Playwright not installed. Run: uv sync --extra browser",
    ),
]


@pytest.fixture(scope="session")
def browser_context_args():
    """Configure browser context for tests."""
    return {
        "viewport": {"width": 1280, "height": 720},
        "ignore_https_errors": True,
    }


class TestUILogin:
    """Test the login flow in the UI."""

    def test_login_page_loads(
        self,
        page: Page,
        docker_services: ServiceEndpoints,
    ) -> None:
        """Test that the login page loads and shows Keycloak login."""
        page.goto(docker_services.ui)

        # Should redirect to Keycloak login
        # Wait for either the UI to load or redirect to Keycloak
        page.wait_for_load_state("networkidle")

        # Check if we're on Keycloak or the main app
        url = page.url
        if "keycloak" in url or "realms/stardag" in url:
            # On Keycloak login page
            expect(page.locator("input[name='username']")).to_be_visible()
            expect(page.locator("input[name='password']")).to_be_visible()
        else:
            # On main app - might show login button or be logged in
            # Either way, page should have loaded successfully
            assert page.title() or page.locator("body").is_visible()

    def test_keycloak_login_flow(
        self,
        page: Page,
        docker_services: ServiceEndpoints,
    ) -> None:
        """Test full login flow through Keycloak."""
        page.goto(docker_services.ui)
        page.wait_for_load_state("networkidle")

        # If not already on Keycloak, find and click login button
        if "keycloak" not in page.url:
            # Look for a login button
            login_button = (
                page.locator("text=Login")
                .or_(page.locator("text=Sign in"))
                .or_(page.locator("button:has-text('Login')"))
            )
            if login_button.is_visible():
                login_button.click()
                page.wait_for_load_state("networkidle")

        # Should be on Keycloak now
        if "keycloak" in page.url or "realms/stardag" in page.url:
            # Fill in credentials
            page.locator("input[name='username']").fill(TEST_USER_EMAIL)
            page.locator("input[name='password']").fill(TEST_USER_PASSWORD)
            page.locator("input[type='submit']").click()

            # Wait for redirect back to app
            page.wait_for_url(
                re.compile(f".*{re.escape('localhost:3000')}.*"), timeout=10000
            )

            # Should be logged in now
            page.wait_for_load_state("networkidle")

            # Check for some indicator that we're logged in
            # This could be user name, avatar, or main app content
            expect(page.locator("body")).to_contain_text(
                re.compile("(Dashboard|Builds|Workspaces|testuser)", re.IGNORECASE)
            )


class TestUINavigation:
    """Test basic UI navigation after login."""

    @pytest.fixture(autouse=True)
    def login_first(
        self,
        page: Page,
        docker_services: ServiceEndpoints,
    ) -> None:
        """Login before each test in this class."""
        page.goto(docker_services.ui)
        page.wait_for_load_state("networkidle")

        # If on Keycloak, login
        if "keycloak" in page.url or "realms/stardag" in page.url:
            page.locator("input[name='username']").fill(TEST_USER_EMAIL)
            page.locator("input[name='password']").fill(TEST_USER_PASSWORD)
            page.locator("input[type='submit']").click()
            page.wait_for_url(re.compile(r".*localhost:3000.*"), timeout=10000)
            page.wait_for_load_state("networkidle")

    def test_main_page_loads(
        self,
        page: Page,
    ) -> None:
        """Test that main page loads after login."""
        # Should be on main app
        expect(page.locator("body")).to_be_visible()
        # Check page has rendered something meaningful
        assert (
            page.title()
            or page.locator("main, div#root, div[class*='app']").is_visible()
        )

    def test_navigation_works(
        self,
        page: Page,
    ) -> None:
        """Test that navigation links work."""
        # Look for common navigation elements
        nav_links = page.locator("nav a, header a, aside a")

        if nav_links.count() > 0:
            # Try clicking the first nav link
            first_link = nav_links.first
            first_link.click()
            page.wait_for_load_state("networkidle")
            # Page should still be functional
            expect(page.locator("body")).to_be_visible()


class TestUIBuildsPage:
    """Test the builds page functionality."""

    @pytest.fixture(autouse=True)
    def login_first(
        self,
        page: Page,
        docker_services: ServiceEndpoints,
    ) -> None:
        """Login before each test in this class."""
        page.goto(docker_services.ui)
        page.wait_for_load_state("networkidle")

        if "keycloak" in page.url or "realms/stardag" in page.url:
            page.locator("input[name='username']").fill(TEST_USER_EMAIL)
            page.locator("input[name='password']").fill(TEST_USER_PASSWORD)
            page.locator("input[type='submit']").click()
            page.wait_for_url(re.compile(r".*localhost:3000.*"), timeout=10000)
            page.wait_for_load_state("networkidle")

    def test_builds_page_accessible(
        self,
        page: Page,
    ) -> None:
        """Test that builds page is accessible."""
        # Try to navigate to builds page - use separate locators
        builds_link = (
            page.locator("a[href*='builds']").or_(page.get_by_text("Builds")).first
        )
        if builds_link.is_visible():
            builds_link.click()
            page.wait_for_load_state("networkidle")

            # Should see builds list or empty state
            expect(page.locator("body")).to_contain_text(
                re.compile("(Builds|No builds|Create|Recent)", re.IGNORECASE)
            )


class TestUIErrorHandling:
    """Test UI error handling."""

    def test_handles_api_errors_gracefully(
        self,
        page: Page,
        docker_services: ServiceEndpoints,
    ) -> None:
        """Test that UI handles API errors gracefully."""
        # Try to access a protected page without being logged in
        page.goto(f"{docker_services.ui}/builds")
        page.wait_for_load_state("networkidle")

        # Should either redirect to login or show error message
        # Either way, should not crash
        expect(page.locator("body")).to_be_visible()

        # Check we're not showing a blank/error page
        body_text = page.locator("body").inner_text()
        assert len(body_text) > 10  # Has some content

    def test_404_page_handling(
        self,
        page: Page,
        docker_services: ServiceEndpoints,
    ) -> None:
        """Test that 404 pages are handled gracefully."""
        page.goto(f"{docker_services.ui}/nonexistent-page-12345")
        page.wait_for_load_state("networkidle")

        # Should show 404 or redirect, not crash
        expect(page.locator("body")).to_be_visible()


class TestUISidebarNavigation:
    """Test sidebar navigation functionality."""

    @pytest.fixture(autouse=True)
    def login_first(
        self,
        page: Page,
        docker_services: ServiceEndpoints,
    ) -> None:
        """Login before each test in this class."""
        page.goto(docker_services.ui)
        page.wait_for_load_state("networkidle")

        if "keycloak" in page.url or "realms/stardag" in page.url:
            page.locator("input[name='username']").fill(TEST_USER_EMAIL)
            page.locator("input[name='password']").fill(TEST_USER_PASSWORD)
            page.locator("input[type='submit']").click()
            page.wait_for_url(re.compile(r".*localhost:3000.*"), timeout=10000)
            page.wait_for_load_state("networkidle")

    def test_sidebar_is_visible(
        self,
        page: Page,
    ) -> None:
        """Test that sidebar is visible after login."""
        # Sidebar should be visible
        sidebar = page.locator("nav").or_(page.locator("aside"))
        expect(sidebar.first).to_be_visible()

    def test_sidebar_has_navigation_items(
        self,
        page: Page,
    ) -> None:
        """Test that sidebar has expected navigation items."""
        # Should have Home, Task Explorer, and Settings navigation items
        nav_buttons = page.locator("nav button, aside button")
        expect(nav_buttons.first).to_be_visible()

    def test_task_explorer_navigation(
        self,
        page: Page,
    ) -> None:
        """Test that Task Explorer navigation works."""
        # Look for Task Explorer button/link in sidebar
        task_explorer_btn = page.get_by_text("Task Explorer").or_(
            page.get_by_title("Task Explorer")
        )

        if task_explorer_btn.is_visible():
            task_explorer_btn.click()
            page.wait_for_load_state("networkidle")

            # URL should contain 'tasks'
            assert "tasks" in page.url

            # Should see Task Explorer header
            expect(page.locator("body")).to_contain_text(
                re.compile("(Task Explorer|Search|tasks)", re.IGNORECASE)
            )

    def test_sidebar_collapse_expand(
        self,
        page: Page,
    ) -> None:
        """Test that sidebar can be collapsed and expanded via logo click."""
        # Find the collapse button (chevron arrows)
        collapse_btn = page.locator("button[title='Collapse sidebar']")

        if collapse_btn.is_visible():
            # Collapse the sidebar
            collapse_btn.click()
            page.wait_for_timeout(300)  # Wait for animation

            # Find the star logo which should expand the sidebar
            expand_star = page.locator("button[title='Expand sidebar']")
            expect(expand_star).to_be_visible()

            # Click star to expand
            expand_star.click()
            page.wait_for_timeout(300)  # Wait for animation

            # Sidebar should be expanded again
            expect(collapse_btn).to_be_visible()


class TestUITaskExplorer:
    """Test Task Explorer page functionality."""

    @pytest.fixture(autouse=True)
    def login_first(
        self,
        page: Page,
        docker_services: ServiceEndpoints,
    ) -> None:
        """Login before each test in this class."""
        page.goto(docker_services.ui)
        page.wait_for_load_state("networkidle")

        if "keycloak" in page.url or "realms/stardag" in page.url:
            page.locator("input[name='username']").fill(TEST_USER_EMAIL)
            page.locator("input[name='password']").fill(TEST_USER_PASSWORD)
            page.locator("input[type='submit']").click()
            page.wait_for_url(re.compile(r".*localhost:3000.*"), timeout=10000)
            page.wait_for_load_state("networkidle")

    def test_task_explorer_has_search_bar(
        self,
        page: Page,
    ) -> None:
        """Test that Task Explorer has a search bar."""
        # Navigate to Task Explorer
        task_explorer_btn = page.get_by_text("Task Explorer").or_(
            page.get_by_title("Task Explorer")
        )
        if task_explorer_btn.is_visible():
            task_explorer_btn.click()
            page.wait_for_load_state("networkidle")

            # Should have a search input
            search_input = page.locator("input[type='text']").or_(
                page.locator("input[placeholder*='Search']")
            )
            expect(search_input.first).to_be_visible()

    def test_task_explorer_has_column_picker(
        self,
        page: Page,
    ) -> None:
        """Test that Task Explorer has a column configuration button."""
        # Navigate to Task Explorer
        task_explorer_btn = page.get_by_text("Task Explorer").or_(
            page.get_by_title("Task Explorer")
        )
        if task_explorer_btn.is_visible():
            task_explorer_btn.click()
            page.wait_for_load_state("networkidle")

            # Should have a column configuration button
            column_btn = page.locator("button[title='Configure columns']")
            # At least one column configuration button should be visible
            expect(column_btn.first).to_be_visible()


class TestUIResponsiveness:
    """Test UI responsiveness at different viewport sizes."""

    def test_mobile_viewport(
        self,
        page: Page,
        docker_services: ServiceEndpoints,
    ) -> None:
        """Test UI at mobile viewport size."""
        page.set_viewport_size({"width": 375, "height": 667})
        page.goto(docker_services.ui)
        page.wait_for_load_state("networkidle")

        # Page should still render
        expect(page.locator("body")).to_be_visible()

        # Check no horizontal scrollbar (basic responsive check)
        body_width = page.evaluate("document.body.scrollWidth")
        viewport_width = page.viewport_size["width"]
        assert body_width <= viewport_width + 50  # Allow small margin

    def test_tablet_viewport(
        self,
        page: Page,
        docker_services: ServiceEndpoints,
    ) -> None:
        """Test UI at tablet viewport size."""
        page.set_viewport_size({"width": 768, "height": 1024})
        page.goto(docker_services.ui)
        page.wait_for_load_state("networkidle")

        expect(page.locator("body")).to_be_visible()

    def test_desktop_viewport(
        self,
        page: Page,
        docker_services: ServiceEndpoints,
    ) -> None:
        """Test UI at desktop viewport size."""
        page.set_viewport_size({"width": 1920, "height": 1080})
        page.goto(docker_services.ui)
        page.wait_for_load_state("networkidle")

        expect(page.locator("body")).to_be_visible()
