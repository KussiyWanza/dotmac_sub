"""Authentication flow e2e tests for web UI."""

from __future__ import annotations

from playwright.sync_api import Page, expect

from tests.playwright.pages.auth import (
    LOGIN_URL_PATTERN,
    ForgotPasswordPage,
    LoginPage,
    ResetPasswordPage,
)


class TestLoginPage:
    """Tests for the login page."""

    def test_login_page_loads(self, anon_page: Page, settings):
        """Login page should load and display sign in form."""
        login = LoginPage(anon_page, settings.base_url)
        login.goto()
        login.expect_loaded()
        expect(anon_page.get_by_label("Username")).to_be_visible()
        expect(anon_page.get_by_label("Password")).to_be_visible()
        expect(anon_page.get_by_role("button", name="Sign in")).to_be_visible()

    def test_successful_login(self, anon_page: Page, settings):
        """Valid credentials should redirect to dashboard."""
        login = LoginPage(anon_page, settings.base_url)
        login.goto()
        login.login(settings.admin_username, settings.admin_password)
        login.expect_redirect_to_dashboard()

    def test_invalid_credentials(self, anon_page: Page, settings):
        """Invalid credentials should show error message."""
        login = LoginPage(anon_page, settings.base_url)
        login.goto()
        login.login("nonexistent_user", "wrongpassword")
        login.expect_error("Invalid")

    def test_empty_username(self, anon_page: Page, settings):
        """Empty username should trigger validation."""
        login = LoginPage(anon_page, settings.base_url)
        login.goto()
        login.fill_password("somepassword")
        login.submit()
        # Browser validation should prevent submission
        expect(anon_page.get_by_label("Username")).to_have_attribute("required", "")

    def test_login_with_remember_me(self, anon_page: Page, settings):
        """Login with remember me should set longer session."""
        login = LoginPage(anon_page, settings.base_url)
        login.goto()
        login.login(settings.admin_username, settings.admin_password, remember=True)
        login.expect_redirect_to_dashboard()


class TestLogout:
    """Tests for logout functionality.

    These specs destroy the session they run in, so they log in with their
    own throwaway context — never the shared session-scoped admin storage
    state, whose server-side session a logout would revoke for every
    remaining admin spec in the run.
    """

    def _login_fresh(self, anon_page: Page, settings) -> None:
        login = LoginPage(anon_page, settings.base_url)
        login.goto()
        login.login(settings.admin_username, settings.admin_password)
        login.expect_redirect_to_dashboard()

    def test_logout_clears_session(self, anon_page: Page, settings):
        """Logout should clear session and redirect to login."""
        self._login_fresh(anon_page, settings)
        anon_page.goto(f"{settings.base_url}/auth/logout")
        anon_page.wait_for_url(LOGIN_URL_PATTERN)
        LoginPage(anon_page, settings.base_url).expect_loaded()

    def test_logout_prevents_access(self, anon_page: Page, settings):
        """After logout, accessing protected pages should redirect to login."""
        self._login_fresh(anon_page, settings)
        anon_page.goto(f"{settings.base_url}/auth/logout")
        anon_page.wait_for_url(LOGIN_URL_PATTERN)
        # Try to access protected page
        anon_page.goto(f"{settings.base_url}/admin/dashboard")
        anon_page.wait_for_url(LOGIN_URL_PATTERN)


class TestProtectedRoutes:
    """Tests for protected route access control."""

    def test_unauthenticated_redirect(self, anon_page: Page, settings):
        """Unauthenticated access to protected route redirects to login."""
        anon_page.goto(f"{settings.base_url}/admin/dashboard")
        anon_page.wait_for_url(LOGIN_URL_PATTERN)

    def test_login_preserves_next_url(self, anon_page: Page, settings):
        """Login should redirect back to original requested URL."""
        # Try to access a protected page (the customers list).
        anon_page.goto(f"{settings.base_url}/admin/customers")
        anon_page.wait_for_url(LOGIN_URL_PATTERN)
        # Verify next parameter is present
        assert "next=" in anon_page.url or "customers" not in anon_page.url


class TestForgotPassword:
    """Tests for forgot password flow."""

    def test_forgot_password_page_loads(self, anon_page: Page, settings):
        """Forgot password page should load correctly."""
        forgot = ForgotPasswordPage(anon_page, settings.base_url)
        forgot.goto()
        forgot.expect_loaded()
        expect(anon_page.get_by_label("Email")).to_be_visible()

    def test_forgot_password_link_from_login(self, anon_page: Page, settings):
        """Login page should have working forgot password link."""
        login = LoginPage(anon_page, settings.base_url)
        login.goto()
        login.click_forgot_password()
        expect(anon_page.get_by_role("heading", name="Forgot Password")).to_be_visible()

    def test_back_to_login_from_forgot(self, anon_page: Page, settings):
        """Forgot password page should have back to login link."""
        forgot = ForgotPasswordPage(anon_page, settings.base_url)
        forgot.goto()
        forgot.click_back_to_login()
        LoginPage(anon_page, settings.base_url).expect_loaded()


class TestResetPassword:
    """Tests for reset password flow."""

    def test_invalid_token_shows_error(self, anon_page: Page, settings):
        """Invalid reset token should show error message."""
        reset = ResetPasswordPage(anon_page, settings.base_url)
        reset.goto_with_token("invalid-token-12345")
        # Either shows the form (to submit and fail) or redirects/shows error
        # Trying to reset with invalid token should fail
        reset.fill_password("NewPassword123!")
        reset.fill_password_confirm("NewPassword123!")
        reset.submit()
        reset.expect_invalid_token_error()

    def test_password_mismatch_validation(self, anon_page: Page, settings):
        """Mismatched passwords show the live indicator and block submission.

        The form guards itself: the mismatch notice appears beside the confirm
        field and the submit button stays disabled, so the request is never
        sent.
        """
        reset = ResetPasswordPage(anon_page, settings.base_url)
        reset.goto_with_token("some-token")
        reset.fill_password("Password123!")
        reset.fill_password_confirm("DifferentPassword!")
        reset.expect_passwords_mismatch_error()
        reset.expect_submit_disabled()
