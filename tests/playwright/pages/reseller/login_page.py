"""Reseller portal login page object."""

from __future__ import annotations

from playwright.sync_api import Page, expect

from tests.playwright.pages.base_page import BasePage


class ResellerLoginPage(BasePage):
    """Page object for the reseller portal login page."""

    def __init__(self, page: Page, base_url: str) -> None:
        super().__init__(page, base_url)

    def goto(self, path: str = "/reseller/auth/login") -> None:
        """Navigate to the reseller login page."""
        super().goto(path)

    def expect_loaded(self) -> None:
        """Assert the login page is loaded."""
        expect(
            self.page.get_by_role("heading", name="Login", exact=True)
            .or_(
                self.page.get_by_label("Email").or_(self.page.get_by_label("Username"))
            )
            .first
        ).to_be_visible()

    def fill_email(self, email: str) -> None:
        """Fill email field."""
        self.page.get_by_label("Email").or_(
            self.page.get_by_label("Username")
        ).first.fill(email)

    def fill_password(self, password: str) -> None:
        """Fill password field."""
        # "Password" also substring-matches the visibility-toggle button's
        # aria-label, so target the field itself.
        self.page.locator("input#password").fill(password)

    def click_login(self) -> None:
        """Click login button."""
        self.page.get_by_role("button", name="Login").or_(
            self.page.get_by_role("button", name="Sign In")
        ).first.click()

    def login(self, email: str, password: str) -> None:
        """Perform full login."""
        self.fill_email(email)
        self.fill_password(password)
        self.click_login()

    def expect_error(self, message: str) -> None:
        """Assert an error message is displayed."""
        expect(self.page.get_by_text(message)).to_be_visible()

    def expect_login_success(self) -> None:
        """Assert login was successful."""
        self.page.wait_for_url("**/reseller/dashboard**")
