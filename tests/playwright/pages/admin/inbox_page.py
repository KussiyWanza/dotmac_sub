"""Admin team-inbox workspace page object."""

from __future__ import annotations

from playwright.sync_api import Page, expect

from tests.playwright.pages.base_page import BasePage


class AdminInboxPage(BasePage):
    """The three-pane inbox workspace at /admin/inbox."""

    def __init__(self, page: Page, base_url: str) -> None:
        super().__init__(page, base_url)

    def goto(self, path: str = "/admin/inbox") -> None:
        self.page.goto(
            f"{self.base_url}{path}",
            wait_until="domcontentloaded",
            timeout=30000,
        )

    def expect_loaded(self) -> None:
        expect(self.page.locator("[data-inbox-workspace]")).to_be_visible()

    def expect_sidebar(self) -> None:
        expect(self.page.locator("[data-inbox-sidebar-content]")).to_be_visible()

    # --- filters --------------------------------------------------------

    def apply_query_filter(self, query: str) -> None:
        """Filters round-trip through the URL, so drive them that way."""
        self.goto(f"/admin/inbox?{query}")

    def conversation_rows(self):
        return self.page.locator("[data-conversation-id]")

    # --- mailbox routing ------------------------------------------------

    def goto_email_routes(self) -> None:
        self.goto("/admin/inbox/settings/email-routes")

    def expect_email_routes_loaded(self) -> None:
        expect(
            self.page.get_by_role("heading", name="Mailbox routing", exact=True)
        ).to_be_visible()

    # --- responsive -----------------------------------------------------

    def triage_mode(self) -> str | None:
        return self.page.locator("[data-triage-shell]").get_attribute(
            "data-triage-mode"
        )
