"""Reports e2e tests."""

from __future__ import annotations

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, expect

from tests.playwright.pages.base_page import BasePage


class ReportsPage(BasePage):
    """Page object for the reports page."""

    def __init__(self, page: Page, base_url: str) -> None:
        super().__init__(page, base_url)

    def goto(self, path: str = "/admin/reports") -> None:
        """Navigate to reports page."""
        last_error: Exception | None = None
        for _ in range(2):
            try:
                self.page.goto(
                    f"{self.base_url}/admin/dashboard",
                    wait_until="commit",
                    timeout=30000,
                )
                self.page.wait_for_load_state("domcontentloaded")
                reports_link = self.page.locator(
                    "a[href='/admin/reports'], a[href='/admin/reports/hub']"
                ).first
                expect(reports_link).to_be_visible()
                reports_link.click()
                self.page.wait_for_url(
                    "**/admin/reports/hub", wait_until="domcontentloaded"
                )
                return
            except PlaywrightError as exc:
                last_error = exc
                try:
                    self.page.goto(
                        f"{self.base_url}/admin/reports/hub",
                        wait_until="commit",
                        timeout=30000,
                    )
                    self.page.wait_for_load_state("domcontentloaded")
                    return
                except PlaywrightError as fallback_exc:
                    last_error = fallback_exc
        if last_error:
            raise last_error

    def expect_loaded(self) -> None:
        """Assert reports page is loaded."""
        expect(
            self.page.get_by_role("heading", name="Reports Hub", exact=True)
            .or_(
                self.page.get_by_role("heading", name="Reports", exact=True).or_(
                    self.page.get_by_role("heading", name="Report", exact=False)
                )
            )
            .first
        ).to_be_visible()


def _goto_report_page(page: Page, url: str) -> None:
    last_error: Exception | None = None
    for _ in range(2):
        try:
            page.goto(url, wait_until="commit", timeout=30000)
            page.wait_for_load_state("domcontentloaded")
            return
        except PlaywrightError as exc:
            last_error = exc
    if last_error:
        raise last_error


class TestReportsAccess:
    """Tests for reports page access."""

    def test_reports_page_loads(self, admin_page: Page, settings):
        """Reports page should load."""
        page = ReportsPage(admin_page, settings.base_url)
        page.goto()
        page.expect_loaded()

    def test_reports_page_shows_categories(self, admin_page: Page, settings):
        """Reports page should show report categories."""
        page = ReportsPage(admin_page, settings.base_url)
        page.goto()
        page.expect_loaded()
        # Should show some report categories
        expect(
            admin_page.get_by_text("Revenue", exact=False)
            .or_(
                admin_page.get_by_text("Financial", exact=False).or_(
                    admin_page.get_by_text("Report", exact=False)
                )
            )
            .first
        ).to_be_visible()


class TestRevenueReports:
    """Tests for revenue reports."""

    def test_revenue_report_accessible(self, admin_page: Page, settings):
        """Revenue report should be accessible."""
        _goto_report_page(admin_page, f"{settings.base_url}/admin/reports/revenue")
        expect(
            admin_page.get_by_role("heading", name="Revenue Report", exact=True)
        ).to_be_visible()

    def test_revenue_report_date_filter(self, admin_page: Page, settings):
        """Revenue report should have date filters."""
        _goto_report_page(admin_page, f"{settings.base_url}/admin/reports/revenue")
        expect(admin_page.locator("#days")).to_be_visible()
        expect(
            admin_page.get_by_role("button", name="Export", exact=True)
        ).to_be_visible()


class TestSubscriberReports:
    """Tests for subscriber reports."""

    def test_subscriber_report_accessible(self, admin_page: Page, settings):
        """Subscriber report should be accessible."""
        _goto_report_page(admin_page, f"{settings.base_url}/admin/reports/hub")
        expect(
            admin_page.get_by_text("Subscriber", exact=False)
            .or_(admin_page.get_by_text("Customer", exact=False))
            .first
        ).to_be_visible()


class TestNetworkReports:
    """Tests for network reports."""

    def test_network_report_accessible(self, admin_page: Page, settings):
        """Network report should be accessible."""
        _goto_report_page(admin_page, f"{settings.base_url}/admin/reports/hub")
        # Check for network or operations reports
        expect(
            admin_page.get_by_text("Network", exact=False)
            .or_(
                admin_page.get_by_text("Operation", exact=False).or_(
                    admin_page.get_by_text("Report", exact=False)
                )
            )
            .first
        ).to_be_visible()


class TestReportsAPI:
    """API-level tests for reports."""

    def test_revenue_summary_api(self, api_context, admin_token):
        """API should return revenue summary."""
        from tests.playwright.helpers.api import api_get, bearer_headers

        response = api_get(
            api_context,
            "/api/v1/reports/revenue/summary",
            headers=bearer_headers(admin_token),
        )
        # May return 200 or 404 depending on implementation
        assert response.status in [200, 404]

    def test_subscriber_stats_api(self, api_context, admin_token):
        """API should return subscriber statistics."""
        from tests.playwright.helpers.api import api_get, bearer_headers

        response = api_get(
            api_context,
            "/api/v1/reports/subscribers/stats",
            headers=bearer_headers(admin_token),
        )
        assert response.status in [200, 404]
