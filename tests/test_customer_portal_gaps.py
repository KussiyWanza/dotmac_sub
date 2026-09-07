"""Tests for customer portal gap fixes.

Covers: ticket creation, password change, event types, route registration.
"""

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

from app.services.events.types import EventType

# ---------------------------------------------------------------------------
# 1. Customer event types
# ---------------------------------------------------------------------------


class TestCustomerEventTypes:
    def test_customer_login_event(self) -> None:
        assert EventType.customer_login.value == "customer.login"

    def test_customer_logout_event(self) -> None:
        assert EventType.customer_logout.value == "customer.logout"

    def test_customer_ticket_created_event(self) -> None:
        assert EventType.customer_ticket_created.value == "customer.ticket_created"

    def test_customer_password_changed_event(self) -> None:
        assert EventType.customer_password_changed.value == "customer.password_changed"


# ---------------------------------------------------------------------------
# 2. Route registration
# ---------------------------------------------------------------------------


class TestCustomerRouteRegistration:
    def test_support_new_get_route_exists(self) -> None:
        from app.web.customer.routes import router

        found = False
        for route in router.routes:
            if getattr(route, "path", "") == "/portal/support/new" and "GET" in getattr(
                route, "methods", set()
            ):
                found = True
                break
        assert found, "GET /portal/support/new route not found"

    def test_support_new_post_route_exists(self) -> None:
        from app.web.customer.routes import router

        found = False
        for route in router.routes:
            if getattr(
                route, "path", ""
            ) == "/portal/support/new" and "POST" in getattr(route, "methods", set()):
                found = True
                break
        assert found, "POST /portal/support/new route not found"

    def test_password_change_get_route_removed(self) -> None:
        """Password change route must not exist — credentials are shared with PPPoE."""
        from app.web.customer.routes import router

        for route in router.routes:
            if getattr(
                route, "path", ""
            ) == "/portal/profile/password" and "GET" in getattr(
                route, "methods", set()
            ):
                raise AssertionError(
                    "GET /portal/profile/password must not exist (PPPoE credential safety)"
                )

    def test_password_change_post_route_removed(self) -> None:
        """Password change route must not exist — credentials are shared with PPPoE."""
        from app.web.customer.routes import router

        for route in router.routes:
            if getattr(
                route, "path", ""
            ) == "/portal/profile/password" and "POST" in getattr(
                route, "methods", set()
            ):
                raise AssertionError(
                    "POST /portal/profile/password must not exist (PPPoE credential safety)"
                )

    def test_support_info_route_exists(self) -> None:
        """Public support contact page must be accessible without auth."""
        from app.web.customer.auth import router as auth_router

        found = False
        for route in auth_router.routes:
            if getattr(
                route, "path", ""
            ) == "/portal/auth/support-info" and "GET" in getattr(
                route, "methods", set()
            ):
                found = True
                break
        assert found, "GET /portal/auth/support-info route not found"

    def test_support_comment_post_route_exists(self) -> None:
        from app.web.customer.routes import router

        found = False
        for route in router.routes:
            path = getattr(route, "path", "")
            if (
                "/portal/support/" in path
                and "comment" in path
                and "POST" in getattr(route, "methods", set())
            ):
                found = True
                break
        assert found, "POST /portal/support/{ticket}/comment route not found"

    def test_contacts_routes_exist(self) -> None:
        from app.web.customer.routes import router

        routes = {
            (getattr(route, "path", ""), method)
            for route in router.routes
            for method in getattr(route, "methods", set())
        }
        assert ("/portal/contacts", "GET") in routes
        assert ("/portal/contacts", "POST") in routes
        assert ("/portal/contacts/{contact_id}", "POST") in routes

    def test_service_reboot_post_route_exists(self) -> None:
        from app.web.customer.routes import router

        routes = {
            (getattr(route, "path", ""), method)
            for route in router.routes
            for method in getattr(route, "methods", set())
        }
        assert ("/portal/services/{subscription_id}/reboot", "POST") in routes

    def test_service_wifi_post_route_exists(self) -> None:
        from app.web.customer.routes import router

        routes = {
            (getattr(route, "path", ""), method)
            for route in router.routes
            for method in getattr(route, "methods", set())
        }
        assert ("/portal/services/{subscription_id}/wifi", "POST") in routes

    def test_topup_get_route_exists(self) -> None:
        from app.web.customer.routes import router

        routes = {
            (getattr(route, "path", ""), method)
            for route in router.routes
            for method in getattr(route, "methods", set())
        }
        assert ("/portal/billing/topup", "GET") in routes

    def test_topup_verify_get_route_exists(self) -> None:
        from app.web.customer.routes import router

        routes = {
            (getattr(route, "path", ""), method)
            for route in router.routes
            for method in getattr(route, "methods", set())
        }
        assert ("/portal/billing/topup/verify", "GET") in routes

    def test_topup_intent_post_route_exists(self) -> None:
        from app.web.customer.routes import router

        routes = {
            (getattr(route, "path", ""), method)
            for route in router.routes
            for method in getattr(route, "methods", set())
        }
        assert ("/portal/billing/topup/intent", "POST") in routes


# ---------------------------------------------------------------------------
# 3. Ticket creation schema
# ---------------------------------------------------------------------------


class TestTicketCreationSchema:
    def test_ticket_create_schema_accepts_portal_fields(self) -> None:
        from app.models.support import TicketChannel
        from app.schemas.support import TicketCreate

        payload = TicketCreate(
            title="Internet not working",
            description="Connection drops every 10 minutes",
            priority="high",
            channel=TicketChannel.web,
        )
        assert payload.title == "Internet not working"
        assert payload.priority == "high"
        assert payload.channel == TicketChannel.web

    def test_ticket_create_with_subscriber_id(self) -> None:
        import uuid

        from app.schemas.support import TicketCreate

        sub_id = uuid.uuid4()
        payload = TicketCreate(
            title="Speed issue",
            subscriber_id=sub_id,
            customer_account_id=sub_id,
        )
        assert payload.subscriber_id == sub_id


# ---------------------------------------------------------------------------
# 4. Password validation
# ---------------------------------------------------------------------------


class TestPasswordValidation:
    def test_password_hashing_roundtrip(self) -> None:
        from app.services.auth_flow import hash_password, verify_password

        password = "TestP@ssw0rd!2024"
        hashed = hash_password(password)
        assert hashed != password
        assert verify_password(password, hashed)

    def test_wrong_password_fails(self) -> None:
        from app.services.auth_flow import hash_password, verify_password

        hashed = hash_password("correct_password")
        assert not verify_password("wrong_password", hashed)


# ---------------------------------------------------------------------------
# 5. Event emission helper
# ---------------------------------------------------------------------------


class TestEventEmissionHelper:
    def test_emit_customer_event_handles_missing_type(self) -> None:
        """Non-existent event type should not raise."""
        from unittest.mock import MagicMock

        from app.web.customer.routes import _emit_customer_event

        db = MagicMock()
        # Should not raise
        _emit_customer_event(db, "nonexistent_event_xyz", {})

    def test_emit_customer_event_calls_emit(self) -> None:
        from unittest.mock import MagicMock, patch

        db = MagicMock()
        with patch("app.web.customer.routes.emit_event", create=True):
            from app.web.customer.routes import _emit_customer_event

            _emit_customer_event(db, "customer_login", {"subscriber_id": "test"})


# ---------------------------------------------------------------------------
# 6. Comment schema
# ---------------------------------------------------------------------------


class TestNotificationsRoute:
    def test_notifications_route_exists(self) -> None:
        from app.web.customer.routes import router

        found = False
        for route in router.routes:
            if getattr(
                route, "path", ""
            ) == "/portal/notifications" and "GET" in getattr(route, "methods", set()):
                found = True
                break
        assert found, "GET /portal/notifications route not found"

    def test_invoice_pdf_route_exists(self) -> None:
        from app.web.customer.routes import router

        found = False
        for route in router.routes:
            path = getattr(route, "path", "")
            if (
                "/portal/billing/invoices/" in path
                and "pdf" in path
                and "GET" in getattr(route, "methods", set())
            ):
                found = True
                break
        assert found, "GET /portal/billing/invoices/{id}/pdf route not found"


class TestCommentSchema:
    def test_comment_create_schema(self) -> None:
        from app.schemas.support import TicketCommentCreate

        comment = TicketCommentCreate(body="This is my reply", is_internal=False)
        assert comment.body == "This is my reply"
        assert comment.is_internal is False


class TestCaptiveRedirectPersistence:
    def test_subscriber_update_schema_accepts_captive_redirect_flag(self) -> None:
        from app.schemas.subscriber import SubscriberUpdate

        payload = SubscriberUpdate(captive_redirect_enabled=True)
        assert payload.captive_redirect_enabled is True

    def test_save_radius_config_persists_explicit_captive_redirect_toggle(
        self, db_session
    ) -> None:
        from app.services.domain_settings import radius_settings
        from app.services.web_system_config import save_radius_config

        save_radius_config(
            db_session,
            {
                "captive_redirect_enabled": "true",
                "captive_portal_ip": "203.0.113.10/32",
                "captive_portal_url": "https://example.com/portal",
            },
        )

        setting = radius_settings.get_by_key(db_session, "captive_redirect_enabled")
        assert setting.value_text == "true"

    def test_billing_override_payload_sets_captive_redirect_flag(self) -> None:
        from app.services.web_customer_actions import _billing_override_payload

        payload = _billing_override_payload(
            billing_enabled_override=None,
            billing_day=None,
            payment_due_days=None,
            grace_period_days=None,
            min_balance=None,
            captive_redirect_enabled="true",
            tax_rate_id=None,
            payment_method=None,
        )

        assert payload["captive_redirect_enabled"] is True


class TestRestrictedContextHelpers:
    def test_get_restricted_since_uses_metadata_timestamp(self) -> None:
        from app.models.subscriber import Subscriber
        from app.services.customer_portal_context import get_restricted_since

        subscriber = Subscriber(
            first_name="Jane",
            last_name="Doe",
            email="jane@example.com",
            metadata_={"restricted_since": "2026-03-10T12:00:00+00:00"},
        )

        result = get_restricted_since(subscriber)
        assert result == datetime(2026, 3, 10, 12, 0, tzinfo=UTC)

    def test_total_outstanding_balance_uses_negative_available_balance(self) -> None:
        from app.services.customer_portal_context import get_total_outstanding_balance

        db = MagicMock()
        with patch(
            "app.services.customer_portal_context.get_available_balance",
            return_value=-125.5,
        ):
            total = get_total_outstanding_balance(
                db, "00000000-0000-0000-0000-000000000001"
            )

        assert total == 125.5

    def test_total_outstanding_balance_excludes_reconciliation_hold(
        self, db_session, subscriber
    ) -> None:
        from decimal import Decimal

        from app.models.billing import Invoice, InvoiceStatus
        from app.services.customer_portal_context import get_total_outstanding_balance

        db_session.add_all(
            [
                Invoice(
                    account_id=subscriber.id,
                    status=InvoiceStatus.overdue,
                    total=Decimal("100.00"),
                    balance_due=Decimal("100.00"),
                    is_active=True,
                    metadata_={},
                ),
                Invoice(
                    account_id=subscriber.id,
                    status=InvoiceStatus.overdue,
                    total=Decimal("75.00"),
                    balance_due=Decimal("75.00"),
                    is_active=True,
                    metadata_={"reconciliation_hold": True},
                ),
            ]
        )
        db_session.commit()

        total = get_total_outstanding_balance(db_session, str(subscriber.id))

        assert total == 100.0


class TestPlanChangeUiHelpers:
    def test_offer_price_summary_uses_recurring_price_cycle(self) -> None:
        from types import SimpleNamespace

        from app.models.catalog import PriceType
        from app.services.customer_portal_flow_changes import get_offer_price_summary

        offer = SimpleNamespace(
            prices=[
                SimpleNamespace(
                    is_active=True,
                    price_type=PriceType.recurring,
                    amount="4999.99",
                    currency="NGN",
                    billing_cycle=SimpleNamespace(value="weekly"),
                )
            ],
            billing_cycle=None,
        )

        summary = get_offer_price_summary(offer)

        assert summary.amount == 4999.99
        assert summary.currency == "NGN"
        assert summary.period_label == "/week"

    def test_plan_change_copy_mentions_proration_for_prepaid(self) -> None:
        from types import SimpleNamespace

        from app.services.customer_portal_flow_changes import get_plan_change_copy

        copy = get_plan_change_copy(
            SimpleNamespace(billing_mode=SimpleNamespace(value="prepaid"))
        )

        assert "prepaid funding" in copy["billing_message"]
        assert "prorated difference" in copy["billing_message"]

    def test_get_fup_status_uses_nearest_rule_threshold_and_quota_bucket(
        self, db_session, subscription, catalog_offer
    ) -> None:
        # The web FUP panel must key off the same QuotaBucket + nearest (lowest)
        # rule threshold + usage_warning setting as the mobile app and the
        # enforcement task — not UsageRecord / highest-rule / hardcoded 80%.
        from datetime import timedelta
        from decimal import Decimal

        from app.models.fup import FupAction, FupDataUnit, FupPolicy, FupRule
        from app.models.usage import QuotaBucket
        from app.services.customer_portal_flow_services import _get_fup_status

        subscription.offer_id = catalog_offer.id
        policy = FupPolicy(offer_id=catalog_offer.id, is_active=True)
        db_session.add(policy)
        db_session.flush()
        db_session.add_all(
            [
                FupRule(
                    policy_id=policy.id,
                    name="reduce-80",
                    sort_order=1,
                    threshold_amount=80,
                    threshold_unit=FupDataUnit.gb,
                    action=FupAction.reduce_speed,
                ),
                FupRule(
                    policy_id=policy.id,
                    name="reduce-120",
                    sort_order=2,
                    threshold_amount=120,
                    threshold_unit=FupDataUnit.gb,
                    action=FupAction.reduce_speed,
                ),
            ]
        )
        now = datetime.now(UTC)
        db_session.add(
            QuotaBucket(
                subscription_id=subscription.id,
                period_start=now - timedelta(days=5),
                period_end=now + timedelta(days=25),
                used_gb=Decimal("40"),
            )
        )
        db_session.commit()

        status = _get_fup_status(
            db_session, str(catalog_offer.id), str(subscription.id)
        )

        assert status is not None
        # Nearest (lowest) rule threshold, not the highest (was 120).
        assert status["allowance_gb"] == 80.0
        # Usage sourced from the authoritative QuotaBucket (40), not UsageRecord.
        assert status["usage_gb"] == 40.0
        assert status["usage_pct"] == 50.0

    def test_get_fup_status_ignores_unlimited_offer_fup_rules(
        self, db_session, subscription, catalog_offer
    ) -> None:
        from app.models.fup import FupAction, FupDataUnit, FupPolicy, FupRule
        from app.services.customer_portal_flow_services import _get_fup_status

        catalog_offer.plan_family = "unlimited"
        subscription.offer_id = catalog_offer.id
        policy = FupPolicy(offer_id=catalog_offer.id, is_active=True)
        db_session.add(policy)
        db_session.flush()
        db_session.add(
            FupRule(
                policy_id=policy.id,
                name="stale-unlimited-fup",
                sort_order=1,
                threshold_amount=100,
                threshold_unit=FupDataUnit.gb,
                action=FupAction.reduce_speed,
            )
        )
        db_session.commit()

        status = _get_fup_status(
            db_session, str(catalog_offer.id), str(subscription.id)
        )

        assert status is None

    def test_get_fup_status_prefers_usage_records_over_bandwidth_estimate(
        self, db_session, subscription, catalog_offer
    ) -> None:
        from app.models.fup import FupDataUnit
        from app.models.usage import UsageRecord, UsageSource
        from app.services.customer_portal_flow_services import _get_fup_status

        subscription.offer_id = catalog_offer.id
        subscription.next_billing_at = datetime(2026, 3, 30, tzinfo=UTC)
        db_session.add(
            UsageRecord(
                subscription_id=subscription.id,
                source=UsageSource.radius,
                recorded_at=datetime(2026, 3, 20, 12, 0, tzinfo=UTC),
                input_gb=2,
                output_gb=1,
                total_gb=3,
            )
        )
        db_session.commit()

        policy = SimpleNamespace(
            is_active=True,
            rules=[
                SimpleNamespace(
                    is_active=True,
                    sort_order=1,
                    threshold_amount=10,
                    threshold_unit=FupDataUnit.gb,
                ),
            ],
            offer=None,
        )

        from unittest.mock import patch

        with patch("app.services.fup.FupPolicies.get_by_offer", return_value=policy):
            status = _get_fup_status(
                db_session, str(catalog_offer.id), str(subscription.id)
            )

        assert status is not None
        assert status["usage_gb"] == 3.0


class TestPortalServiceVisibility:
    def test_get_usage_page_skips_postgres_fallback_when_disabled(
        self, db_session, subscription
    ) -> None:
        from app.services.customer_portal_flow_services import get_usage_page

        customer = {"subscription_id": str(subscription.id)}

        with (
            patch(
                "app.services.customer_portal_flow_services._daily_bandwidth_usage"
            ) as daily_bandwidth_usage,
            patch(
                "app.services.customer_portal_flow_services._usage_summary_stats"
            ) as usage_summary_stats,
            patch(
                "app.services.customer_portal_flow_services._get_fup_status"
            ) as get_fup_status,
        ):
            page = get_usage_page(
                db_session,
                customer,
                allow_postgres_fallback=False,
            )

        assert page["usage_records"] == []
        assert page["chart_records"] == []
        assert page["has_subscription"] is True
        assert page["usage_source"] == "unavailable"
        daily_bandwidth_usage.assert_not_called()
        usage_summary_stats.assert_not_called()
        get_fup_status.assert_not_called()

    def test_get_usage_page_returns_full_chart_records_with_paginated_table(
        self, db_session, subscription
    ) -> None:
        from app.services.customer_portal_flow_services import get_usage_page

        customer = {"subscription_id": str(subscription.id)}
        chart_source_records = [
            SimpleNamespace(
                recorded_at=datetime(2026, 5, 1, tzinfo=UTC),
                amount=1.25,
                usage_amount=1.25,
                download_amount=0.75,
                upload_amount=0.5,
                unit="GB",
            ),
            SimpleNamespace(
                recorded_at=datetime(2026, 5, 2, tzinfo=UTC),
                amount=2.5,
                usage_amount=2.5,
                download_amount=1.5,
                upload_amount=1.0,
                unit="GB",
            ),
            SimpleNamespace(
                recorded_at=datetime(2026, 5, 3, tzinfo=UTC),
                amount=3.75,
                usage_amount=3.75,
                download_amount=2.25,
                upload_amount=1.5,
                unit="GB",
            ),
        ]

        with (
            patch(
                "app.services.customer_portal_flow_services._daily_bandwidth_usage_records",
                return_value=chart_source_records,
            ) as daily_records,
            patch(
                "app.services.customer_portal_flow_services._usage_summary_stats",
                return_value={
                    "average_daily_usage_gb": 2.5,
                    "average_speed_mbps": 10.0,
                    "average_download_mbps": 6.0,
                    "average_upload_mbps": 4.0,
                },
            ),
            patch(
                "app.services.customer_portal_flow_services._get_fup_status",
                return_value=None,
            ),
        ):
            page = get_usage_page(
                db_session,
                customer,
                page=1,
                per_page=2,
            )

        daily_records.assert_called_once()
        assert len(page["usage_records"]) == 2
        assert [record.amount for record in page["usage_records"]] == [1.25, 2.5]
        assert len(page["chart_records"]) == 3
        assert [record["label"] for record in page["chart_records"]] == [
            "May 01",
            "May 02",
            "May 03",
        ]
        assert page["chart_records"][-1]["value"] == 3.75
        assert page["chart_records"][0]["download_value"] == 0.75
        assert page["chart_records"][0]["upload_value"] == 0.5

    def test_daily_bandwidth_usage_batches_bandwidth_lookup(self) -> None:
        from app.services.customer_portal_flow_services import _daily_bandwidth_usage

        db = MagicMock()
        query = MagicMock()
        db.query.return_value = query
        query.filter.return_value = query
        query.group_by.return_value = query
        query.all.return_value = [
            SimpleNamespace(
                bucket_start=datetime(2026, 3, 20, tzinfo=UTC),
                rx_bps=8_000_000,
                tx_bps=2_000_000,
            )
        ]

        with patch(
            "app.services.customer_portal_flow_services._daily_usage_breakdown_records",
            return_value={},
        ):
            records, total = _daily_bandwidth_usage(
                db,
                subscription_id="00000000-0000-0000-0000-000000000001",
                start_at=datetime(2026, 3, 20, 0, 0, tzinfo=UTC),
                end_at=datetime(2026, 3, 20, 23, 59, tzinfo=UTC),
                page=1,
                per_page=10,
            )

        assert db.query.call_count == 2
        assert query.group_by.call_count == 2
        assert query.all.call_count == 2
        query.first.assert_not_called()
        assert total == 1
        assert len(records) == 1
        assert records[0].usage_amount > 0

    def test_daily_bandwidth_usage_records_falls_back_to_radius_accounting(
        self,
    ) -> None:
        from app.services.customer_portal_flow_services import (
            _daily_bandwidth_usage_records,
        )

        db = MagicMock()
        start_at = datetime(2026, 3, 19, 0, 0, tzinfo=UTC)
        end_at = datetime(2026, 3, 20, 23, 59, tzinfo=UTC)

        with (
            patch(
                "app.services.customer_portal_flow_services._daily_usage_breakdown_records",
                return_value={},
            ),
            patch(
                "app.services.customer_portal_flow_services._daily_bandwidth_averages",
                return_value={},
            ),
            patch(
                "app.services.customer_portal_flow_services._daily_radius_accounting_usage",
                return_value={
                    datetime(2026, 3, 19, tzinfo=UTC).date(): (1.25, 0.5, 1.75)
                },
            ),
        ):
            records = _daily_bandwidth_usage_records(
                db,
                subscription_id="00000000-0000-0000-0000-000000000001",
                start_at=start_at,
                end_at=end_at,
            )

        assert len(records) == 2
        assert records[0].recorded_at == datetime(2026, 3, 20, tzinfo=UTC)
        assert records[0].usage_amount == 0
        assert records[0].download_amount == 0
        assert records[0].upload_amount == 0
        assert records[1].recorded_at == datetime(2026, 3, 19, tzinfo=UTC)
        assert records[1].usage_amount == 1.75
        assert records[1].download_amount == 1.25
        assert records[1].upload_amount == 0.5

    def test_services_page_includes_blocked_subscription(
        self, db_session, subscription, subscriber
    ) -> None:
        from app.models.catalog import SubscriptionStatus
        from app.services.customer_portal_flow_services import get_services_page

        subscription.status = SubscriptionStatus.blocked
        db_session.commit()

        page = get_services_page(
            db_session,
            {"account_id": subscriber.id},
            page=1,
            per_page=10,
        )

        assert page["total"] == 1
        assert len(page["services"]) == 1
        assert page["services"][0].status == SubscriptionStatus.blocked

    def test_services_page_suspended_prepaid_uses_suspension_evidence(
        self, db_session, subscription, subscriber
    ) -> None:
        from app.models.catalog import BillingMode, SubscriptionStatus
        from app.models.enforcement_lock import EnforcementLock, EnforcementReason
        from app.services.customer_portal_flow_services import (
            PortalServiceDateKind,
            get_services_page,
        )

        stale_anchor = datetime(2025, 9, 4, tzinfo=UTC)
        suspended_at = datetime(2026, 9, 3, 0, 1, tzinfo=UTC)
        subscriber.billing_mode = BillingMode.prepaid
        subscription.billing_mode = BillingMode.prepaid
        subscription.status = SubscriptionStatus.suspended
        subscription.next_billing_at = stale_anchor
        db_session.add(
            EnforcementLock(
                subscription_id=subscription.id,
                subscriber_id=subscriber.id,
                reason=EnforcementReason.prepaid,
                source="test:prepaid-balance-sweep",
                created_at=suspended_at,
            )
        )
        db_session.commit()

        page = get_services_page(
            db_session,
            {"account_id": subscriber.id},
            page=1,
            per_page=10,
        )

        service = page["services"][0]
        assert service.next_billing_at is not None
        assert service.next_billing_at.replace(tzinfo=UTC) == stale_anchor
        assert service.date_projection.kind == PortalServiceDateKind.suspended_since
        assert service.date_projection.label == "Suspended since"
        assert service.date_projection.value.is_present is True
        assert service.date_projection.value.value == suspended_at

    def test_services_page_active_prepaid_uses_exact_coverage_end(
        self, db_session, subscription, subscriber
    ) -> None:
        from datetime import timedelta
        from decimal import Decimal

        from app.models.billing import ServiceEntitlement, ServiceEntitlementStatus
        from app.models.catalog import BillingMode, SubscriptionStatus
        from app.services.customer_portal_flow_services import (
            PortalServiceDateKind,
            get_services_page,
        )

        now = datetime.now(UTC)
        coverage_end = now + timedelta(days=29)
        subscriber.billing_mode = BillingMode.prepaid
        subscription.billing_mode = BillingMode.prepaid
        subscription.status = SubscriptionStatus.active
        subscription.next_billing_at = datetime(2025, 9, 4, tzinfo=UTC)
        db_session.add(
            ServiceEntitlement(
                account_id=subscriber.id,
                subscription_id=subscription.id,
                starts_at=now - timedelta(days=1),
                ends_at=coverage_end,
                amount_funded=Decimal("430000.00"),
                currency="NGN",
                status=ServiceEntitlementStatus.active,
            )
        )
        db_session.commit()

        page = get_services_page(
            db_session,
            {"account_id": subscriber.id},
            page=1,
            per_page=10,
        )

        service = page["services"][0]
        assert service.date_projection.kind == PortalServiceDateKind.paid_through
        assert service.date_projection.label == "Paid through"
        assert service.date_projection.value.is_present is True
        assert service.date_projection.value.value == coverage_end

    def test_services_page_eager_loads_template_relationships(
        self, db_session, subscription, subscriber
    ) -> None:
        from sqlalchemy import inspect as sa_inspect

        from app.services.customer_portal_flow_services import get_services_page

        db_session.expire_all()

        page = get_services_page(
            db_session,
            {"account_id": subscriber.id},
            page=1,
            per_page=10,
        )

        assert page["services"]
        service = page["services"][0]
        unloaded = sa_inspect(service.subscription).unloaded
        assert "offer" not in unloaded
        assert "offer_version" not in unloaded


class TestCustomerUsageRoute:
    def test_usage_page_defaults_to_chart_and_includes_initial_bandwidth_stats(
        self,
    ) -> None:
        from app.web.customer.routes import customer_usage

        request = MagicMock()
        customer = {"subscriber_id": "sub-1", "account_id": "acct-1"}
        subscription = SimpleNamespace(id="svc-123")
        usage_page = {
            "usage_records": [],
            "chart_records": [],
            "period": "current",
            "page": 1,
            "per_page": 25,
            "total": 0,
            "total_pages": 1,
            "usage_summary": {},
            "fup_status": None,
            "usage_source": "none",
            "has_subscription": True,
        }
        template_response = MagicMock(name="template_response")

        with (
            patch(
                "app.web.customer.routes.get_current_customer_from_request",
                return_value=customer,
            ),
            patch(
                "app.web.customer.routes.customer_portal.get_usage_page",
                return_value=usage_page,
            ),
            patch(
                "app.web.customer.routes.customer_portal.get_usage_history",
                return_value={"has_history": False, "chart_records": []},
            ),
            patch(
                "app.web.customer.routes.resolve_customer_subscription",
                return_value=subscription,
            ),
            patch(
                "app.web.customer.routes._load_initial_bandwidth_stats",
                return_value={"current_rx_formatted": "9.41 Kbps"},
            ),
            patch(
                "app.web.customer.routes.templates.TemplateResponse",
                return_value=template_response,
            ) as render,
        ):
            response = customer_usage(
                request=request,
                period="current",
                page=1,
                per_page=25,
                db=MagicMock(),
            )

        assert response is template_response
        assert render.call_args.args[0] == "customer/usage/index.html"
        context = render.call_args.args[1]
        assert context["bandwidth_chart_initial_stats"] == {
            "current_rx_formatted": "9.41 Kbps"
        }
        assert context["usage_chart_records"] == usage_page["chart_records"]
        assert context["usage_enable_records_chart"] is True
        assert context["usage_records_default_view"] == "chart"
        assert context["usage_records_chart_id"] == "portal-usage-records-chart"
        assert context["usage_records_chart_label"] == "Daily Usage (GB)"
        # Live streaming is enabled for customers with an active subscription so
        # the usage page subscribes to the /portal/bandwidth/my/live SSE stream.
        assert context["bandwidth_chart_live_stream"] is True


class TestAdminUsageTemplateDefaults:
    def test_admin_stats_defaults_to_chart_view(self) -> None:
        template = Path("templates/admin/customers/_stats_panel.html").read_text(
            encoding="utf-8"
        )

        assert "{% set usage_records_default_view = 'chart' %}" in template

    def test_admin_stats_consumes_central_live_stream(self) -> None:
        stats_panel = Path("templates/admin/customers/_stats_panel.html").read_text(
            encoding="utf-8"
        )
        usage_content = Path("templates/customer/usage/_content.html").read_text(
            encoding="utf-8"
        )

        assert "/api/v1/bandwidth/mikrotik-live/" not in stats_panel
        assert "bandwidth_chart_direct_live_endpoint" not in stats_panel
        assert (
            "bandwidth_chart_live_stream = usage_subscription_id is not none"
            in stats_panel
        )
        assert "liveStream: true" in usage_content

    def test_legacy_direct_probe_polling_is_sequential_and_cancellable(self) -> None:
        js = Path("static/js/bandwidth-chart.js").read_text(encoding="utf-8")

        direct_section = js.split("async loadDirectLive()", 1)[1].split(
            "// Load historical data", 1
        )[0]
        assert "directLiveRequestInFlight" in direct_section
        assert "new AbortController()" in direct_section
        assert "setTimeout(pollAfterCompletion" in direct_section
        assert "setInterval" not in direct_section

    def test_customer_usage_template_supports_live_stream(self) -> None:
        usage_content = Path("templates/customer/usage/_content.html").read_text(
            encoding="utf-8"
        )
        js = Path("static/js/bandwidth-chart.js").read_text(encoding="utf-8")

        # The live UI gate is decoupled from the admin-only directLiveEndpoint so
        # the customer SSE (/portal/bandwidth/my/live) can drive it.
        assert "bandwidth_chart_live_stream" in usage_content
        assert "bandwidth_chart_live" in usage_content
        assert "liveStream: true" in usage_content
        assert "liveStream: config.liveStream" in js

    def test_admin_monitoring_bandwidth_panel_refreshes_on_demand(self) -> None:
        index = Path("templates/admin/network/monitoring/index.html").read_text(
            encoding="utf-8"
        )
        partial = Path(
            "templates/admin/network/monitoring/_bandwidth_partial.html"
        ).read_text(encoding="utf-8")

        # Network-wide bandwidth refreshes on demand (a Refresh button targets
        # the partial) rather than auto-polling every 15s.
        assert "/admin/network/monitoring/bandwidth" in index
        assert 'hx-target="#monitoring-bandwidth-container"' in index
        assert 'hx-trigger="every 15s"' not in index
        assert 'hx-trigger="every 30s"' not in index
        assert "Bandwidth Overview" in partial
        assert "NAS Throughput" in partial

    def test_admin_monitoring_bandwidth_partial_renders_nas_items(self) -> None:
        from app.web.templates import templates

        template = templates.env.get_template(
            "admin/network/monitoring/_bandwidth_partial.html"
        )

        html = template.render(
            bandwidth={
                "has_data": False,
                "total_rx_formatted": "0 bps",
                "total_tx_formatted": "0 bps",
                "top_users": [],
            },
            nas_throughput={
                "has_data": True,
                "items": [
                    {
                        "name": "NAS A",
                        "rx_bps": 10.0,
                        "tx_bps": 5.0,
                        "rx_formatted": "10 bps",
                        "tx_formatted": "5 bps",
                    }
                ],
            },
        )

        assert "NAS A" in html
        assert "10 bps" in html


class TestPortalNotificationsPage:
    def test_notifications_page_merges_event_queue_and_customer_notification_events(
        self, db_session, subscriber
    ) -> None:
        from app.models.comms import (
            CustomerNotificationEvent,
            CustomerNotificationStatus,
        )
        from app.models.notification import (
            Notification,
            NotificationChannel,
            NotificationStatus,
            NotificationTemplate,
        )
        from app.services.customer_portal_notifications import get_notifications_page

        subscriber.phone = "+2348000000000"
        template = NotificationTemplate(
            name="Invoice Created",
            code="invoice_created",
            channel=NotificationChannel.email,
            body="Invoice body",
            is_active=True,
        )
        db_session.add(template)
        db_session.flush()

        queued = Notification(
            template_id=template.id,
            channel=NotificationChannel.email,
            recipient=subscriber.email,
            body="Invoice ready",
            status=NotificationStatus.delivered,
        )
        direct = CustomerNotificationEvent(
            entity_type="service_order",
            entity_id=subscriber.id,
            channel="sms",
            recipient=subscriber.phone,
            message="Technician dispatched",
            status=CustomerNotificationStatus.sent,
        )
        db_session.add_all([queued, direct])
        db_session.commit()

        page = get_notifications_page(
            db_session,
            {"subscriber_id": str(subscriber.id)},
            page=1,
            per_page=10,
        )

        assert page["total"] == 2
        entity_types = {item.entity_type for item in page["notifications"]}
        assert "invoice_created" in entity_types
        assert "service_order" in entity_types

    def test_notifications_preview_returns_recent_items_and_total(
        self, db_session, subscriber
    ) -> None:
        from app.models.comms import (
            CustomerNotificationEvent,
            CustomerNotificationStatus,
        )
        from app.models.notification import (
            Notification,
            NotificationChannel,
            NotificationStatus,
        )
        from app.services.customer_portal_notifications import get_notifications_preview

        subscriber.phone = "+2348000000000"
        db_session.add_all(
            [
                Notification(
                    subscriber_id=subscriber.id,
                    channel=NotificationChannel.email,
                    recipient=subscriber.email,
                    body="Billing reminder",
                    status=NotificationStatus.delivered,
                ),
                CustomerNotificationEvent(
                    entity_type="service_order",
                    entity_id=subscriber.id,
                    subscriber_id=subscriber.id,
                    channel="sms",
                    recipient=subscriber.phone,
                    message="Technician dispatched",
                    status=CustomerNotificationStatus.sent,
                ),
            ]
        )
        db_session.commit()

        preview = get_notifications_preview(
            db_session,
            {"subscriber_id": str(subscriber.id)},
            limit=1,
        )

        assert preview["recent_notifications_total"] == 2
        assert preview["unread_notifications_count"] == 2
        assert preview["has_recent_notifications"] is True
        assert len(preview["recent_notifications"]) == 1

    def test_notifications_page_prefers_subscriber_id_and_hides_non_visible_statuses(
        self, db_session, subscriber
    ) -> None:
        from app.models.notification import (
            Notification,
            NotificationChannel,
            NotificationStatus,
        )
        from app.models.subscriber import Subscriber
        from app.services.customer_portal_notifications import get_notifications_page

        other_subscriber = Subscriber(
            first_name="Other",
            last_name="User",
            email="other@example.com",
            phone=subscriber.phone,
        )
        db_session.add(other_subscriber)
        db_session.flush()

        db_session.add_all(
            [
                Notification(
                    subscriber_id=subscriber.id,
                    channel=NotificationChannel.email,
                    recipient="old-email@example.com",
                    event_type="invoice_paid",
                    category="billing",
                    body="Delivered to owned subscriber",
                    status=NotificationStatus.delivered,
                ),
                Notification(
                    subscriber_id=subscriber.id,
                    channel=NotificationChannel.email,
                    recipient=subscriber.email,
                    event_type="invoice_paid",
                    category="billing",
                    body="Queued row should stay hidden",
                    status=NotificationStatus.queued,
                ),
                Notification(
                    subscriber_id=other_subscriber.id,
                    channel=NotificationChannel.email,
                    recipient=subscriber.email,
                    event_type="invoice_paid",
                    category="billing",
                    body="Recipient collision should stay hidden",
                    status=NotificationStatus.delivered,
                ),
            ]
        )
        db_session.commit()

        page = get_notifications_page(
            db_session,
            {"subscriber_id": str(subscriber.id)},
            page=1,
            per_page=10,
        )

        assert page["total"] == 1
        assert page["notifications"][0].message == "Delivered to owned subscriber"

    def test_notifications_page_respects_billing_and_sms_preferences(
        self, db_session, subscriber
    ) -> None:
        from app.models.comms import (
            CustomerNotificationEvent,
            CustomerNotificationStatus,
        )
        from app.models.notification import (
            Notification,
            NotificationChannel,
            NotificationStatus,
        )
        from app.services.customer_portal_notifications import get_notifications_page

        subscriber.metadata_ = {
            "billing_notifications": False,
            "sms_updates": False,
        }
        subscriber.phone = "+2348000000011"
        db_session.add_all(
            [
                Notification(
                    subscriber_id=subscriber.id,
                    channel=NotificationChannel.email,
                    recipient=subscriber.email,
                    event_type="invoice_paid",
                    category="billing",
                    body="Billing email",
                    status=NotificationStatus.delivered,
                ),
                CustomerNotificationEvent(
                    entity_type="service_order_completed",
                    entity_id=subscriber.id,
                    subscriber_id=subscriber.id,
                    channel="sms",
                    recipient=subscriber.phone,
                    message="Service order completed",
                    status=CustomerNotificationStatus.sent,
                ),
            ]
        )
        db_session.commit()

        page = get_notifications_page(
            db_session,
            {"subscriber_id": str(subscriber.id)},
            page=1,
            per_page=10,
        )

        assert page["total"] == 0


class TestCustomerProfileNotifications:
    def test_update_customer_profile_persists_preferences_and_emits_subscriber_updated(
        self, db_session, subscriber
    ) -> None:
        from app.services.events.types import EventType
        from app.services.web_customer_actions import update_customer_profile

        with patch("app.services.subscriber.emit_event") as emit_event_mock:
            updated = update_customer_profile(
                db_session,
                subscriber_id=str(subscriber.id),
                first_name="Updated",
                last_name="Customer",
                email="updated@example.com",
                phone="+2348000000012",
                billing_notifications=False,
                sms_updates=True,
            )

        assert updated is not None
        assert updated.email == "updated@example.com"
        assert updated.phone == "+2348000000012"
        assert (updated.metadata_ or {}).get("billing_notifications") is False
        assert (updated.metadata_ or {}).get("sms_updates") is True
        assert emit_event_mock.call_args.args[1] == EventType.subscriber_updated


class TestPaymentSuccessBanner:
    def test_payment_success_only_marks_service_restored_after_post_payment_check(
        self,
    ) -> None:
        from unittest.mock import patch

        from app.web.customer.routes import customer_verify_payment

        request = MagicMock()
        customer = {"subscriber_id": "sub-1", "account_id": "acct-1"}
        result = {
            "payment": SimpleNamespace(receipt_number="RCT-1"),
            "invoice": SimpleNamespace(id="inv-1", invoice_number="INV-1"),
            "amount": 5000,
            "reference": "ref-1",
            "provider_type": "paystack",
        }

        template_response = MagicMock(name="template_response")

        with (
            patch(
                "app.web.customer.routes.get_current_customer_from_request",
                return_value=customer,
            ),
            patch(
                "app.web.customer.routes.customer_portal.verify_and_record_payment",
                return_value=result,
            ),
            patch(
                "app.web.customer.routes.is_subscriber_restricted",
                side_effect=[True, False],
            ),
            patch(
                "app.web.customer.routes.templates.TemplateResponse",
                return_value=template_response,
            ) as render,
        ):
            response = customer_verify_payment(
                request=request,
                reference="ref-1",
                provider="paystack",
                db=MagicMock(),
            )

        assert response is template_response
        context = render.call_args.args[1]
        assert context["was_restricted"] is True
        assert context["service_restored"] is True

    def test_payment_success_does_not_claim_restoration_after_partial_payment(
        self,
    ) -> None:
        from unittest.mock import patch

        from app.web.customer.routes import customer_verify_payment

        request = MagicMock()
        customer = {"subscriber_id": "sub-1", "account_id": "acct-1"}
        result = {
            "payment": SimpleNamespace(receipt_number="RCT-2"),
            "invoice": SimpleNamespace(id="inv-2", invoice_number="INV-2"),
            "amount": 1000,
            "reference": "ref-2",
            "provider_type": "paystack",
        }

        template_response = MagicMock(name="template_response")

        with (
            patch(
                "app.web.customer.routes.get_current_customer_from_request",
                return_value=customer,
            ),
            patch(
                "app.web.customer.routes.customer_portal.verify_and_record_payment",
                return_value=result,
            ),
            patch(
                "app.web.customer.routes.is_subscriber_restricted",
                side_effect=[True, True],
            ),
            patch(
                "app.web.customer.routes.templates.TemplateResponse",
                return_value=template_response,
            ) as render,
        ):
            response = customer_verify_payment(
                request=request,
                reference="ref-2",
                provider="paystack",
                db=MagicMock(),
            )

        assert response is template_response
        context = render.call_args.args[1]
        assert context["was_restricted"] is True
        assert context["service_restored"] is False


class TestPaymentArrangementRouteErrors:
    def test_arrangement_submit_rerenders_form_on_http_exception(self) -> None:
        from app.web.customer.routes import customer_submit_payment_arrangement

        request = MagicMock()
        customer = {"subscriber_id": "sub-1", "account_id": "acct-1"}
        template_response = MagicMock(name="template_response")

        with (
            patch(
                "app.web.customer.routes.get_current_customer_from_request",
                return_value=customer,
            ),
            patch(
                "app.web.customer.routes.customer_portal.submit_payment_arrangement",
                side_effect=HTTPException(
                    status_code=400, detail="Invalid arrangement"
                ),
            ) as submit,
            patch(
                "app.web.customer.routes.customer_portal.get_arrangement_error_context",
                return_value={"invoices": [], "outstanding_balance": 0},
            ),
            patch(
                "app.web.customer.routes.templates.TemplateResponse",
                return_value=template_response,
            ) as render,
        ):
            response = customer_submit_payment_arrangement(
                request=request,
                total_amount="1000",
                installments=3,
                frequency="monthly",
                start_date="2025-01-31",
                invoice_id=None,
                notes=None,
                terms="on",
                db=MagicMock(),
            )

        assert response is template_response
        assert render.call_args.args[0] == "customer/billing/arrangement_form.html"
        assert render.call_args.kwargs["status_code"] == 400
        context = render.call_args.args[1]
        assert context["error"] == "Invalid arrangement"
        assert submit.call_args.kwargs["terms_accepted"] is True


class TestCustomerTopupRoutes:
    def test_topup_page_renders_dedicated_template(self) -> None:
        from unittest.mock import patch

        from app.web.customer.routes import customer_billing_topup

        request = MagicMock()
        customer = {"subscriber_id": "sub-1", "account_id": "acct-1"}
        page_data = {
            "provider_type": "paystack",
            "provider_public_key": "pk_test",
            "customer_email": "test@example.com",
            "prepaid_balance": 2500,
            "min_amount": 1000,
            "max_amount": 500000,
            "preset_amounts": [1000, 2000, 5000],
        }
        template_response = MagicMock(name="template_response")

        with (
            patch(
                "app.web.customer.routes.get_current_customer_from_request",
                return_value=customer,
            ),
            patch(
                "app.web.customer.routes.customer_portal.get_topup_page",
                return_value=page_data,
            ),
            patch(
                "app.web.customer.routes.templates.TemplateResponse",
                return_value=template_response,
            ) as render,
        ):
            response = customer_billing_topup(request=request, db=MagicMock())

        assert response is template_response
        assert render.call_args.args[0] == "customer/billing/topup.html"
        context = render.call_args.args[1]
        assert "payment_reference" not in context
        assert context["active_page"] == "billing"

    def test_topup_intent_route_returns_json_payload(self) -> None:
        import json
        from unittest.mock import patch

        from app.web.customer.routes import customer_create_topup_intent

        request = MagicMock()
        customer = {"subscriber_id": "sub-1", "account_id": "acct-1"}

        with (
            patch(
                "app.web.customer.routes.get_current_customer_from_request",
                return_value=customer,
            ),
            patch(
                "app.web.customer.routes.customer_portal.create_topup_intent",
                return_value={
                    "intent_id": "intent-1",
                    "provider_type": "paystack",
                    "provider_public_key": "pk_test",
                    "reference": "ref-topup",
                    "requested_amount": 5000,
                    "currency": "NGN",
                    "checkout_metadata": {
                        "payment_flow": "account_topup",
                        "topup_intent_id": "intent-1",
                        "account_id": "acct-1",
                    },
                    "preview_fingerprint": "a" * 64,
                },
            ),
        ):
            response = customer_create_topup_intent(
                request=request,
                payload={
                    "amount": 5000,
                    "provider": "paystack",
                    "preview_fingerprint": "a" * 64,
                },
                db=MagicMock(),
            )

        assert response.status_code == 200
        payload = json.loads(response.body)
        assert payload["reference"] == "ref-topup"
        assert payload["checkout_metadata"]["topup_intent_id"] == "intent-1"

    def test_topup_success_marks_service_restored_after_post_payment_check(
        self,
    ) -> None:
        from unittest.mock import patch

        from app.web.customer.routes import customer_verify_topup

        request = MagicMock()
        customer = {"subscriber_id": "sub-1", "account_id": "acct-1"}
        result = {
            "payment": SimpleNamespace(receipt_number="RCT-T1"),
            "amount": 5000,
            "reference": "ref-topup-1",
            "already_recorded": False,
            "allocated_to_invoices": [],
            "allocated_total": 0,
            "credit_added": 5000,
            "available_balance": 5000,
            "policy_warnings": [],
            "provider_type": "paystack",
        }

        template_response = MagicMock(name="template_response")

        with (
            patch(
                "app.web.customer.routes.get_current_customer_from_request",
                return_value=customer,
            ),
            patch(
                "app.web.customer.routes.customer_portal.verify_and_record_topup",
                return_value=result,
            ),
            patch(
                "app.web.customer.routes.is_subscriber_restricted",
                side_effect=[True, False],
            ),
            patch(
                "app.web.customer.routes.templates.TemplateResponse",
                return_value=template_response,
            ) as render,
        ):
            response = customer_verify_topup(
                request=request,
                reference="ref-topup-1",
                provider="paystack",
                db=MagicMock(),
            )

        assert response is template_response
        assert render.call_args.args[0] == "customer/billing/topup_success.html"
        context = render.call_args.args[1]
        assert context["was_restricted"] is True
        assert context["service_restored"] is True
        assert context["credit_added"] == 5000


class TestPlanChangeSettingsValidation:
    def test_save_plan_change_rejects_invalid_refund_policy(self) -> None:
        import pytest

        from app.services.web_system_config import save_plan_change

        with pytest.raises(ValueError, match="Refund Policy"):
            save_plan_change(
                MagicMock(),
                {
                    "refund_policy": "bogus",
                    "upgrade_fee": "0",
                    "downgrade_fee": "0",
                    "fee_tax_rate": "0",
                    "invoice_timing": "immediate",
                    "prepaid_rollover": "false",
                    "discount_transfer": "false",
                    "minimum_invoice_amount": "0",
                },
            )

    def test_save_plan_change_normalizes_valid_values(self) -> None:
        from unittest.mock import patch

        from app.services.web_system_config import save_plan_change

        with patch("app.services.web_system_config._save_settings") as save_mock:
            save_plan_change(
                MagicMock(),
                {
                    "refund_policy": "PRORATED",
                    "upgrade_fee": "500.00",
                    "downgrade_fee": "0",
                    "fee_tax_rate": "7.50",
                    "invoice_timing": "Immediate",
                    "prepaid_rollover": "TRUE",
                    "discount_transfer": "false",
                    "minimum_invoice_amount": "100.00",
                },
            )

        saved_payload = save_mock.call_args.args[2]
        assert saved_payload["refund_policy"] == "prorated"
        assert saved_payload["invoice_timing"] == "immediate"
        assert saved_payload["prepaid_rollover"] == "true"


class TestRestrictedStatusMetadata:
    def test_restricted_status_transition_sets_restricted_since(self) -> None:
        from app.models.subscriber import Subscriber, SubscriberStatus
        from app.services.subscriber import _update_restricted_status_metadata

        subscriber = Subscriber(
            first_name="Jane",
            last_name="Doe",
            email="jane@example.com",
            metadata_={},
        )

        _update_restricted_status_metadata(
            subscriber,
            previous_status=SubscriberStatus.active,
            next_status=SubscriberStatus.blocked,
        )

        assert subscriber.metadata_ is not None
        assert "restricted_since" in subscriber.metadata_
        assert subscriber.metadata_["restricted_status"] == "blocked"

    def test_leaving_restricted_status_records_exit(self) -> None:
        from app.models.subscriber import Subscriber, SubscriberStatus
        from app.services.subscriber import _update_restricted_status_metadata

        subscriber = Subscriber(
            first_name="Jane",
            last_name="Doe",
            email="jane@example.com",
            metadata_={"restricted_since": "2026-03-10T12:00:00+00:00"},
        )

        _update_restricted_status_metadata(
            subscriber,
            previous_status=SubscriberStatus.suspended,
            next_status=SubscriberStatus.active,
        )

        assert subscriber.metadata_ is not None
        assert subscriber.metadata_["last_restricted_status"] == "suspended"
        assert "last_restricted_ended_at" in subscriber.metadata_


class TestSuspendResumeRoutes:
    def test_suspend_get_route_exists(self) -> None:
        from app.web.customer.routes import router

        found = False
        for route in router.routes:
            path = getattr(route, "path", "")
            methods = getattr(route, "methods", set())
            if "/portal/services/" in path and "suspend" in path and "GET" in methods:
                found = True
                break
        assert found, "GET /portal/services/{id}/suspend route not found"

    def test_suspend_post_route_exists(self) -> None:
        from app.web.customer.routes import router

        found = False
        for route in router.routes:
            path = getattr(route, "path", "")
            methods = getattr(route, "methods", set())
            if "/portal/services/" in path and "suspend" in path and "POST" in methods:
                found = True
                break
        assert found, "POST /portal/services/{id}/suspend route not found"

    def test_resume_get_route_exists(self) -> None:
        from app.web.customer.routes import router

        found = False
        for route in router.routes:
            path = getattr(route, "path", "")
            methods = getattr(route, "methods", set())
            if "/portal/services/" in path and "resume" in path and "GET" in methods:
                found = True
                break
        assert found, "GET /portal/services/{id}/resume route not found"

    def test_resume_post_route_exists(self) -> None:
        from app.web.customer.routes import router

        found = False
        for route in router.routes:
            path = getattr(route, "path", "")
            methods = getattr(route, "methods", set())
            if "/portal/services/" in path and "resume" in path and "POST" in methods:
                found = True
                break
        assert found, "POST /portal/services/{id}/resume route not found"


class TestSuspendResumeServiceLayer:
    def test_get_suspend_page_returns_none_for_non_active_subscription(
        self, db_session, subscription, subscriber
    ) -> None:
        from app.models.catalog import SubscriptionStatus
        from app.services.customer_portal_flow_services import get_suspend_page

        subscription.status = SubscriptionStatus.suspended
        db_session.commit()

        result = get_suspend_page(
            db_session,
            {"account_id": subscriber.id},
            str(subscription.id),
        )

        assert result is None

    def test_get_suspend_page_returns_context_for_active_subscription(
        self, db_session, subscription, subscriber
    ) -> None:
        from app.models.catalog import SubscriptionStatus
        from app.services.customer_portal_flow_services import get_suspend_page

        subscription.status = SubscriptionStatus.active
        db_session.commit()

        result = get_suspend_page(
            db_session,
            {"account_id": subscriber.id},
            str(subscription.id),
        )

        assert result is not None
        assert "subscription" in result
        assert "max_days" in result

    def test_apply_service_suspend_creates_enforcement_lock(
        self, db_session, subscription, subscriber
    ) -> None:
        from app.models.catalog import SubscriptionStatus
        from app.models.enforcement_lock import EnforcementLock, EnforcementReason
        from app.services.customer_portal_flow_services import apply_service_suspend

        subscription.status = SubscriptionStatus.active
        db_session.commit()

        result = apply_service_suspend(
            db_session,
            {"account_id": subscriber.id},
            str(subscription.id),
            days=7,
        )

        assert result["subscription_id"] == str(subscription.id)
        assert result["days"] == 7

        # Verify lock was created
        lock = (
            db_session.query(EnforcementLock)
            .filter(
                EnforcementLock.subscription_id == subscription.id,
                EnforcementLock.reason == EnforcementReason.customer_hold,
            )
            .first()
        )
        assert lock is not None
        assert lock.is_active is True

    def test_get_resume_page_returns_none_without_customer_hold_lock(
        self, db_session, subscription, subscriber
    ) -> None:
        from app.models.catalog import SubscriptionStatus
        from app.services.customer_portal_flow_services import get_resume_page

        subscription.status = SubscriptionStatus.suspended
        db_session.commit()

        result = get_resume_page(
            db_session,
            {"account_id": subscriber.id},
            str(subscription.id),
        )

        # No customer_hold lock exists, so cannot self-service resume
        assert result is None

    def test_apply_service_resume_restores_subscription(
        self, db_session, subscription, subscriber
    ) -> None:
        from app.models.catalog import SubscriptionStatus
        from app.models.enforcement_lock import EnforcementLock, EnforcementReason
        from app.services.customer_portal_flow_services import (
            apply_service_resume,
            apply_service_suspend,
        )

        # First suspend the subscription
        subscription.status = SubscriptionStatus.active
        db_session.commit()

        apply_service_suspend(
            db_session,
            {"account_id": subscriber.id},
            str(subscription.id),
            days=7,
        )
        db_session.refresh(subscription)
        assert subscription.status == SubscriptionStatus.suspended

        # Now resume it
        result = apply_service_resume(
            db_session,
            {"account_id": subscriber.id},
            str(subscription.id),
        )

        db_session.refresh(subscription)
        assert result["restored"] is True
        assert subscription.status == SubscriptionStatus.active

        # Verify lock was resolved
        lock = (
            db_session.query(EnforcementLock)
            .filter(
                EnforcementLock.subscription_id == subscription.id,
                EnforcementLock.reason == EnforcementReason.customer_hold,
            )
            .first()
        )
        assert lock is not None
        assert lock.is_active is False


class TestVacationHoldUsageLimits:
    """Tests for vacation hold usage limits and cooldown periods."""

    def test_get_vacation_hold_usage_counts_holds_this_year(
        self, db_session, subscription, subscriber
    ) -> None:
        from datetime import UTC, datetime

        from app.models.enforcement_lock import EnforcementLock, EnforcementReason
        from app.services.customer_portal_flow_services import _get_vacation_hold_usage

        # Create a resolved hold from this year
        lock = EnforcementLock(
            subscription_id=subscription.id,
            subscriber_id=subscriber.id,
            reason=EnforcementReason.customer_hold,
            source="test",
            is_active=False,
            resolved_at=datetime.now(UTC),
            resolved_by="test",
            created_at=datetime.now(UTC),
        )
        db_session.add(lock)
        db_session.commit()

        usage = _get_vacation_hold_usage(db_session, str(subscription.id))

        assert usage["holds_this_year"] == 1
        assert usage["last_hold_date"] is not None
        assert usage["days_since_last"] is not None
        assert usage["days_since_last"] >= 0

    def test_get_vacation_hold_usage_returns_zero_for_no_holds(
        self, db_session, subscription
    ) -> None:
        from app.services.customer_portal_flow_services import _get_vacation_hold_usage

        usage = _get_vacation_hold_usage(db_session, str(subscription.id))

        assert usage["holds_this_year"] == 0
        assert usage["last_hold_date"] is None
        assert usage["days_since_last"] is None

    def test_apply_service_suspend_rejects_when_max_holds_reached(
        self, db_session, subscription, subscriber
    ) -> None:
        from unittest.mock import patch

        import pytest

        from app.models.catalog import SubscriptionStatus
        from app.services.customer_portal_flow_services import apply_service_suspend

        subscription.status = SubscriptionStatus.active
        db_session.commit()

        # Mock the settings and usage to simulate max holds reached
        def mock_resolve_value(db, domain, key):
            if key == "max_suspend_holds_per_year":
                return 1
            if key == "customer_suspend_enabled":
                return True
            if key == "max_suspend_days":
                return 30
            if key == "suspend_cooldown_days":
                return 0
            return None

        from app.models.enforcement_lock import EnforcementLock, EnforcementReason

        db_session.add(
            EnforcementLock(
                subscription_id=subscription.id,
                subscriber_id=subscriber.id,
                reason=EnforcementReason.customer_hold,
                source="historical:0",
                is_active=False,
                resolved_at=datetime.now(UTC),
                resolved_by="test",
                created_at=datetime.now(UTC),
            )
        )
        db_session.commit()

        with patch(
            "app.services.settings_spec.resolve_value",
            side_effect=mock_resolve_value,
        ):
            with pytest.raises(ValueError, match="maximum of 1 vacation holds"):
                apply_service_suspend(
                    db_session,
                    {"account_id": subscriber.id},
                    str(subscription.id),
                    days=7,
                )

    def test_apply_service_suspend_rejects_during_cooldown(
        self, db_session, subscription, subscriber
    ) -> None:
        from datetime import UTC, datetime
        from unittest.mock import patch

        import pytest

        from app.models.catalog import SubscriptionStatus
        from app.models.enforcement_lock import EnforcementLock, EnforcementReason
        from app.services.customer_portal_flow_services import apply_service_suspend

        subscription.status = SubscriptionStatus.active
        db_session.commit()

        # Create a recent hold (within cooldown period)
        lock = EnforcementLock(
            subscription_id=subscription.id,
            subscriber_id=subscriber.id,
            reason=EnforcementReason.customer_hold,
            source="test",
            is_active=False,
            resolved_at=datetime.now(UTC),
            resolved_by="test",
            created_at=datetime.now(UTC),  # Just created today
        )
        db_session.add(lock)
        db_session.commit()

        # Mock the setting to have 7 day cooldown
        def mock_resolve_value(db, domain, key):
            if key == "suspend_cooldown_days":
                return 7
            if key == "customer_suspend_enabled":
                return True
            if key == "max_suspend_days":
                return 30
            if key == "max_suspend_holds_per_year":
                return 0  # Unlimited
            return None

        with patch(
            "app.services.settings_spec.resolve_value",
            side_effect=mock_resolve_value,
        ):
            with pytest.raises(ValueError, match="wait .* more day"):
                apply_service_suspend(
                    db_session,
                    {"account_id": subscriber.id},
                    str(subscription.id),
                    days=7,
                )

    def test_get_suspend_page_shows_block_reason_when_limit_reached(
        self, db_session, subscription, subscriber
    ) -> None:
        from datetime import UTC, datetime
        from unittest.mock import patch

        from app.models.catalog import SubscriptionStatus
        from app.models.enforcement_lock import EnforcementLock, EnforcementReason
        from app.services.customer_portal_flow_services import get_suspend_page

        subscription.status = SubscriptionStatus.active
        db_session.commit()

        # Create 1 hold (simulating max_holds_per_year=1)
        lock = EnforcementLock(
            subscription_id=subscription.id,
            subscriber_id=subscriber.id,
            reason=EnforcementReason.customer_hold,
            source="test",
            is_active=False,
            resolved_at=datetime.now(UTC),
            resolved_by="test",
            created_at=datetime.now(UTC),
        )
        db_session.add(lock)
        db_session.commit()

        def mock_resolve_value(db, domain, key):
            if key == "max_suspend_holds_per_year":
                return 1
            if key == "customer_suspend_enabled":
                return True
            if key == "max_suspend_days":
                return 30
            if key == "suspend_cooldown_days":
                return 0
            return None

        with patch(
            "app.services.settings_spec.resolve_value",
            side_effect=mock_resolve_value,
        ):
            result = get_suspend_page(
                db_session,
                {"account_id": subscriber.id},
                str(subscription.id),
            )

        assert result is not None
        assert result["can_suspend"] is False
        assert result["block_reason"] is not None
        assert "maximum" in result["block_reason"]


class TestVacationHoldCeleryTask:
    """Tests for the vacation hold auto-resume Celery task."""

    def test_resume_expired_holds_processes_expired_locks(
        self, db_session, subscription, subscriber
    ) -> None:
        from datetime import UTC, datetime, timedelta
        from unittest.mock import MagicMock, patch

        from app.models.catalog import SubscriptionStatus
        from app.models.enforcement_lock import EnforcementReason
        from app.services.account_lifecycle import suspend_subscription

        # First suspend the subscription properly
        subscription.status = SubscriptionStatus.active
        db_session.commit()

        lock = suspend_subscription(
            db_session,
            str(subscription.id),
            reason=EnforcementReason.customer_hold,
            source="test",
        )
        # Set resume_at to the past
        lock.resume_at = datetime.now(UTC) - timedelta(hours=1)
        db_session.commit()

        db_session.refresh(subscription)
        assert subscription.status == SubscriptionStatus.suspended

        # Import and run the task function directly
        from app.tasks.vacation_holds import resume_expired_holds

        # Create a mock session that delegates to db_session but doesn't close
        mock_session = MagicMock(wraps=db_session)
        mock_session.close = MagicMock()  # Don't close test session
        mock_session.commit = db_session.commit
        mock_session.rollback = db_session.rollback
        mock_session.scalars = db_session.scalars

        with patch("app.tasks.vacation_holds.SessionLocal", return_value=mock_session):
            result = resume_expired_holds()

        assert result["total"] >= 1
        assert result["resumed"] >= 1

        db_session.refresh(subscription)
        assert subscription.status == SubscriptionStatus.active

    def test_resume_expired_holds_skips_non_expired_locks(
        self, db_session, subscription, subscriber
    ) -> None:
        from datetime import UTC, datetime, timedelta
        from unittest.mock import MagicMock, patch

        from app.models.catalog import SubscriptionStatus
        from app.models.enforcement_lock import EnforcementReason
        from app.services.account_lifecycle import suspend_subscription

        # Suspend the subscription
        subscription.status = SubscriptionStatus.active
        db_session.commit()

        lock = suspend_subscription(
            db_session,
            str(subscription.id),
            reason=EnforcementReason.customer_hold,
            source="test",
        )
        # Set resume_at to the future
        lock.resume_at = datetime.now(UTC) + timedelta(days=7)
        db_session.commit()

        from app.tasks.vacation_holds import resume_expired_holds

        mock_session = MagicMock(wraps=db_session)
        mock_session.close = MagicMock()
        mock_session.commit = db_session.commit
        mock_session.rollback = db_session.rollback
        mock_session.scalars = db_session.scalars

        with patch("app.tasks.vacation_holds.SessionLocal", return_value=mock_session):
            result = resume_expired_holds()

        # Should not resume since resume_at is in the future
        assert result["resumed"] == 0

        db_session.refresh(subscription)
        assert subscription.status == SubscriptionStatus.suspended

    def test_resume_expired_holds_handles_errors_gracefully(
        self, db_session, subscription, subscriber
    ) -> None:
        from datetime import UTC, datetime, timedelta
        from unittest.mock import MagicMock, patch

        from app.models.catalog import SubscriptionStatus
        from app.models.enforcement_lock import EnforcementReason
        from app.services.account_lifecycle import suspend_subscription

        # Suspend the subscription
        subscription.status = SubscriptionStatus.active
        db_session.commit()

        lock = suspend_subscription(
            db_session,
            str(subscription.id),
            reason=EnforcementReason.customer_hold,
            source="test",
        )
        lock.resume_at = datetime.now(UTC) - timedelta(hours=1)
        db_session.commit()

        from app.tasks.vacation_holds import resume_expired_holds

        mock_session = MagicMock(wraps=db_session)
        mock_session.close = MagicMock()
        mock_session.commit = db_session.commit
        mock_session.rollback = db_session.rollback
        mock_session.scalars = db_session.scalars

        # Mock the lifecycle command owner to raise an error
        with patch("app.tasks.vacation_holds.SessionLocal", return_value=mock_session):
            with patch(
                "app.tasks.vacation_holds.execute_subscription_command",
                side_effect=Exception("Test error"),
            ):
                result = resume_expired_holds()

        # Should count as failed but not crash
        assert result["failed"] >= 1
        assert result["resumed"] == 0


class TestPlaywrightPortalRoutes:
    def test_usage_page_object_uses_canonical_portal_route(self) -> None:
        from tests.playwright.pages.customer.usage_page import CustomerUsagePage

        defaults = CustomerUsagePage.goto.__defaults__
        assert defaults is not None
        assert defaults[0] == "/portal/usage"


class TestDashboardLiveSessionCutover:
    def test_page_local_framed_ip_resolver_is_retired(self):
        from app.services import customer_portal_context

        assert not hasattr(customer_portal_context, "_live_framed_ips")
