import json
import uuid
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute
from starlette.requests import Request


def _bare_request(path: str = "/admin/customers/person/x/pppoe-password") -> Request:
    """Minimal request with empty state (no authenticated user) for unit calls."""
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("utf-8"),
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 5555),
            "server": ("testserver", 80),
        }
    )


from app.models.billing import Invoice, InvoiceStatus
from app.models.catalog import (
    AccessCredential,
    ConnectionType,
    Subscription,
    SubscriptionStatus,
)
from app.models.crm_sync_failure import CrmSyncFailure, CrmSyncFailureStatus
from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.network import SubscriberAdditionalRoute
from app.models.subscriber import (
    Address,
    Subscriber,
    SubscriberCategory,
    SubscriberStatus,
    UserType,
)
from app.models.subscription_engine import SettingValueType
from app.models.system_user import SystemUser, SystemUserType
from app.services.credential_crypto import encrypt_credential
from app.services.web_customer_details import (
    build_business_detail_snapshot,
    build_customer_detail_snapshot,
    build_person_detail_snapshot,
    reveal_customer_pppoe_password,
)
from app.web.admin import customers as customer_routes


def _billing_setting(key: str, value: str) -> DomainSetting:
    return DomainSetting(
        domain=SettingDomain.billing,
        key=key,
        value_type=SettingValueType.string,
        value_text=value,
        is_active=True,
    )


def test_person_detail_includes_billing_policy_override_snapshot(
    db_session, subscriber
):
    subscriber.user_type = UserType.customer
    db_session.add_all(
        [
            _billing_setting("billing_enabled", "true"),
            _billing_setting("billing_day", "1"),
            _billing_setting("payment_due_days", "14"),
            _billing_setting("minimum_balance", "0"),
        ]
    )
    subscriber.billing_day = 5
    subscriber.payment_due_days = 7
    subscriber.grace_period_days = 3
    subscriber.min_balance = Decimal("150.00")
    db_session.commit()

    context = build_person_detail_snapshot(db_session, str(subscriber.id))
    rows = {row["key"]: row for row in context["billing_policy"]["rows"]}

    assert context["billing_policy"]["has_overrides"] is True
    assert rows["billing_day"]["effective"] == "Day 5"
    assert rows["billing_day"]["source"] == "Customer override"
    assert rows["payment_due_days"]["effective"] == "7 day(s)"
    assert rows["payment_due_days"]["global"] == "14 day(s)"
    assert rows["grace_period_days"]["effective"] == "3 day(s)"


def test_business_detail_marks_mixed_billing_policy(db_session, subscriber):
    subscriber.user_type = UserType.customer
    db_session.add_all(
        [
            _billing_setting("billing_enabled", "true"),
            _billing_setting("payment_due_days", "14"),
        ]
    )
    subscriber.company_name = "Test Org"
    subscriber.legal_name = "Test Org Ltd"
    subscriber.tax_id = "RC-123"
    subscriber.domain = "test.example.com"
    subscriber.website = "https://test.example.com"
    subscriber.category = SubscriberCategory.business
    subscriber.payment_due_days = 21
    db_session.commit()

    context = build_business_detail_snapshot(db_session, str(subscriber.id))
    rows = {row["key"]: row for row in context["billing_policy"]["rows"]}

    assert context["organization"] is not None
    assert context["organization"].name == "Test Org"
    assert rows["payment_due_days"]["effective"] == "21 day(s)"
    assert rows["payment_due_days"]["source"] == "Customer override"


def test_person_detail_includes_json_safe_geocode_payload(db_session, subscriber):
    subscriber.user_type = UserType.customer
    address = Address(
        subscriber_id=subscriber.id,
        address_line1="123 Sample Street",
        address_line2="Suite 5",
        city="Lagos",
        region="LA",
        postal_code="100001",
        country_code="NG",
        is_primary=True,
    )
    db_session.add(address)
    db_session.commit()

    context = build_person_detail_snapshot(db_session, str(subscriber.id))

    assert context["geocode_target"] is not None
    assert context["geocode_target"]["payload"]["address_line1"] == "123 Sample Street"
    assert (
        json.loads(context["geocode_target"]["payload_json"])
        == context["geocode_target"]["payload"]
    )


def test_customer_detail_includes_active_additional_routes(db_session, subscriber):
    subscriber.user_type = UserType.customer
    db_session.add(
        SubscriberAdditionalRoute(
            subscriber_id=subscriber.id,
            cidr="102.220.189.10/32",
            prefix_length=32,
            metric=1,
            is_active=True,
            source="test",
        )
    )
    db_session.add(
        SubscriberAdditionalRoute(
            subscriber_id=subscriber.id,
            cidr="160.119.125.24/30",
            prefix_length=30,
            metric=1,
            is_active=False,
            source="test",
        )
    )
    db_session.commit()

    context = build_customer_detail_snapshot(db_session, str(subscriber.id))

    assert [route.cidr for route in context["active_additional_routes"]] == [
        "102.220.189.10/32"
    ]


def test_customer_detail_includes_crm_sync_link_status(db_session, subscriber):
    subscriber.user_type = UserType.customer
    crm_id = uuid.uuid4()
    subscriber.crm_subscriber_id = crm_id
    subscriber.metadata_ = {
        "crm_sync": {"last_success_at": "2026-07-07T12:00:00+00:00"}
    }
    db_session.commit()

    context = build_customer_detail_snapshot(db_session, str(subscriber.id))

    assert context["crm_sync_status"]["status"] == "linked"
    assert context["crm_sync_status"]["crm_subscriber_id"] == str(crm_id)
    assert context["crm_sync_status"]["last_success_display"] == (
        "2026-07-07T12:00:00+00:00"
    )
    assert context["customer_status_presentation"].value == subscriber.status.value
    assert (
        context["account_status_presentations"][str(subscriber.id)]
        == context["customer_status_presentation"]
    )
    assert context["account_health"].account_id == subscriber.id


def test_customer_360_rehomes_canonical_service_health() -> None:
    template = Path("templates/admin/customers/detail.html").read_text(encoding="utf-8")

    assert (
        'from "components/portal/account_health.html" import service_health_strip'
        not in template
    )
    assert (
        'service_health_strip(account_health, "/admin/catalog/subscriptions/")'
        not in template
    )
    assert "service_health_by_subscription" in template
    assert "{% set card_health = card.service_health %}" in template


def test_customer_360_service_health_contains_only_active_services(
    db_session, subscriber, subscription
):
    subscriber.user_type = UserType.customer
    subscription.status = SubscriptionStatus.active
    previous_service = Subscription(
        subscriber_id=subscriber.id,
        offer_id=subscription.offer_id,
        status=SubscriptionStatus.disabled,
        billing_mode=subscription.billing_mode,
    )
    db_session.add(previous_service)
    db_session.commit()

    context = build_customer_detail_snapshot(db_session, str(subscriber.id))

    assert [
        service.subscription_id for service in context["account_health"].services
    ] == [subscription.id]
    assert previous_service.id not in {
        service.subscription_id for service in context["account_health"].services
    }
    assert context["restorable_subscription_ids"] == {str(previous_service.id)}


def test_customer_360_places_contact_and_portal_access_side_by_side() -> None:
    template = Path("templates/admin/customers/detail.html").read_text(encoding="utf-8")

    assert '<div class="contents">' in template
    assert (
        'class="order-3 rounded-2xl border border-slate-200/60 bg-white shadow-sm'
    ) in template
    assert (
        'class="order-4 rounded-2xl border border-slate-200/60 bg-white shadow-sm'
    ) in template
    assert "lg:col-span-2" in template


def test_customer_360_restore_action_is_permission_gated_and_reviewed() -> None:
    template = Path("templates/admin/customers/detail.html").read_text(encoding="utf-8")

    assert (
        "{% if can_activate_subscriptions and subscription.id|string in "
        "restorable_subscription_ids %}"
    ) in template
    assert "@click=\"restoreSubscription('{{ subscription.id }}')\"" in template
    assert (
        "showAllSubscriptions: {{ ((stats.active_subscriptions | default(0)) == 0) "
        "| tojson }}"
    ) in template
    assert "Reason for restoring this subscription:" in template
    assert "`${lifecycleUrl}/preview`" in template
    assert "customer access will remain blocked" in template
    assert "preview.access_impact?.block_reason_after" in template
    assert "body.set('expected_head', preview.expected_head)" in template
    assert "'Idempotency-Key': idempotencyKey" in template
    assert "`${lifecycleUrl}/execute`" in template


def test_customer_360_unsuspend_action_is_permission_gated_and_reviewed() -> None:
    template = Path("templates/admin/customers/detail.html").read_text(encoding="utf-8")

    assert (
        "{% if can_unsuspend_account and account_unsuspend_action.visible %}"
        in template
    )
    assert (
        'action="/admin/customers/{{ customer_type }}/{{ customer.id }}/unsuspend"'
        in template
    )
    assert 'name="preview_fingerprint"' in template
    assert "Unsuspend Account" in template
    assert "disabled preserved" in template
    assert "Unrelated locks preserved" in template


def test_customer_unsuspend_route_requires_customer_update() -> None:
    path = "/customers/{customer_type}/{customer_id}/unsuspend"
    route = next(
        item
        for item in customer_routes.router.routes
        if isinstance(item, APIRoute) and item.path == path
    )

    assert route.methods == {"POST"}
    assert any(
        "customer:update"
        in " ".join(
            str(cell.cell_contents)
            for cell in (getattr(dependency.call, "__closure__", None) or ())
        )
        for dependency in route.dependant.dependencies
    )


def test_customer_detail_projects_safe_legacy_unsuspend(
    db_session, subscriber, subscription
):
    subscriber.user_type = UserType.customer
    subscriber.billing_enabled = True
    subscriber.status = SubscriberStatus.suspended
    subscriber.lifecycle_override_status = SubscriberStatus.suspended
    subscriber.lifecycle_override_reason = "Legacy administrative suspension"
    subscriber.lifecycle_override_source = "subscriber_service:update"
    subscription.status = SubscriptionStatus.active
    disabled = Subscription(
        subscriber_id=subscriber.id,
        offer_id=subscription.offer_id,
        status=SubscriptionStatus.disabled,
        billing_mode=subscription.billing_mode,
    )
    db_session.add(disabled)
    db_session.commit()

    context = build_customer_detail_snapshot(db_session, str(subscriber.id))
    action = context["account_unsuspend_action"]

    assert action["visible"] is True
    assert action["allowed"] is True
    assert action["current_status"] == "suspended"
    assert action["projected_status"] == "active"
    assert action["affected_subscription_count"] == 0
    assert action["preserved_disabled_count"] == 1
    assert len(action["fingerprint"]) == 64


def test_customer_dashboard_welcome_card_keeps_content_inset() -> None:
    template = Path("templates/customer/dashboard/index.html").read_text(
        encoding="utf-8"
    )

    assert "px-6 py-6 sm:px-8 sm:py-7" in template
    assert "min-w-0 flex-1" in template
    assert "break-words text-3xl" in template
    assert "max-w-2xl leading-relaxed" in template


def test_customer_detail_exposes_invoice_status_presentations(db_session, subscriber):
    subscriber.user_type = UserType.customer
    invoice = Invoice(
        account_id=subscriber.id,
        invoice_number="INV-DETAIL-001",
        status=InvoiceStatus.issued,
        subtotal=Decimal("100.00"),
        tax_total=Decimal("0.00"),
        total=Decimal("100.00"),
        balance_due=Decimal("100.00"),
        is_active=True,
    )
    db_session.add(invoice)
    db_session.commit()

    context = build_customer_detail_snapshot(db_session, str(subscriber.id))

    presentation = context["invoice_status_presentations"][str(invoice.id)]
    assert presentation.value == "issued"
    assert presentation.label == "Issued"


def test_customer_detail_includes_masked_identity_profile(db_session, subscriber):
    subscriber.user_type = UserType.customer
    subscriber.nin = "12345678901"
    subscriber.metadata_ = {"nin_verified": True}
    db_session.commit()

    context = build_customer_detail_snapshot(db_session, str(subscriber.id))

    assert context["identity_profile"]["nin_masked"] == "123456*****"
    assert context["identity_profile"]["nin_verified"] is True
    assert "date_of_birth" in context["identity_profile"]["missing"]


def test_customer_detail_includes_crm_sync_dead_letter(db_session, subscriber):
    subscriber.user_type = UserType.customer
    failure = CrmSyncFailure(
        entity="subscriber",
        external_id=str(subscriber.id),
        external_system="selfcare",
        payload={"status": "active"},
        error="CRM unavailable",
        attempts=9,
        status=CrmSyncFailureStatus.unresolved,
    )
    db_session.add(failure)
    db_session.commit()

    context = build_customer_detail_snapshot(db_session, str(subscriber.id))

    assert context["crm_sync_status"]["status"] == "failed"
    assert context["crm_sync_status"]["dead_letter_id"] == str(failure.id)
    assert context["crm_sync_status"]["error"] == "CRM unavailable"
    assert context["crm_sync_status"]["attempts"] == 9


def test_person_detail_exposes_pppoe_access_login(db_session, subscriber):
    subscriber.user_type = UserType.customer
    credential = AccessCredential(
        subscriber_id=subscriber.id,
        username="100025929",
        secret_hash=encrypt_credential("hYNAtwqj"),
        connection_type=ConnectionType.pppoe,
        is_active=True,
    )
    db_session.add(credential)
    db_session.commit()

    context = build_person_detail_snapshot(db_session, str(subscriber.id))

    assert context["pppoe_access"] == {
        "has_credential": True,
        "credential_id": str(credential.id),
        "login": "100025929",
        "has_password": True,
    }


def test_person_detail_preserves_disabled_service_network_access(
    db_session, subscriber, subscription
):
    subscriber.user_type = UserType.customer
    subscription.status = SubscriptionStatus.disabled
    subscription.login = "disabled-login"
    subscription.ipv4_address = "10.70.0.25"
    db_session.commit()

    context = build_person_detail_snapshot(db_session, str(subscriber.id))

    assert len(context["network_access_cards"]) == 1
    assert context["network_access_cards"][0]["status"] == "disabled"
    assert context["network_access_cards"][0]["login"] == "disabled-login"
    assert context["network_access_cards"][0]["ipv4_address"] == "10.70.0.25"


def test_person_detail_projects_subscription_status_for_network_access(
    db_session, subscriber, subscription
):
    subscriber.user_type = UserType.customer
    subscription.status = SubscriptionStatus.suspended
    subscription.login = "suspended-login"
    db_session.commit()

    context = build_person_detail_snapshot(db_session, str(subscriber.id))

    assert len(context["network_access_cards"]) == 1
    presentation = context["network_access_cards"][0]["status_presentation"]
    assert presentation.value == "suspended"
    assert presentation.tone.value == "warning"


def test_customer_detail_enables_access_repair_for_stale_active_projection(
    db_session, subscriber, subscription
):
    subscriber.user_type = UserType.customer
    subscriber.status = SubscriberStatus.blocked
    subscription.status = SubscriptionStatus.active
    subscription.access_state = "suspended"
    db_session.add(
        AccessCredential(
            subscriber_id=subscriber.id,
            username="repair-detail-1",
            is_active=True,
        )
    )
    db_session.commit()

    context = build_customer_detail_snapshot(db_session, str(subscriber.id))

    assert context["access_repair_state"]["enabled"] is True
    assert context["access_repair_state"]["needed"] is True
    assert "Repair needed" in context["access_repair_state"]["reason"]


def test_customer_detail_grays_access_repair_when_projection_is_active(
    db_session, subscriber, subscription
):
    subscriber.user_type = UserType.customer
    subscriber.status = SubscriberStatus.active
    subscription.status = SubscriptionStatus.active
    subscription.access_state = "active"
    db_session.add(
        AccessCredential(
            subscriber_id=subscriber.id,
            username="repair-detail-2",
            is_active=True,
        )
    )
    db_session.commit()

    context = build_customer_detail_snapshot(db_session, str(subscriber.id))

    assert context["access_repair_state"]["enabled"] is False
    assert context["access_repair_state"]["needed"] is False
    assert "No repair needed" in context["access_repair_state"]["reason"]


def test_reveal_customer_pppoe_password_is_customer_scoped(db_session, subscriber):
    subscriber.user_type = UserType.customer
    credential = AccessCredential(
        subscriber_id=subscriber.id,
        username="100025929",
        secret_hash=encrypt_credential("hYNAtwqj"),
        connection_type=ConnectionType.pppoe,
        is_active=True,
    )
    db_session.add(credential)
    db_session.commit()
    admin = SystemUser(
        first_name="Audit",
        last_name="Admin",
        email="audit-admin@example.com",
        user_type=SystemUserType.system_user,
        is_active=True,
    )
    db_session.add(admin)
    db_session.commit()

    password, found = reveal_customer_pppoe_password(
        db_session,
        str(subscriber.id),
        credential_id=str(credential.id),
    )
    missing_password, missing_found = reveal_customer_pppoe_password(
        db_session,
        str(subscriber.id),
        credential_id="00000000-0000-0000-0000-000000000000",
    )
    request = _bare_request()
    request.state.user = admin
    request.state.auth = {"principal_type": "system_user"}
    response = customer_routes.person_pppoe_password(
        request=request,
        customer_id=str(subscriber.id),
        credential_id=str(credential.id),
        db=db_session,
    )

    assert (password, found) == ("hYNAtwqj", True)
    assert (missing_password, missing_found) == ("", False)
    assert response.status_code == 200
    assert json.loads(response.body)["password"] == "hYNAtwqj"

    # The reveal must be audited (who revealed which customer's credential).
    from app.models.audit import AuditEvent

    audit = (
        db_session.query(AuditEvent)
        .filter(AuditEvent.action == "customer.pppoe_password_reveal")
        .filter(AuditEvent.entity_id == str(subscriber.id))
        .first()
    )
    assert audit is not None
    assert audit.is_success is True
    assert audit.actor_id == str(admin.id)
    assert audit.metadata_["actor_name"] == "Audit Admin"

    detail = build_customer_detail_snapshot(db_session, str(subscriber.id))
    reveal_activity = next(
        item
        for item in detail["activity_items"]
        if item["type"] == "audit"
        and item["title"] == "Subscriber Customer.Pppoe Password Reveal"
    )
    assert reveal_activity["actor_name"] == "Audit Admin"


def test_customer_detail_rejects_reseller_users(db_session):
    reseller_user = Subscriber(
        first_name="Mimi",
        last_name="David",
        email="reseller-detail@example.com",
        user_type=UserType.reseller,
        is_active=True,
    )
    db_session.add(reseller_user)
    db_session.commit()

    with pytest.raises(HTTPException) as exc:
        build_customer_detail_snapshot(db_session, str(reseller_user.id))

    assert exc.value.status_code == 404


def test_normalize_usage_period_tolerates_trailing_punctuation():
    assert customer_routes._normalize_usage_period("last,") == "last"
    assert customer_routes._normalize_usage_period("CURRENT!") == "current"
    assert customer_routes._normalize_usage_period("unexpected") == "current"


def test_customer_subscription_action_context_hides_unauthorized_actions(
    monkeypatch,
) -> None:
    request = SimpleNamespace(
        state=SimpleNamespace(auth={"principal_id": "operator-1"})
    )
    granted = {"subscription:activate"}
    monkeypatch.setattr(
        customer_routes,
        "has_permission",
        lambda auth, db, permission: permission in granted,
    )

    context = customer_routes._subscription_action_permission_context(
        request,
        object(),
    )

    assert context == {
        "can_activate_subscriptions": True,
        "can_suspend_subscriptions": False,
        "can_reconcile_service_changes": False,
    }


def test_person_detail_normalizes_usage_period(monkeypatch, db_session):
    captured: dict[str, object] = {}

    def _template_response(template_name, context, status_code=200):
        captured["template_name"] = template_name
        captured["context"] = context
        captured["status_code"] = status_code
        return SimpleNamespace(
            template_name=template_name,
            context=context,
            status_code=status_code,
        )

    monkeypatch.setattr(
        customer_routes,
        "templates",
        SimpleNamespace(TemplateResponse=_template_response),
    )
    monkeypatch.setattr(
        customer_routes.web_customer_details_service,
        "build_customer_detail_snapshot",
        # **kwargs absorbs include_conversations, which the route now decides
        # from the principal's permissions and passes down.
        lambda db, customer_id, **kwargs: {"customer": SimpleNamespace(id=customer_id)},
    )
    monkeypatch.setattr(
        customer_routes.web_notifications_service,
        "bulk_notification_setup_context",
        lambda db: {},
    )

    import app.web.admin as admin_module

    monkeypatch.setattr(admin_module, "get_current_user", lambda request: None)
    monkeypatch.setattr(admin_module, "get_sidebar_stats", lambda db: {})

    response = customer_routes.person_detail(
        request=SimpleNamespace(headers={}),
        customer_id="cust-123",
        usage_period="last,",
        usage_page=2,
        usage_per_page=50,
        db=db_session,
    )

    assert response.status_code == 200
    assert captured["template_name"] == "admin/customers/detail.html"
    assert captured["context"]["pppoe_access"] == {
        "has_credential": False,
        "credential_id": None,
        "login": None,
        "has_password": False,
    }
    assert captured["context"]["usage_period"] == "last"
    assert captured["context"]["customer_type"] == "person"
    assert captured["context"]["bulk_notification_channels"] == []
    assert captured["context"]["bulk_notification_templates"] == []
    assert captured["context"]["can_activate_subscriptions"] is False
    assert captured["context"]["can_suspend_subscriptions"] is False
    assert captured["context"]["can_unsuspend_account"] is False
    assert captured["context"]["detail_config"] == {
        "statsUrl": (
            "/admin/customers/person/cust-123/stats"
            "?usage_period=last&usage_page=2&usage_per_page=50"
        ),
        "detailUrl": "/admin/customers/person/cust-123",
        "customerId": "cust-123",
        "customerType": "person",
        "notificationChannels": [],
        "notificationTemplates": [],
    }
    json.dumps(captured["context"]["detail_config"])


def test_person_detail_stats_normalizes_usage_period(monkeypatch, db_session):
    captured: dict[str, object] = {}

    def _template_response(template_name, context, status_code=200):
        captured["template_name"] = template_name
        captured["context"] = context
        captured["status_code"] = status_code
        return SimpleNamespace(
            template_name=template_name,
            context=context,
            status_code=status_code,
        )

    def _get_usage_page(
        db,
        usage_customer,
        *,
        period,
        page,
        per_page,
        allow_postgres_fallback,
    ):
        captured["period"] = period
        return {
            "usage_records": [],
            "period": period,
            "page": page,
            "per_page": per_page,
            "total": 0,
            "total_pages": 1,
            "usage_summary": {},
            "fup_status": None,
            "usage_source": "none",
            "has_subscription": False,
        }

    monkeypatch.setattr(
        customer_routes,
        "templates",
        SimpleNamespace(TemplateResponse=_template_response),
    )
    monkeypatch.setattr(
        customer_routes,
        "_get_subscriber",
        lambda db, subscriber_id: SimpleNamespace(id=subscriber_id),
    )
    monkeypatch.setattr(
        customer_routes.customer_portal,
        "get_usage_page",
        _get_usage_page,
    )
    monkeypatch.setattr(
        customer_routes,
        "resolve_customer_subscription",
        lambda db, usage_customer: SimpleNamespace(id="sub-123"),
    )
    monkeypatch.setattr(
        customer_routes,
        "_load_initial_bandwidth_stats",
        lambda db, subscription_id: {"current_rx_formatted": "1.20 Mbps"},
    )

    response = customer_routes.person_detail_stats(
        request=SimpleNamespace(headers={}),
        customer_id="cust-123",
        usage_period="last,",
        usage_page=1,
        usage_per_page=25,
        db=db_session,
    )

    assert response.status_code == 200
    assert captured["period"] == "last"
    assert captured["template_name"] == "admin/customers/_stats_panel.html"
    assert captured["context"]["usage_portal"]["period"] == "last"
    assert captured["context"]["usage_subscription_id"] == "sub-123"
    assert captured["context"]["bandwidth_chart_initial_stats"] == {
        "current_rx_formatted": "1.20 Mbps"
    }


def test_admin_customer_stats_templates_register_portal_datetime_filter():
    portal_datetime = customer_routes.templates.env.filters.get("portal_datetime")

    assert portal_datetime is not None
    assert portal_datetime(None, "%b %d, %Y", "N/A") == "N/A"


def test_customer_detail_snapshot_includes_pending_location_request(
    db_session, subscriber
):
    from app.models.gis import (
        CustomerLocationChangeRequest,
        CustomerLocationChangeRequestStatus,
    )

    subscriber.user_type = UserType.customer
    db_session.add(
        Address(
            subscriber_id=subscriber.id,
            address_line1="12 Service Lane",
            city="Abuja",
            is_primary=True,
        )
    )
    db_session.add(
        CustomerLocationChangeRequest(
            subscriber_id=subscriber.id,
            status=CustomerLocationChangeRequestStatus.pending,
            current_latitude=9.0501,
            current_longitude=7.4902,
            requested_latitude=9.0512,
            requested_longitude=7.4923,
            customer_note="Pin is one compound away.",
        )
    )
    db_session.commit()

    context = build_customer_detail_snapshot(db_session, str(subscriber.id))

    pending = context["pending_location_request"]
    assert pending is not None
    assert pending.status == CustomerLocationChangeRequestStatus.pending
    assert pending.customer_note == "Pin is one compound away."


def test_customer_detail_snapshot_pending_location_request_none_when_absent(
    db_session, subscriber
):
    subscriber.user_type = UserType.customer
    db_session.commit()

    context = build_customer_detail_snapshot(db_session, str(subscriber.id))

    assert context["pending_location_request"] is None
