"""Reset password page object."""

from __future__ import annotations

from playwright.sync_api import Page, expect

from tests.playwright.pages.base_page import BasePage


class ResetPasswordPage(BasePage):
    """Page object for the reset password page."""

    def __init__(self, page: Page, base_url: str) -> None:
        super().__init__(page, base_url)

    def goto(self, path: str = "/auth/reset-password") -> None:
        """Navigate to the reset password page."""
        super().goto(path)

    def goto_with_token(self, token: str) -> None:
        """Navigate to the reset password page with token."""
        self.goto(f"/auth/reset-password?token={token}")

    def expect_loaded(self) -> None:
        """Assert the reset password page is loaded."""
        expect(
            self.page.get_by_role("heading", name="Set new password", exact=True)
        ).to_be_visible()

    def fill_password(self, password: str) -> None:
        """Fill the new password field."""
        self.page.locator("input[name='password']").first.fill(password)

    def fill_password_confirm(self, password: str) -> None:
        """Fill the confirm password field."""
        self.page.locator("input[name='password_confirm']").fill(password)

    def submit(self) -> None:
        """Click the reset password button."""
        self.page.get_by_role("button", name="Reset Password").click()

    def reset_password(self, password: str) -> None:
        """Reset password with the given new password."""
        self.fill_password(password)
        self.fill_password_confirm(password)
        self.submit()

    def expect_error(self, message: str) -> None:
        """Assert an error message is displayed."""
        error_locator = self.page.locator(".text-red-700, .text-red-200").first
        expect(error_locator).to_contain_text(message)

    def expect_submit_disabled(self) -> None:
        """Assert the form refuses submission (weak or mismatched password)."""
        expect(self.page.get_by_role("button", name="Reset password")).to_be_disabled()

    def expect_passwords_mismatch_error(self) -> None:
        """Assert the live mismatch indicator (rendered beside the confirm
        field, not in the flash-error container)."""
        expect(self.page.get_by_text("Passwords do not match")).to_be_visible()

    def expect_invalid_token_error(self) -> None:
        """Assert invalid or expired token error."""
        self.expect_error("Invalid or expired")

    def expect_redirect_to_login(self) -> None:
        """Assert redirect to login page after successful reset."""
        self.page.wait_for_url("**/auth/login**")
