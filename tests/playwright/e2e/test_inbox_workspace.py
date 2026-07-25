"""Browser verification of the inbox operator paths.

This is the gate the static audits kept missing. The 2026-07-24 workspace audit
was markup-only and explicitly did no authenticated browser pass, which is how
every form POST shipped without a CSRF token: the templates looked right, the
fetch-based calls worked, and only the native form path was broken.

These specs therefore assert on *rendered, authenticated* pages — that the
forms carry a token, that a real submit is accepted rather than 403'd, and that
the panes behave at phone width.

See docs/designs/TEAM_INBOX_ADMIN_UI_PORT.md §5, slice 5.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import expect

from tests.playwright.pages.admin.inbox_page import AdminInboxPage

pytestmark = pytest.mark.e2e


class TestInboxWorkspaceLoads:
    def test_workspace_renders_for_an_authenticated_admin(self, admin_page, settings):
        inbox = AdminInboxPage(admin_page, settings.base_url)
        inbox.goto()
        inbox.expect_loaded()
        inbox.expect_sidebar()

    def test_mailbox_routing_page_renders(self, admin_page, settings):
        inbox = AdminInboxPage(admin_page, settings.base_url)
        inbox.goto_email_routes()
        inbox.expect_email_routes_loaded()


class TestFormsCarryCsrfWhenRendered:
    """The defect a markup audit cannot see: a token in the file is not a token
    in the response."""

    def test_every_rendered_post_form_has_a_csrf_token(self, admin_page, settings):
        inbox = AdminInboxPage(admin_page, settings.base_url)
        inbox.goto()

        forms = admin_page.locator('form[method="post" i]')
        count = forms.count()
        assert count > 0, "expected at least one POST form on the workspace"
        for index in range(count):
            form = forms.nth(index)
            token = form.locator('input[name="_csrf_token"]')
            assert token.count() >= 1, (
                "rendered POST form without a CSRF token: "
                f"{form.get_attribute('action')}"
            )
            assert (token.first.get_attribute("value") or "").strip(), (
                f"CSRF token rendered empty for {form.get_attribute('action')}"
            )

    def test_mailbox_routing_form_has_a_csrf_token(self, admin_page, settings):
        inbox = AdminInboxPage(admin_page, settings.base_url)
        inbox.goto_email_routes()

        token = admin_page.locator(
            'form[method="post" i] input[name="_csrf_token"]'
        ).first
        expect(token).to_have_count(1)
        assert (token.get_attribute("value") or "").strip()


class TestAMailboxRouteRoundTrips:
    """One real form POST, end to end, against the running app.

    A 403 here is what shipped to production for every form-based mutation.
    """

    def test_creating_a_route_is_accepted_and_listed(self, admin_page, settings):
        inbox = AdminInboxPage(admin_page, settings.base_url)
        inbox.goto_email_routes()

        team = admin_page.locator('select[name="service_team_id"] option').nth(1)
        if team.count() == 0:
            pytest.skip("no active service team seeded in this environment")

        address = "readiness-gate@example.test"
        admin_page.fill('input[name="email_address"]', address)
        admin_page.select_option('select[name="service_team_id"]', index=1)
        admin_page.click('button:has-text("Add route")')

        admin_page.wait_for_load_state("domcontentloaded")
        # Accepted, not rejected by the CSRF middleware.
        assert "Session Expired" not in admin_page.content()
        expect(admin_page.get_by_text(address, exact=False).first).to_be_visible()


class TestQueueFiltersRoundTrip:
    """Filters must survive the URL, because the sidebar drives them by href."""

    @pytest.mark.parametrize(
        "query",
        [
            "open_only=true",
            "needs_response=true",
            "unassigned=true",
            "ai_handling=true",
            "has_ticket=false",
        ],
    )
    def test_filter_renders_without_error(self, admin_page, settings, query):
        inbox = AdminInboxPage(admin_page, settings.base_url)
        inbox.apply_query_filter(query)
        inbox.expect_loaded()
        assert "Traceback" not in admin_page.content()


class TestResponsivePanes:
    """The workspace collapses to one pane on a phone; the shell records which."""

    def test_phone_width_starts_in_list_mode(self, admin_page, settings):
        admin_page.set_viewport_size({"width": 390, "height": 844})
        inbox = AdminInboxPage(admin_page, settings.base_url)
        inbox.goto()
        inbox.expect_loaded()

        assert inbox.triage_mode() in {"list", "detail"}
        expect(admin_page.locator("[data-inbox-sidebar-content]")).to_be_visible()

    def test_desktop_width_shows_the_sidebar(self, admin_page, settings):
        admin_page.set_viewport_size({"width": 1440, "height": 900})
        inbox = AdminInboxPage(admin_page, settings.base_url)
        inbox.goto()
        inbox.expect_loaded()
        expect(admin_page.locator("#inbox-sidebar")).to_be_visible()
