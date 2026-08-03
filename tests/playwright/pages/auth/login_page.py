"""Login page object."""

from __future__ import annotations

import re

from playwright.sync_api import Page, expect

from tests.playwright.pages.base_page import BasePage

# Unauthenticated admin requests land on /auth/login?next=<path>. Playwright's
# URL globs do not match that redirect target — "**/auth/login**" times out
# against a URL carrying a query string — so waits use this regex instead.
LOGIN_URL_PATTERN = re.compile(r"/auth/login")


class LoginPage(BasePage):
    """Page object for the login page."""

    def __init__(self, page: Page, base_url: str) -> None:
        super().__init__(page, base_url)

    def goto(self, path: str = "/auth/login") -> None:
        """Navigate to the login page."""
        super().goto(path)

    def expect_loaded(self) -> None:
        """Assert the login page is loaded."""
        expect(
            self.page.get_by_role("heading", name="Welcome back", exact=True)
        ).to_be_visible()

    def fill_username(self, username: str) -> None:
        """Fill the username field."""
        self.page.get_by_label("Username").fill(username)

    def fill_password(self, password: str) -> None:
        """Fill the password field."""
        self.page.get_by_label("Password").fill(password)

    def check_remember_me(self) -> None:
        """Check the remember me checkbox."""
        self.page.get_by_label("Remember me").check()

    def submit(self) -> None:
        """Click the sign in button."""
        self.page.get_by_role("button", name="Sign in").click()

    def login(self, username: str, password: str, remember: bool = False) -> None:
        """Perform login with username and password."""
        self.fill_username(username)
        self.fill_password(password)
        if remember:
            self.check_remember_me()
        self.submit()

    def expect_error(self, message: str) -> None:
        """Assert an error message is displayed."""
        error_locator = self.page.locator(".text-red-700, .text-red-200").first
        expect(error_locator).to_contain_text(message)

    def expect_redirect_to_dashboard(self) -> None:
        """Assert redirect to admin dashboard after login."""
        self.page.wait_for_url("**/admin/dashboard**")

    def expect_redirect_to_mfa(self) -> None:
        """Assert redirect to MFA page after login."""
        self.page.wait_for_url("**/auth/mfa**")

    def click_forgot_password(self) -> None:
        """Click the forgot password link."""
        self.page.get_by_role("link", name="Forgot password").click()

    def get_next_param(self) -> str | None:
        """Get the 'next' URL parameter if present."""
        url = self.page.url
        if "next=" in url:
            return url.split("next=")[1].split("&")[0]
        return None
