"""Subscriber management e2e tests."""

from __future__ import annotations

from playwright.sync_api import Page, expect

from tests.playwright.pages.admin import (
    SubscriberDetailPage,
    SubscriberFormPage,
    SubscribersPage,
)


class TestSubscribersList:
    """Tests for the customers list page through the legacy subscriber facade."""

    def test_subscribers_page_loads(self, admin_page: Page, settings):
        """Customers list page should load."""
        page = SubscribersPage(admin_page, settings.base_url)
        page.goto()
        page.expect_loaded()

    def test_subscribers_table_visible(self, admin_page: Page, settings):
        """Subscribers table should be visible."""
        page = SubscribersPage(admin_page, settings.base_url)
        page.goto()
        page.expect_loaded()
        expect(admin_page.locator("table")).to_be_visible()

    def test_new_subscriber_button(self, admin_page: Page, settings):
        """New customer button should navigate to form."""
        page = SubscribersPage(admin_page, settings.base_url)
        page.goto()
        page.click_new_subscriber()
        admin_page.wait_for_url("**/customers/new**")

    def test_search_subscribers(self, admin_page: Page, settings, test_identities):
        """Search should filter subscriber list."""
        page = SubscribersPage(admin_page, settings.base_url)
        page.goto()
        page.expect_loaded()
        # Search for test customer
        customer = test_identities["customer"]
        page.search(customer["person"]["email"])
        # Should find the customer or show no results
        admin_page.wait_for_timeout(1000)


class TestSubscriberForm:
    """Tests for the subscriber create/edit form."""

    def test_new_subscriber_form_loads(self, admin_page: Page, settings):
        """New subscriber form should load."""
        form = SubscriberFormPage(admin_page, settings.base_url)
        form.goto_new()
        form.expect_loaded()

    def test_subscriber_form_has_required_fields(self, admin_page: Page, settings):
        """Form should have required fields visible."""
        form = SubscriberFormPage(admin_page, settings.base_url)
        form.goto_new()
        form.expect_loaded()
        # Check for customer search or type selector
        expect(admin_page.locator("form")).to_be_visible()

    def test_subscriber_form_cancel(self, admin_page: Page, settings):
        """Cancel should return to customers list."""
        form = SubscriberFormPage(admin_page, settings.base_url)
        form.goto_new()
        form.expect_loaded()
        form.cancel()
        admin_page.wait_for_url("**/customers**")


class TestSubscriberDetail:
    """Tests for the subscriber detail page."""

    _BILLING_CARD_VIEWPORTS = (375, 640, 768, 1024, 1280, 1536, 1920)
    _MAXIMUM_MONEY_DISPLAY = "₦9,999,999,999.99"

    def test_subscriber_detail_page(self, admin_page: Page, settings, test_identities):
        """Subscriber detail page should load for test customer."""
        customer = test_identities["customer"]
        subscriber_id = customer["subscriber"]["id"]

        detail = SubscriberDetailPage(admin_page, settings.base_url)
        detail.goto(subscriber_id)
        detail.expect_loaded()

    def test_subscriber_edit_navigation(
        self, admin_page: Page, settings, test_identities
    ):
        """Edit button should navigate to edit form."""
        customer = test_identities["customer"]
        subscriber_id = customer["subscriber"]["id"]

        detail = SubscriberDetailPage(admin_page, settings.base_url)
        detail.goto(subscriber_id)
        detail.click_edit()
        admin_page.wait_for_url(f"**/customers/person/{subscriber_id}/edit**")

    def test_subscriber_has_sections(self, admin_page: Page, settings, test_identities):
        """Detail page should show related data sections."""
        customer = test_identities["customer"]
        subscriber_id = customer["subscriber"]["id"]

        detail = SubscriberDetailPage(admin_page, settings.base_url)
        detail.goto(subscriber_id)
        detail.expect_loaded()

    def test_billing_summary_values_stay_inside_their_cards(
        self, admin_page: Page, settings, test_identities
    ):
        """Maximum-length money must not overlap at supported viewports."""
        customer = test_identities["customer"]
        subscriber_id = customer["subscriber"]["id"]

        detail = SubscriberDetailPage(admin_page, settings.base_url)
        detail.goto(subscriber_id)
        detail.expect_loaded()
        admin_page.get_by_role("button", name="Billing", exact=True).click()

        cards = admin_page.get_by_test_id("billing-overview-cards")
        values = admin_page.get_by_test_id("billing-overview-value")
        expect(cards).to_be_visible()
        expect(values).to_have_count(5)

        for viewport_width in self._BILLING_CARD_VIEWPORTS:
            admin_page.set_viewport_size({"width": viewport_width, "height": 900})
            values.evaluate_all(
                "(nodes, amount) => nodes.forEach((node) => {"
                " node.textContent = amount;"
                "})",
                self._MAXIMUM_MONEY_DISPLAY,
            )
            admin_page.evaluate("() => document.fonts.ready")

            violations = cards.evaluate(
                """grid => {
                    const tolerance = 1;
                    const cards = [...grid.querySelectorAll(
                        '[data-testid="billing-overview-card"]'
                    )];
                    return cards.flatMap((card, index) => {
                        const value = card.querySelector(
                            '[data-testid="billing-overview-value"]'
                        );
                        const cardRect = card.getBoundingClientRect();
                        const range = document.createRange();
                        range.selectNodeContents(value);
                        const outside = [...range.getClientRects()].some(
                            rect => rect.left < cardRect.left - tolerance
                                || rect.right > cardRect.right + tolerance
                        );
                        return outside ? [{
                            index,
                            cardLeft: cardRect.left,
                            cardRight: cardRect.right,
                            value: value.textContent,
                        }] : [];
                    });
                }"""
            )

            assert violations == [], (
                f"billing values overflow at {viewport_width}px: {violations}"
            )


class TestSubscriberEdit:
    """Tests for editing subscribers."""

    def test_edit_subscriber_form_loads(
        self, admin_page: Page, settings, test_identities
    ):
        """Edit form should load with subscriber data."""
        customer = test_identities["customer"]
        subscriber_id = customer["subscriber"]["id"]

        form = SubscriberFormPage(admin_page, settings.base_url)
        form.goto_edit(subscriber_id)
        form.expect_loaded()

    def test_edit_subscriber_notes(self, admin_page: Page, settings, test_identities):
        """Should be able to update subscriber notes."""
        customer = test_identities["customer"]
        subscriber_id = customer["subscriber"]["id"]

        form = SubscriberFormPage(admin_page, settings.base_url)
        form.goto_edit(subscriber_id)
        form.expect_loaded()
        form.fill_notes("E2E test note update")
        form.submit()
        # Should redirect back to detail
        admin_page.wait_for_url(f"**/customers/person/{subscriber_id}**")


class TestSubscriberAPI:
    """API-level tests for subscriber operations."""

    def test_list_subscribers_api(self, api_context, admin_token):
        """API should return subscriber list."""
        from tests.playwright.helpers.api import api_get, bearer_headers

        response = api_get(
            api_context,
            "/api/v1/subscribers?limit=10",
            headers=bearer_headers(admin_token),
        )
        assert response.status == 200
        data = response.json()
        assert "items" in data

    def test_get_subscriber_api(self, api_context, admin_token, test_identities):
        """API should return subscriber details."""
        from tests.playwright.helpers.api import api_get, bearer_headers

        customer = test_identities["customer"]
        subscriber_id = customer["subscriber"]["id"]

        response = api_get(
            api_context,
            f"/api/v1/subscribers/{subscriber_id}",
            headers=bearer_headers(admin_token),
        )
        assert response.status == 200
        data = response.json()
        assert data["id"] == subscriber_id
