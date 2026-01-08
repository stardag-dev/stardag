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
                re.compile("(Dashboard|Builds|Workspaces|testuser)", re.IGNORECASE)
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
        home_btn = logged_in_page.get_by_text("Home", exact=True)
        expect(home_btn).to_be_visible()

        # Test collapse/expand
        collapse_btn.click()
        logged_in_page.wait_for_timeout(300)

        expand_star = logged_in_page.locator("button[title='Expand sidebar']")
        expect(expand_star).to_be_visible()

        expand_star.click()
        logged_in_page.wait_for_timeout(300)
        expect(collapse_btn).to_be_visible()

    def test_task_explorer_navigation(
        self,
        logged_in_page: Page,
        docker_services: ServiceEndpoints,
    ) -> None:
        """Test that Task Explorer navigation works."""
        self._go_to_home(logged_in_page, docker_services)

        task_explorer_btn = logged_in_page.get_by_text("Task Explorer").or_(
            logged_in_page.get_by_title("Task Explorer")
        )

        if task_explorer_btn.is_visible():
            task_explorer_btn.click()
            logged_in_page.wait_for_load_state("networkidle")

            assert "tasks" in logged_in_page.url
            expect(logged_in_page.locator("body")).to_contain_text(
                re.compile("(Task Explorer|Search|tasks)", re.IGNORECASE)
            )


class TestUITaskExplorer:
    """Test Task Explorer page functionality."""

    def _navigate_to_task_explorer(
        self, page: Page, docker_services: ServiceEndpoints
    ) -> None:
        """Helper to navigate to Task Explorer page."""
        page.goto(docker_services.ui)
        page.wait_for_load_state("networkidle")
        page.locator("button[title='Collapse sidebar']").wait_for(
            state="visible", timeout=10000
        )

        task_explorer_btn = page.get_by_text("Task Explorer").or_(
            page.get_by_title("Task Explorer")
        )
        task_explorer_btn.click()
        page.wait_for_load_state("networkidle")

        page.locator("h1:has-text('Task Explorer')").wait_for(
            state="visible", timeout=5000
        )

    def test_task_explorer_basic_elements(
        self,
        logged_in_page: Page,
        docker_services: ServiceEndpoints,
    ) -> None:
        """Test Task Explorer has search bar and column picker."""
        self._navigate_to_task_explorer(logged_in_page, docker_services)

        # Test search bar exists
        search_input = logged_in_page.locator("input[type='text']").or_(
            logged_in_page.locator("input[placeholder*='Search']")
        )
        expect(search_input.first).to_be_visible()

        # Test column picker exists
        column_btn = logged_in_page.locator("button[title='Manage columns']")
        expect(column_btn.first).to_be_visible()

    def test_search_key_autocomplete(
        self,
        logged_in_page: Page,
        docker_services: ServiceEndpoints,
    ) -> None:
        """Test that typing in search shows key autocomplete suggestions."""
        self._navigate_to_task_explorer(logged_in_page, docker_services)

        search_input = logged_in_page.locator("input[placeholder*='Search']")
        search_input.click()
        search_input.fill("task")
        logged_in_page.wait_for_timeout(500)

        # Should show autocomplete with "Keys" header and task_name option
        expect(logged_in_page.locator("text=Keys")).to_be_visible()
        task_name_option = logged_in_page.get_by_role(
            "button", name="task_name", exact=True
        )
        expect(task_name_option).to_be_visible()

    def test_search_keyboard_and_operator_autocomplete(
        self,
        logged_in_page: Page,
        docker_services: ServiceEndpoints,
    ) -> None:
        """Test keyboard navigation and operator autocomplete in search."""
        self._navigate_to_task_explorer(logged_in_page, docker_services)

        search_input = logged_in_page.locator("input[placeholder*='Search']")
        search_input.click()
        search_input.fill("stat")
        logged_in_page.wait_for_timeout(500)

        expect(logged_in_page.locator("text=Keys")).to_be_visible()

        # Keyboard navigation - select and confirm
        search_input.press("ArrowDown")
        logged_in_page.wait_for_timeout(100)
        search_input.press("Enter")
        logged_in_page.wait_for_timeout(300)

        # Should now show operators
        expect(logged_in_page.locator("text=Operators")).to_be_visible()
        expect(logged_in_page.get_by_role("button", name="= equals")).to_be_visible()
        expect(
            logged_in_page.get_by_role("button", name="!= not equals")
        ).to_be_visible()

    def test_search_add_filter_via_space_syntax(
        self,
        logged_in_page: Page,
        docker_services: ServiceEndpoints,
    ) -> None:
        """Test adding a filter using space-based syntax."""
        self._navigate_to_task_explorer(logged_in_page, docker_services)

        search_input = logged_in_page.locator("input[placeholder*='Search']")
        search_input.click()
        search_input.fill("status = completed")
        search_input.press("Enter")
        logged_in_page.wait_for_timeout(500)

        # Should show filter chip
        filter_chip = logged_in_page.locator("button:has-text('status')").first
        expect(filter_chip).to_be_visible()

        # Search bar should be cleared
        expect(search_input).to_have_value("")

    def test_search_results_preserved_during_composition(
        self,
        logged_in_page: Page,
        docker_services: ServiceEndpoints,
    ) -> None:
        """Test that search results are preserved while composing a filter."""
        self._navigate_to_task_explorer(logged_in_page, docker_services)
        logged_in_page.wait_for_timeout(1000)

        has_results = logged_in_page.locator("table").is_visible()
        initial_state = "results" if has_results else "empty"

        search_input = logged_in_page.locator("input[placeholder*='Search']")
        search_input.click()
        search_input.fill("status ")
        logged_in_page.wait_for_timeout(500)

        # Results should not disappear while composing
        if initial_state == "results":
            expect(logged_in_page.locator("table")).to_be_visible()
        else:
            expect(logged_in_page.locator("text=No tasks found")).to_be_visible()


class TestUIDAGPanel:
    """Test DAG panel functionality in Task Explorer."""

    def _navigate_to_task_explorer(
        self, page: Page, docker_services: ServiceEndpoints
    ) -> None:
        """Helper to navigate to Task Explorer page."""
        page.goto(docker_services.ui)
        page.wait_for_load_state("networkidle")
        page.locator("button[title='Collapse sidebar']").wait_for(
            state="visible", timeout=10000
        )

        task_explorer_btn = page.get_by_text("Task Explorer").or_(
            page.get_by_title("Task Explorer")
        )
        task_explorer_btn.click()
        page.wait_for_load_state("networkidle")
        page.locator("h1:has-text('Task Explorer')").wait_for(
            state="visible", timeout=5000
        )

    def test_dag_panel_elements_and_toggle(
        self,
        logged_in_page: Page,
        docker_services: ServiceEndpoints,
    ) -> None:
        """Test DAG panel has toggle button, resize handle, and toggle works."""
        self._navigate_to_task_explorer(logged_in_page, docker_services)

        # Test DAG button exists
        dag_button = logged_in_page.get_by_text("DAG View", exact=False)
        expect(dag_button).to_be_visible()

        # Test resize handle exists
        resize_handle = logged_in_page.locator(".cursor-row-resize")
        expect(resize_handle.first).to_be_visible()

        # Test toggle works (check chevron changes)
        dag_button = logged_in_page.locator("button:has-text('DAG View')")
        chevron = dag_button.locator("svg").first
        expect(chevron).to_be_visible()

        is_enabled = dag_button.is_enabled()
        if is_enabled:
            dag_button.click()
            logged_in_page.wait_for_timeout(300)
            expect(dag_button).to_be_visible()
        else:
            # Verify disabled state shows reason
            dag_text = dag_button.inner_text()
            assert any(
                msg in dag_text
                for msg in [
                    "(No tasks)",
                    "(No build associated)",
                    "builds - select a single build",
                    "(Limit: 100 tasks)",
                ]
            ), f"Disabled DAG button should show reason, got: {dag_text}"

    def test_dag_panel_disabled_message_when_no_single_build(
        self,
        logged_in_page: Page,
        docker_services: ServiceEndpoints,
    ) -> None:
        """Test that DAG panel shows disabled message when multiple builds."""
        self._navigate_to_task_explorer(logged_in_page, docker_services)

        dag_section = logged_in_page.locator("button:has-text('DAG View')")
        expect(dag_section).to_be_visible()

        dag_text = dag_section.inner_text()
        assert (
            "Click to" in dag_text
            or "(No tasks)" in dag_text
            or "(No build associated)" in dag_text
            or "builds - select a single build" in dag_text
            or "(Limit: 100 tasks)" in dag_text
        ), f"DAG section should show state indicator, got: {dag_text}"


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

        # Navigate to Home (builds list)
        logged_in_page.get_by_text("Home", exact=True).click()
        logged_in_page.wait_for_load_state("networkidle")

        # Check if there are any builds to click on
        build_rows = logged_in_page.locator("tr").filter(
            has=logged_in_page.locator("td")
        )
        if build_rows.count() > 0:
            build_rows.first.click()
            logged_in_page.wait_for_load_state("networkidle")
            logged_in_page.wait_for_timeout(500)

            # Should have DAG View toggle button
            dag_button = logged_in_page.get_by_text("DAG View", exact=False)
            expect(dag_button).to_be_visible()

            # Test collapse/expand
            dag_button = logged_in_page.locator("button:has-text('DAG View')")
            if dag_button.is_visible():
                dag_button.click()
                logged_in_page.wait_for_timeout(300)

                collapsed_text = dag_button.inner_text()
                assert "Click to expand" in collapsed_text, (
                    f"After collapse, expected 'Click to expand', got: {collapsed_text}"
                )

                dag_button.click()
                logged_in_page.wait_for_timeout(300)

                expanded_text = dag_button.inner_text()
                assert "Click to collapse" in expanded_text, (
                    f"After expand, expected 'Click to collapse', got: {expanded_text}"
                )


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
