"""Forgot password page object."""

from __future__ import annotations

from playwright.sync_api import Page, expect

from tests.playwright.pages.base_page import BasePage


class ForgotPasswordPage(BasePage):
    """Page object for the forgot password page."""

    def __init__(self, page: Page, base_url: str) -> None:
        super().__init__(page, base_url)

    def goto(self, path: str = "/auth/forgot-password") -> None:
        """Navigate to the forgot password page."""
        super().goto(path)

    def expect_loaded(self) -> None:
        """Assert the forgot password page is loaded."""
        expect(
            self.page.get_by_role("heading", name="Forgot password?", exact=True)
        ).to_be_visible()

    def fill_email(self, email: str) -> None:
        """Fill the email field."""
        self.page.get_by_label("Email").fill(email)

    def submit(self) -> None:
        """Click the send reset link button."""
        self.page.get_by_role("button", name="Send Reset Link").click()

    def request_reset(self, email: str) -> None:
        """Request a password reset."""
        self.fill_email(email)
        self.submit()

    def expect_success_message(self, message: str = "check your email") -> None:
        """Assert the success message is displayed."""
        expect(self.page.get_by_text(message)).to_be_visible()

    def click_back_to_login(self) -> None:
        """Click the back to login link."""
        self.page.get_by_role("link", name="Back to sign in").click()
