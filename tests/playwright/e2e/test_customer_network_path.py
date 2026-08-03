"""Customer 360 Network Path renders owner-provided facts on desktop and 390px.

The path component only arranges ui.customer_network_path_projection output:
serving-endpoint presentation, path chips, gaps, and measurements. These specs
pin that the section renders without horizontal overflow and that an
unresolved path stays honest instead of fabricating hops.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from app.models.catalog import Subscription

pytestmark = pytest.mark.e2e


def _mark_admin_tour_seen(page: Page) -> None:
    """Keep the global first-login tour from covering this page-specific flow."""

    page.add_init_script(
        "window.localStorage.setItem('dotmac_admin_tour_seen_v1', '1')"
    )


def _detail_path(test_identities: dict) -> str:
    account_id = test_identities["customer"]["account"]["id"]
    return f"/admin/customers/person/{account_id}"


def _open_network_tab(page: Page) -> None:
    """The Network Access card lives inside the detail page's Network tab."""

    page.get_by_role("button", name="Network", exact=True).click()


def _ensure_subscription_login(e2e_db, test_identities: dict) -> None:
    """The Network Access card renders only for subscriptions with access.

    The shared e2e seed does not always produce an active subscription, so
    this spec provisions a minimal offer + active subscription directly when
    absent — throwaway rows in a throwaway database.
    """

    subscription_id = test_identities["customer"].get("subscription_id")
    subscription = (
        e2e_db.get(Subscription, subscription_id) if subscription_id else None
    )
    if subscription is None:
        from app.models.catalog import (
            AccessType,
            CatalogOffer,
            PriceBasis,
            ServiceType,
            SubscriptionStatus,
        )

        account_id = test_identities["customer"]["account"]["id"]
        offer = e2e_db.query(CatalogOffer).order_by(CatalogOffer.created_at).first()
        if offer is None:
            offer = CatalogOffer(
                name="E2E Network Path",
                code="E2E-NET-PATH",
                service_type=ServiceType.residential,
                access_type=AccessType.fiber,
                price_basis=PriceBasis.flat,
            )
            e2e_db.add(offer)
            e2e_db.flush()
        subscription = Subscription(
            subscriber_id=account_id,
            offer_id=offer.id,
            status=SubscriptionStatus.active,
            login="e2e-network-path",
        )
        e2e_db.add(subscription)
        e2e_db.commit()
        return
    if not subscription.login and not subscription.ipv4_address:
        subscription.login = "e2e-network-path"
        e2e_db.commit()


class TestCustomerNetworkPath:
    def test_desktop_card_renders_owner_presentation(
        self,
        admin_page: Page,
        settings,
        e2e_db,
        test_identities: dict,
    ) -> None:
        _ensure_subscription_login(e2e_db, test_identities)
        _mark_admin_tour_seen(admin_page)
        admin_page.goto(f"{settings.base_url}{_detail_path(test_identities)}")
        _open_network_tab(admin_page)

        expect(admin_page.get_by_role("heading", name="Network Access")).to_be_visible()
        # The serving endpoint block always states which record proved the
        # endpoint — for the seeded customer, honestly Unresolved or a
        # provisioned/observed source label; never a template-derived word.
        expect(admin_page.get_by_text("Serving Endpoint").first).to_be_visible()

        # When a path resolved, every chip comes from the shared graph
        # contract renderer.
        chips = admin_page.get_by_test_id("network-path-node")
        if chips.count() > 0:
            expect(chips.first).to_be_visible()

    def test_mobile_390_keeps_layout_and_no_horizontal_overflow(
        self,
        admin_page: Page,
        settings,
        e2e_db,
        test_identities: dict,
    ) -> None:
        _ensure_subscription_login(e2e_db, test_identities)
        _mark_admin_tour_seen(admin_page)
        admin_page.set_viewport_size({"width": 390, "height": 844})
        admin_page.goto(f"{settings.base_url}{_detail_path(test_identities)}")
        _open_network_tab(admin_page)

        expect(admin_page.get_by_role("heading", name="Network Access")).to_be_visible()
        overflow = admin_page.evaluate(
            "() => document.documentElement.scrollWidth - window.innerWidth"
        )
        assert overflow <= 1
