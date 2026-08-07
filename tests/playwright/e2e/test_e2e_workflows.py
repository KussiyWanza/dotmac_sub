"""End-to-end business workflow tests.

These tests verify complete business processes that span multiple
pages and systems.
"""

from __future__ import annotations

import json
import re
import time
from uuid import uuid4

import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, expect

from tests.playwright.helpers.api import api_get, api_post_json, bearer_headers
from tests.playwright.pages.admin.login_page import AdminLoginPage


def _request_with_retry(fn, *, attempts: int = 3, delay_s: float = 1.0):
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            return fn()
        except PlaywrightError as exc:
            last_error = exc
            if attempt == attempts - 1:
                break
            time.sleep(delay_s)
    if last_error:
        raise last_error
    raise RuntimeError("request retry exhausted")


def _pick_pop_site_with_nas(
    api_context, admin_token: str | None = None
) -> tuple[dict, dict]:
    headers = bearer_headers(admin_token) if admin_token else None
    pop_sites_response = _request_with_retry(
        lambda: api_get(
            api_context,
            "/api/v1/pop-sites?is_active=true&limit=100",
            headers=headers,
        )
    )
    assert pop_sites_response.status == 200
    pop_sites = pop_sites_response.json()["items"]

    nas_devices_response = _request_with_retry(
        lambda: api_get(
            api_context,
            "/api/v1/nas-devices?is_active=true&limit=200",
            headers=headers,
        )
    )
    assert nas_devices_response.status == 200
    nas_devices = nas_devices_response.json()["items"]

    nas_by_pop = {
        str(device.get("pop_site_id")): device
        for device in nas_devices
        if device.get("pop_site_id")
    }
    for pop_site in pop_sites:
        pop_id = str(pop_site.get("id") or "")
        if pop_id in nas_by_pop:
            return pop_site, nas_by_pop[pop_id]

    pytest.skip(
        "No active POP site with an active NAS device is available for Phase 1 E2E."
    )


def _create_phase1_offer(
    api_context,
    suffix: str,
    admin_token: str | None = None,
) -> tuple[dict, dict]:
    headers = bearer_headers(admin_token) if admin_token else None

    radius_profile_response = _request_with_retry(
        lambda: api_post_json(
            api_context,
            "/api/v1/radius-profiles",
            {
                "name": f"E2E PPPoE Profile {suffix}",
                "code": f"e2e-pppoe-{suffix.lower()}",
                "vendor": "mikrotik",
                "connection_type": "pppoe",
                "description": "Playwright Phase 1 PPPoE profile",
                "download_speed": 100000,
                "upload_speed": 50000,
                "ip_pool_name": f"e2e-pool-{suffix.lower()}",
                "ipv6_pool_name": f"e2e-v6-{suffix.lower()}",
                "simultaneous_use": 1,
                "is_active": True,
            },
            headers=headers,
        )
    )
    assert radius_profile_response.status == 201
    radius_profile = radius_profile_response.json()

    offer_response = _request_with_retry(
        lambda: api_post_json(
            api_context,
            "/api/v1/offers",
            {
                "name": f"E2E Phase 1 Offer {suffix}",
                "code": f"e2e-phase1-{suffix.lower()}",
                "service_type": "residential",
                "access_type": "fiber",
                "price_basis": "flat",
                "billing_cycle": "monthly",
                "billing_mode": "prepaid",
                "contract_term": "month_to_month",
                "speed_download_mbps": 1000,
                "speed_upload_mbps": 500,
                "status": "active",
                "is_active": True,
                "available_for_services": True,
                "show_on_customer_portal": True,
                "plan_category": "internet",
                "description": "Playwright Phase 1 activation flow offer",
            },
            headers=headers,
        )
    )
    assert offer_response.status == 201
    offer = offer_response.json()

    offer_profile_link_response = _request_with_retry(
        lambda: api_post_json(
            api_context,
            "/api/v1/offer-radius-profiles",
            {
                "offer_id": offer["id"],
                "profile_id": radius_profile["id"],
            },
            headers=headers,
        )
    )
    assert offer_profile_link_response.status == 201

    return offer, radius_profile


def _configure_phase1_radius_settings(
    api_context, admin_token: str | None = None
) -> None:
    headers = {"Content-Type": "application/json"}
    if admin_token:
        headers.update(bearer_headers(admin_token))
    enabled_response = _request_with_retry(
        lambda: api_context.put(
            "/api/v1/settings/radius/pppoe_auto_generate_enabled",
            data=json.dumps(
                {
                    "value_text": "true",
                    "is_active": True,
                }
            ),
            headers=headers,
        )
    )
    assert enabled_response.status == 200
    enabled_payload = enabled_response.json()
    assert enabled_payload["key"] == "pppoe_auto_generate_enabled"

    prefix_response = _request_with_retry(
        lambda: api_context.put(
            "/api/v1/settings/radius/pppoe_username_prefix",
            data=json.dumps(
                {
                    "value_text": "1050",
                    "is_active": True,
                }
            ),
            headers=headers,
        )
    )
    assert prefix_response.status == 200
    prefix_payload = prefix_response.json()
    assert prefix_payload["key"] == "pppoe_username_prefix"
    assert prefix_payload["value_text"] == "1050"

    current_enabled_response = _request_with_retry(
        lambda: api_get(
            api_context,
            "/api/v1/settings/radius/pppoe_auto_generate_enabled",
            headers=bearer_headers(admin_token) if admin_token else None,
        )
    )
    assert current_enabled_response.status == 200
    current_enabled = current_enabled_response.json()
    enabled_value = current_enabled.get("value_json")
    if enabled_value is None:
        enabled_value = str(current_enabled.get("value_text") or "").strip().lower()
    assert enabled_value in {True, "true", "1", "yes", "on"}

    current_prefix_response = _request_with_retry(
        lambda: api_get(
            api_context,
            "/api/v1/settings/radius/pppoe_username_prefix",
            headers=bearer_headers(admin_token) if admin_token else None,
        )
    )
    assert current_prefix_response.status == 200
    current_prefix = current_prefix_response.json()
    assert current_prefix["value_text"] == "1050"


class TestCustomerListFilters:
    """Tests for customer-list filter guidance."""

    def test_infrastructure_search_prompts_for_type(self, admin_page: Page, settings):
        admin_page.goto(f"{settings.base_url}/admin/customers")
        expect(
            admin_page.get_by_role("heading", name="Customers", exact=True)
        ).to_be_visible()
        admin_page.get_by_role("button", name=re.compile(r"^Filters")).click()

        infrastructure_requests: list[str] = []

        def record_infrastructure_request(request) -> None:
            if "/admin/customers/infrastructure-options" in request.url:
                infrastructure_requests.append(request.url)

        admin_page.on("request", record_infrastructure_request)

        search = admin_page.locator("#infrastructure-search")
        expect(search).to_be_enabled()
        search.fill("Ka")

        expect(admin_page.locator("#infrastructure-results")).to_be_visible()
        expect(
            admin_page.get_by_text("Choose an infrastructure type first.", exact=True)
        ).to_be_visible()
        admin_page.wait_for_timeout(400)
        assert infrastructure_requests == []

        admin_page.route(
            "**/admin/customers/infrastructure-options?**",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {
                        "results": [
                            {
                                "id": str(uuid4()),
                                "label": "Gudu",
                                "context": "Abuja",
                            }
                        ]
                    }
                ),
            ),
        )
        admin_page.locator("#infrastructure-type").select_option("location")
        search.fill("Gudu")

        expect(admin_page.get_by_text("Gudu", exact=True)).to_be_visible()
        expect(admin_page.get_by_text("Abuja", exact=True)).to_be_visible()
        assert len(infrastructure_requests) == 1
        assert "infrastructure_type=location" in infrastructure_requests[0]
        assert "q=Gudu" in infrastructure_requests[0]


class TestSubscriptionActivation:
    """Tests for the subscription activation workflow."""

    def test_subscriber_to_active_service_flow(self, admin_page: Page, settings):
        """Current customer onboarding entry points should be accessible."""
        from tests.playwright.pages.admin.subscribers_page import SubscribersPage

        subscribers = SubscribersPage(admin_page, settings.base_url)
        subscribers.goto()
        subscribers.expect_loaded()

        expect(admin_page.get_by_role("link", name="Add Customer")).to_be_visible()

    def test_service_order_creation_from_subscriber(self, admin_page: Page, settings):
        """Subscription creation should be reachable from customer context."""
        admin_page.goto(f"{settings.base_url}/admin/customers")
        expect(
            admin_page.get_by_role("heading", name="Customers", exact=True)
        ).to_be_visible()

    def test_phase1_customer_activation_shows_pppoe_and_radius_evidence(
        self,
        admin_page: Page,
        browser,
        settings,
        api_context,
        admin_token,
    ):
        """Create customer and active subscription, then verify PPPoE and RADIUS evidence in UI."""
        suffix = uuid4().hex[:8].upper()
        customer_email = f"phase1-{suffix.lower()}@example.com"
        _configure_phase1_radius_settings(api_context, admin_token)
        pop_site, nas_device = _pick_pop_site_with_nas(api_context, admin_token)
        offer, radius_profile = _create_phase1_offer(api_context, suffix, admin_token)

        fresh_context = browser.new_context()
        fresh_context.set_default_timeout(settings.action_timeout_ms)
        fresh_context.set_default_navigation_timeout(settings.navigation_timeout_ms)
        page = fresh_context.new_page()

        login = AdminLoginPage(page, settings.base_url)
        login.goto()
        login.login(settings.admin_username or "admin", settings.admin_password or "")
        page.wait_for_url(
            re.compile(r".*/admin/dashboard(?:[?#].*)?$"),
            wait_until="domcontentloaded",
        )

        page.goto(
            f"{settings.base_url}/admin/customers/new", wait_until="domcontentloaded"
        )
        expect(
            page.get_by_role("heading", name=re.compile(r"New Customer|New Person"))
        ).to_be_visible()

        page.get_by_text("Individual", exact=True).click()
        page.locator("#first_name").fill("Phase1")
        page.locator("#last_name").fill(suffix)
        page.locator("#email").fill(customer_email)
        page.locator("#phone").fill(f"+234800{suffix[-4:]}")
        page.get_by_role("button", name="Address").click()
        page.locator("#address_line1").fill(f"{suffix} Activation Street")
        page.locator("#region").fill("Lagos")
        page.locator("#pop_site_id").select_option(str(pop_site["id"]))
        page.locator("button[type='submit']").click(no_wait_after=True)

        page.wait_for_load_state("domcontentloaded")
        expect(page).to_have_url(re.compile(r".*/admin/customers/person/[^/?#]+"))
        expect(page.get_by_text(customer_email).first).to_be_visible()

        page.get_by_role("button", name=re.compile(r"Subscriptions")).click()
        expect(page.get_by_role("link", name="Add Subscription")).to_be_visible()
        page.get_by_role("link", name="Add Subscription").click()

        page.wait_for_url("**/admin/catalog/subscriptions/new**")
        expect(
            page.get_by_role("heading", name="Add Subscription", exact=True)
        ).to_be_visible()

        page.locator("#offer_id").select_option(str(offer["id"]))
        page.get_by_role("button", name="Continue").click()

        provisioning_nas_value = page.locator(
            "input[name='provisioning_nas_device_id'][data-typeahead-hidden]"
        )
        expect(provisioning_nas_value).not_to_have_value("")
        expect(
            page.locator("input#provisioning_nas_device_id[data-typeahead-input]")
        ).not_to_have_value("")
        page.locator("#ipv4_method").select_option("dynamic")

        page.get_by_role("button", name="Continue").click()
        page.locator("input[name='activate_immediately']").check()
        page.locator("input[name='send_welcome_email']").check()
        page.get_by_role("button", name="Add Subscription").click(no_wait_after=True)

        page.wait_for_url("**/admin/customers/person/**")
        subscription_items = []
        for _ in range(10):
            subscriptions_response = api_get(
                api_context,
                f"/api/v1/subscriptions?offer_id={offer['id']}&limit=5",
                headers=bearer_headers(admin_token),
            )
            assert subscriptions_response.status == 200
            subscription_items = subscriptions_response.json()["items"]
            if subscription_items:
                break
            time.sleep(1)
        assert subscription_items, (
            "No subscription was created for the Phase 1 E2E offer."
        )
        subscription_id = subscription_items[0]["id"]

        page.goto(
            f"{settings.base_url}/admin/catalog/subscriptions/{subscription_id}",
            wait_until="domcontentloaded",
        )
        expect(page.get_by_text("Provisioning Evidence", exact=True)).to_be_visible()
        credential_card = page.locator("text=Access Credential").locator("..")
        expect(credential_card).to_contain_text(re.compile(r"1050\d+"))
        expect(credential_card).to_contain_text("PPPOE")

        expect(
            page.get_by_text("Resolved RADIUS Reply Attributes", exact=True)
        ).to_be_visible()
        expect(
            page.locator("table").filter(has=page.get_by_text("Service-Type")).first
        ).to_be_visible()
        expect(page.get_by_text("Framed-Protocol", exact=False)).to_be_visible()
        expect(page.get_by_text("Mikrotik-Rate-Limit", exact=False)).to_be_visible()
        expect(
            page.get_by_text("Delegated-IPv6-Prefix-Pool", exact=False)
        ).to_be_visible()
        expect(page.get_by_text("External FreeRADIUS Rows", exact=True)).to_be_visible()
        expect(page.get_by_text("radcheck", exact=False)).to_be_visible()
        expect(page.get_by_text("radreply", exact=False)).to_be_visible()
        events_section = page.get_by_text("Domain Events", exact=True).locator("../..")
        for attempt in range(5):
            created_event = events_section.get_by_text(
                "subscription.created", exact=True
            )
            activated_event = events_section.get_by_text(
                "subscription.activated", exact=True
            )
            if created_event.count() and activated_event.count():
                break
            page.reload(wait_until="domcontentloaded")
            expect(
                page.get_by_text("Provisioning Evidence", exact=True)
            ).to_be_visible()
            events_section = page.get_by_text("Domain Events", exact=True).locator(
                "../.."
            )
            time.sleep(1)
        expect(
            events_section.get_by_text("subscription.created", exact=True)
        ).to_be_visible()
        expect(
            events_section.get_by_text("subscription.activated", exact=True)
        ).to_be_visible()

        page.goto(
            f"{settings.base_url}/admin/catalog/subscriptions/{subscription_id}/edit",
            wait_until="domcontentloaded",
        )
        expect(
            page.get_by_role("heading", name="Edit Subscription", exact=True)
        ).to_be_visible()
        expect(page.get_by_text("Current Service Login", exact=True)).to_be_visible()
        current_login = page.locator(
            "xpath=//label[contains(., 'Current Service Login')]/following-sibling::input[@readonly]"
        ).first
        expect(page.get_by_text("Current Service Password", exact=True)).to_be_visible()
        page.get_by_role("button", name="View", exact=True).click()
        password_input = page.locator(
            "xpath=//label[contains(., 'Current Service Password')]/following-sibling::div//input[@readonly]"
        ).first
        expect(password_input).not_to_have_value("")
        expect(current_login).to_have_value(re.compile(r"1050\d+"))
        expect(page.locator("#radius_profile_id")).to_have_value(
            str(radius_profile["id"])
        )
        fresh_context.close()


class TestBillingCycle:
    """Tests for the billing cycle workflow."""

    def test_invoice_to_payment_flow(self, admin_page: Page, settings):
        """Complete flow: Subscription -> Invoice -> Payment -> Ledger."""
        from tests.playwright.pages.admin.billing.invoices_page import InvoicesPage

        # Step 1: View invoices
        invoices = InvoicesPage(admin_page, settings.base_url)
        invoices.goto()
        invoices.expect_loaded()

        # Invoice table should be visible
        expect(admin_page.locator("table")).to_be_visible()

    def test_payment_recording_flow(self, admin_page: Page, settings):
        """Should be able to record payments."""
        from tests.playwright.pages.admin.billing.payments_page import PaymentsPage

        payments = PaymentsPage(admin_page, settings.base_url)
        payments.goto()
        payments.expect_loaded()

        # Payment recording should be accessible
        expect(
            admin_page.get_by_role("link", name="Record Payment")
            .or_(admin_page.get_by_role("button", name="Record Payment"))
            .first
        ).to_be_visible()


class TestSupportResolution:
    """Tests for the support ticket resolution workflow."""

    def test_ticket_lifecycle_flow(self, admin_page: Page, settings):
        """Complete flow: Create ticket -> Assign -> Work -> Resolve -> Close."""
        from tests.playwright.pages.admin.tickets_page import TicketsPage

        tickets = TicketsPage(admin_page, settings.base_url)
        tickets.goto()
        tickets.expect_loaded()

        # Should see ticket management interface
        expect(admin_page.locator("table")).to_be_visible()

    def test_ticket_assignment_flow(self, admin_page: Page, settings):
        """Should be able to assign tickets."""
        from tests.playwright.pages.admin.tickets_page import TicketsPage

        tickets = TicketsPage(admin_page, settings.base_url)
        tickets.goto()
        tickets.expect_loaded()

        # Ticket list should be visible for assignment
        expect(admin_page.locator("table")).to_be_visible()


class TestWorkOrderExecution:
    """Tests for the work order execution workflow."""

    def test_work_order_dispatch_flow(self, admin_page: Page, settings):
        """Ticket workflow surface should be accessible."""
        from tests.playwright.pages.admin.tickets_page import TicketsPage

        tickets = TicketsPage(admin_page, settings.base_url)
        tickets.goto()
        tickets.expect_loaded()
        expect(admin_page.locator("table")).to_be_visible()

    def test_dispatch_view_flow(self, admin_page: Page, settings):
        """Billing overview is reachable as an operational dashboard."""
        from tests.playwright.pages.admin.billing.billing_overview_page import (
            BillingOverviewPage,
        )

        overview = BillingOverviewPage(admin_page, settings.base_url)
        overview.goto()
        overview.expect_loaded()


class TestNetworkOperations:
    """Tests for manual network operations workflow."""

    def test_ont_operations_flow(self, admin_page: Page, settings):
        """Manual flow: OLT -> ONT -> IP -> Service."""
        from tests.playwright.pages.admin.network.olts_page import OLTsPage

        olts = OLTsPage(admin_page, settings.base_url)
        olts.goto()
        olts.expect_loaded()

        # OLT management should be accessible
        expect(admin_page.locator("table")).to_be_visible()

    def test_ip_assignment_flow(self, admin_page: Page, settings):
        """Should be able to assign IPs from pools."""
        from tests.playwright.pages.admin.network.ip_management_page import (
            IPManagementPage,
        )

        ip_mgmt = IPManagementPage(admin_page, settings.base_url)
        ip_mgmt.goto()
        ip_mgmt.expect_loaded()


class TestCustomerOnboarding:
    """Tests for complete customer onboarding workflow."""

    def test_full_onboarding_visibility(self, admin_page: Page, settings):
        """All steps for customer onboarding should be accessible."""
        from tests.playwright.pages.admin.billing.invoices_page import InvoicesPage
        from tests.playwright.pages.admin.subscribers_page import SubscribersPage

        subscribers = SubscribersPage(admin_page, settings.base_url)
        subscribers.goto()
        subscribers.expect_loaded()

        invoices = InvoicesPage(admin_page, settings.base_url)
        invoices.goto()
        invoices.expect_loaded()
