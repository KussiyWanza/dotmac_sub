"""Permission and access control e2e tests."""

from __future__ import annotations

from playwright.sync_api import Page, expect

from tests.playwright.helpers.api import api_get, api_post_json, bearer_headers
from tests.playwright.pages.auth import LOGIN_URL_PATTERN


class TestAPICredentialPermissions:
    """Tests for credential management permissions via API."""

    def test_admin_can_manage_credentials(self, api_context, admin_token: str):
        """Admin should have access to user credentials."""
        response = api_get(
            api_context,
            "/api/v1/user-credentials?limit=1",
            headers=bearer_headers(admin_token),
        )
        assert response.status == 200

    def test_agent_cannot_manage_credentials(self, api_context, agent_token: str):
        """Support agent should not have access to user credentials."""
        response = api_get(
            api_context,
            "/api/v1/user-credentials?limit=1",
            headers=bearer_headers(agent_token),
        )
        assert response.status == 403

    def test_user_cannot_manage_credentials(self, api_context, user_token: str):
        """Regular user should not have access to user credentials."""
        response = api_get(
            api_context,
            "/api/v1/user-credentials?limit=1",
            headers=bearer_headers(user_token),
        )
        assert response.status == 403


class TestImpersonationPermissions:
    """Tests for customer impersonation permissions."""

    def test_admin_can_impersonate_customer(self, admin_impersonate_response):
        """Admin should be able to impersonate customers."""
        assert admin_impersonate_response.status == 303

    def test_agent_cannot_impersonate_customer(self, agent_impersonate_response):
        """Support agent should not be able to impersonate customers."""
        assert agent_impersonate_response.status == 403


class TestRoleManagementPermissions:
    """Tests for RBAC role management permissions."""

    def test_admin_can_list_roles(self, api_context, admin_token: str):
        """Admin should be able to list roles."""
        response = api_get(
            api_context,
            "/api/v1/rbac/roles?limit=10",
            headers=bearer_headers(admin_token),
        )
        assert response.status == 200

    def test_agent_cannot_create_role(self, api_context, agent_token: str):
        """Support agent should not be able to create roles."""
        response = api_post_json(
            api_context,
            "/api/v1/rbac/roles",
            {"name": "test_role", "display_name": "Test Role"},
            headers=bearer_headers(agent_token),
        )
        assert response.status == 403

    def test_user_cannot_list_roles(self, api_context, user_token: str):
        """Regular user should not be able to list roles."""
        response = api_get(
            api_context,
            "/api/v1/rbac/roles?limit=10",
            headers=bearer_headers(user_token),
        )
        assert response.status == 403


class TestSubscriberPermissions:
    """Tests for subscriber management permissions."""

    def test_admin_can_list_subscribers(self, api_context, admin_token: str):
        """Admin should be able to list subscribers."""
        response = api_get(
            api_context,
            "/api/v1/subscribers?limit=10",
            headers=bearer_headers(admin_token),
        )
        assert response.status == 200

    def test_agent_can_list_subscribers(self, api_context, agent_token: str):
        """Support agent should be able to list subscribers."""
        response = api_get(
            api_context,
            "/api/v1/subscribers?limit=10",
            headers=bearer_headers(agent_token),
        )
        assert response.status == 200

    def test_user_cannot_list_subscribers(self, api_context, user_token: str):
        """Regular user should not be able to list subscribers."""
        response = api_get(
            api_context,
            "/api/v1/subscribers?limit=10",
            headers=bearer_headers(user_token),
        )
        assert response.status == 403


class TestAuditLogPermissions:
    """Tests for audit log access permissions."""

    def test_admin_can_view_audit_logs(self, api_context, admin_token: str):
        """Admin should be able to view audit logs."""
        response = api_get(
            api_context,
            "/api/v1/audit-events?limit=10",
            headers=bearer_headers(admin_token),
        )
        assert response.status == 200

    def test_agent_cannot_view_audit_logs(self, api_context, agent_token: str):
        """Support agent should not be able to view audit logs."""
        response = api_get(
            api_context,
            "/api/v1/audit-events?limit=10",
            headers=bearer_headers(agent_token),
        )
        assert response.status == 403


class TestUnauthenticatedAccess:
    """Tests for unauthenticated request handling."""

    def test_unauthenticated_api_request_rejected(self, api_context):
        """API requests without authentication should be rejected."""
        response = api_get(api_context, "/api/v1/subscribers?limit=1")
        assert response.status == 401

    def test_unauthenticated_admin_page_redirects(self, anon_page: Page, settings):
        """Unauthenticated access to admin pages should redirect to login."""
        anon_page.goto(f"{settings.base_url}/admin/dashboard")
        anon_page.wait_for_url(LOGIN_URL_PATTERN)


class TestWebPortalAccess:
    """Tests for web portal access control."""

    def test_admin_can_access_dashboard(self, admin_page: Page, settings):
        """Admin should be able to access dashboard."""
        admin_page.goto(f"{settings.base_url}/admin/dashboard")
        expect(
            admin_page.get_by_role("heading", name="Operations Overview")
        ).to_be_visible()

    def test_admin_can_access_tickets(self, admin_page: Page, settings):
        """Admin should be able to access tickets page."""
        # /admin/tickets is retired (404); the canonical path is
        # /admin/support/tickets.
        admin_page.goto(f"{settings.base_url}/admin/support/tickets")
        expect(
            admin_page.get_by_role("heading", name="Support Tickets")
        ).to_be_visible()

    def test_agent_can_access_dashboard(self, agent_page: Page, settings):
        """Support agent should be able to access dashboard."""
        agent_page.goto(f"{settings.base_url}/admin/dashboard")
        expect(
            agent_page.get_by_role("heading", name="Operations Overview")
        ).to_be_visible()
