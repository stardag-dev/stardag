"""Frontend browser tests using Playwright.

These tests verify the React UI works correctly with real backend services.
Run with: pytest -m browser
Requires: uv sync --extra browser && uv run playwright install chromium

Performance optimization: Login happens once per session via auth_storage_state fixture.
All tests that need authentication use logged_in_page which reuses the stored session.
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


class TestUILogin:
    """Test the login flow in the UI (these don't use logged_in_page)."""

    def test_login_page_loads(
        self,
        page: Page,
        docker_services: ServiceEndpoints,
    ) -> None:
        """Test that the login page loads and shows Keycloak login."""
        page.goto(docker_services.ui)
        page.wait_for_load_state("networkidle")

        # Check if we're on Keycloak or the main app
        url = page.url
        if "keycloak" in url or "realms/stardag" in url:
            # On Keycloak login page
            expect(page.locator("input[name='username']")).to_be_visible()
            expect(page.locator("input[name='password']")).to_be_visible()
        else:
            # On main app - might show login button or be logged in
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
            page.locator("input[name='username']").fill(TEST_USER_EMAIL)
            page.locator("input[name='password']").fill(TEST_USER_PASSWORD)
            submit_btn = page.locator(
                "input[type='submit'], button[type='submit'], #kc-login"
            )
            submit_btn.first.click()

            page.wait_for_url(
                re.compile(f".*{re.escape('localhost:3000')}.*"), timeout=10000
            )
            page.wait_for_load_state("networkidle")

            expect(page.locator("body")).to_contain_text(
                re.compile("(Dashboard|Builds|Environments|testuser)", re.IGNORECASE)
            )


class TestUINavigation:
    """Test basic UI navigation after login."""

    def test_main_page_loads_and_navigation_works(
        self,
        logged_in_page: Page,
        docker_services: ServiceEndpoints,
    ) -> None:
        """Test that main page loads after login and navigation works."""
        logged_in_page.goto(docker_services.ui)
        logged_in_page.wait_for_load_state("networkidle")

        # Should be on main app
        expect(logged_in_page.locator("body")).to_be_visible()
        assert (
            logged_in_page.title()
            or logged_in_page.locator("main, div#root, div[class*='app']").is_visible()
        )

        # Test navigation links work
        nav_links = logged_in_page.locator("nav a, header a, aside a")
        if nav_links.count() > 0:
            first_link = nav_links.first
            first_link.click()
            logged_in_page.wait_for_load_state("networkidle")
            expect(logged_in_page.locator("body")).to_be_visible()


class TestUIBuildsPage:
    """Test the builds page functionality."""

    def test_builds_page_accessible(
        self,
        logged_in_page: Page,
        docker_services: ServiceEndpoints,
    ) -> None:
        """Test that builds page is accessible."""
        logged_in_page.goto(docker_services.ui)
        logged_in_page.wait_for_load_state("networkidle")

        builds_link = (
            logged_in_page.locator("a[href*='builds']")
            .or_(logged_in_page.get_by_text("Builds"))
            .first
        )
        if builds_link.is_visible():
            builds_link.click()
            logged_in_page.wait_for_load_state("networkidle")

            expect(logged_in_page.locator("body")).to_contain_text(
                re.compile("(Builds|No builds|Create|Recent)", re.IGNORECASE)
            )


class TestUIErrorHandling:
    """Test UI error handling (don't need login for these)."""

    def test_handles_api_errors_gracefully(
        self,
        page: Page,
        docker_services: ServiceEndpoints,
    ) -> None:
        """Test that UI handles API errors gracefully."""
        page.goto(f"{docker_services.ui}/builds")
        page.wait_for_load_state("networkidle")

        expect(page.locator("body")).to_be_visible()
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

        expect(page.locator("body")).to_be_visible()


class TestUISidebarNavigation:
    """Test sidebar navigation functionality."""

    def _go_to_home(self, page: Page, docker_services: ServiceEndpoints) -> None:
        """Navigate to home page and wait for sidebar."""
        page.goto(docker_services.ui)
        page.wait_for_load_state("networkidle")
        page.locator("button[title='Collapse sidebar']").wait_for(
            state="visible", timeout=10000
        )

    def test_sidebar_functionality(
        self,
        logged_in_page: Page,
        docker_services: ServiceEndpoints,
    ) -> None:
        """Test sidebar visibility, navigation items, and collapse/expand."""
        self._go_to_home(logged_in_page, docker_services)

        # Test sidebar is visible
        collapse_btn = logged_in_page.locator("button[title='Collapse sidebar']")
        expect(collapse_btn).to_be_visible()

        # Test navigation items exist
        # Scoped to the sidebar button: the breadcrumb also reads "Builds",
        # so a bare text locator matches two elements and trips strict mode.
        builds_btn = logged_in_page.locator("button[title='Builds']")
        expect(builds_btn).to_be_visible()

        # Test collapse/expand
        collapse_btn.click()
        logged_in_page.wait_for_timeout(300)

        expand_star = logged_in_page.locator("button[title='Expand sidebar']")
        expect(expand_star).to_be_visible()

        expand_star.click()
        logged_in_page.wait_for_timeout(300)
        expect(collapse_btn).to_be_visible()

    # test_task_explorer_navigation, TestUITaskExplorer (5 tests) and
    # TestUIDAGPanel (2 tests) are deleted: the v2 UI has no Task Explorer
    # page (no "Task Explorer" nav button, route, search bar or DAG panel
    # hosted there -- confirmed absent from app/stardag-ui/src). This
    # matches plan.md's status section, which lists task search/explorer
    # among what v2 "removed for want of a v2 route" and is not scheduled
    # to come back under I10. DAG viewing moved to the build page instead
    # (TestUIBuildViewDAG below, `#build-dag-panel`), which already covers
    # the toggle/expand behavior these tests exercised.


class TestUIBuildViewDAG:
    """Test DAG panel functionality in Build View."""

    def _go_to_home(self, page: Page, docker_services: ServiceEndpoints) -> None:
        """Navigate to home page."""
        page.goto(docker_services.ui)
        page.wait_for_load_state("networkidle")
        page.locator("button[title='Collapse sidebar']").wait_for(
            state="visible", timeout=10000
        )

    def test_build_page_dag_functionality(
        self,
        logged_in_page: Page,
        docker_services: ServiceEndpoints,
    ) -> None:
        """Test DAG toggle exists on build page and collapse/expand works."""
        self._go_to_home(logged_in_page, docker_services)

        # Navigate to the builds list (sidebar button — see above on why this
        # is not a text locator).
        logged_in_page.locator("button[title='Builds']").click()
        logged_in_page.wait_for_load_state("networkidle")

        # Check if there are any builds to click on
        build_rows = logged_in_page.locator("tr").filter(
            has=logged_in_page.locator("td")
        )
        if build_rows.count() > 0:
            build_rows.first.click()
            logged_in_page.wait_for_load_state("networkidle")
            logged_in_page.wait_for_timeout(500)

            # Should have a DAG View disclosure toggle.
            #
            # This block asserted on button text ("Click to expand" /
            # "Click to collapse") that has never existed in this UI — the
            # toggle expresses its state with a rotated chevron and the
            # panel's presence. It went unnoticed because the guard above
            # matched nothing while the builds list was a list of buttons
            # rather than a table, so the body never ran. It runs now, and
            # asserts against aria-expanded, which is both the accessible
            # contract and a stable hook.
            dag_button = logged_in_page.locator(
                "button[aria-controls='build-dag-panel']"
            )
            expect(dag_button).to_be_visible()
            expect(dag_button).to_have_attribute("aria-expanded", "true")
            expect(logged_in_page.locator("#build-dag-panel")).to_be_visible()

            dag_button.click()
            expect(dag_button).to_have_attribute("aria-expanded", "false")
            expect(logged_in_page.locator("#build-dag-panel")).to_have_count(0)

            dag_button.click()
            expect(dag_button).to_have_attribute("aria-expanded", "true")
            expect(logged_in_page.locator("#build-dag-panel")).to_be_visible()


class TestUIResponsiveness:
    """Test UI responsiveness at different viewport sizes."""

    def test_responsive_viewports(
        self,
        page: Page,
        docker_services: ServiceEndpoints,
    ) -> None:
        """Test UI renders correctly at mobile, tablet, and desktop viewports."""
        viewports = [
            {"width": 375, "height": 667, "name": "mobile"},
            {"width": 768, "height": 1024, "name": "tablet"},
            {"width": 1920, "height": 1080, "name": "desktop"},
        ]

        for viewport in viewports:
            page.set_viewport_size(
                {"width": viewport["width"], "height": viewport["height"]}
            )
            page.goto(docker_services.ui)
            page.wait_for_load_state("networkidle")

            expect(page.locator("body")).to_be_visible()

            # Check no significant horizontal overflow for mobile
            if viewport["name"] == "mobile":
                body_width = page.evaluate("document.body.scrollWidth")
                viewport_width = viewport["width"]
                assert body_width <= viewport_width + 50
