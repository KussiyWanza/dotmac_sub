import json
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest
from starlette.datastructures import FormData

from app.models.billing import AccountAdjustment, InvoiceLine, TaxRate
from app.models.catalog import (
    AccessCredential,
    AddOn,
    AddOnPrice,
    AddOnType,
    BillingMode,
    NasDevice,
    PriceType,
    Subscription,
    SubscriptionAddOn,
    SubscriptionStatus,
)
from app.models.connector import ConnectorAuthType, ConnectorConfig, ConnectorType
from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.event_store import EventStatus, EventStore
from app.models.network import (
    IPAssignment,
    IPv4Address,
    IPVersion,
    SubscriberAdditionalRoute,
)
from app.models.notification import (
    Notification,
    NotificationChannel,
    NotificationStatus,
    NotificationTemplate,
)
from app.models.radius import (
    RadiusClient,
    RadiusServer,
    RadiusSyncJob,
    RadiusSyncRun,
    RadiusSyncStatus,
    RadiusUser,
)
from app.models.sequence import DocumentSequence
from app.models.subscriber import Address, ChannelType, Subscriber, SubscriberChannel
from app.models.subscription_engine import SettingValueType
from app.schemas.catalog import SubscriptionCreate
from app.services import auth_flow as auth_flow_service
from app.services import catalog as catalog_service
from app.services import (
    web_catalog_subscription_workflows as web_catalog_subscription_workflows_service,
)
from app.services import web_catalog_subscriptions as web_catalog_subscriptions_service
from app.services import web_network_ip as web_network_ip_service
from app.services.subscription_lifecycle import (
    SubscriptionCommandOutcome,
    SubscriptionCommandOutcomeStatus,
)


def _public_ip_addon(
    db_session,
    *,
    name="/29 IP",
    ip_is_public=True,
    addon_type=AddOnType.custom,
    ip_prefix_length=29,
    with_price=True,
) -> AddOn:
    add_on = AddOn(
        name=name,
        addon_type=addon_type,
        is_active=True,
        ip_is_public=ip_is_public,
        ip_prefix_length=ip_prefix_length,
    )
    db_session.add(add_on)
    db_session.flush()
    if with_price:
        db_session.add(
            AddOnPrice(
                add_on_id=add_on.id,
                price_type=PriceType.recurring,
                amount=Decimal("1000.00"),
                currency="NGN",
                is_active=True,
            )
        )
    db_session.commit()
    db_session.refresh(add_on)
    return add_on


def _billing_setting(key: str, value: str) -> DomainSetting:
    return DomainSetting(
        domain=SettingDomain.billing,
        key=key,
        value_text=value,
        is_active=True,
    )


def _radius_setting(
    key: str,
    value_type: SettingValueType,
    *,
    value_text: str | None = None,
    value_json: object | None = None,
) -> DomainSetting:
    return DomainSetting(
        domain=SettingDomain.radius,
        key=key,
        value_type=value_type,
        value_text=value_text,
        value_json=value_json,
        is_active=True,
    )


def test_apply_generated_service_credentials_keeps_custom_password_on_edit(
    db_session,
    subscriber,
):
    subscriber.subscriber_number = "SUB-004167"
    db_session.commit()

    form_data = {
        "id": "existing-subscription-id",
        "subscriber_id": str(subscriber.id),
        "account_id": str(subscriber.id),
        "service_password": "CustomPass123",
    }

    web_catalog_subscriptions_service.apply_generated_service_credentials(
        db_session, form_data
    )

    assert form_data["login"] == "10004167"
    assert form_data["service_password"] == "CustomPass123"


def test_apply_generated_service_credentials_skips_legacy_defaults_when_pppoe_auto_generation_enabled(
    db_session,
    subscriber,
):
    subscriber.subscriber_number = "SUB-004167"
    db_session.add(
        _radius_setting(
            "pppoe_auto_generate_enabled",
            SettingValueType.boolean,
            value_text="true",
            value_json=True,
        )
    )
    db_session.commit()

    form_data = {
        "subscriber_id": str(subscriber.id),
        "account_id": str(subscriber.id),
        "login": "",
        "service_password": "",
    }

    web_catalog_subscriptions_service.apply_generated_service_credentials(
        db_session,
        form_data,
    )

    assert form_data["login"] == ""
    assert form_data["service_password"] == ""


def test_upsert_access_credential_does_not_clear_password_when_blank_on_edit(
    db_session,
    subscriber,
    catalog_offer,
):
    subscriber.subscriber_number = "SUB-004167"
    db_session.commit()

    subscription = catalog_service.subscriptions.create(
        db_session,
        SubscriptionCreate(
            account_id=subscriber.id,
            offer_id=catalog_offer.id,
        ),
    )

    web_catalog_subscriptions_service._upsert_access_credential(
        db_session,
        subscriber_id=subscriber.id,
        username="10004167",
        plain_password="InitialPass123",
        radius_profile_id=None,
    )

    credential = (
        db_session.query(AccessCredential)
        .filter(AccessCredential.subscriber_id == subscriber.id)
        .one()
    )
    original_hash = credential.secret_hash
    assert auth_flow_service.verify_password("InitialPass123", original_hash)

    web_catalog_subscriptions_service.update_subscription_with_audit(
        db_session,
        str(subscription.id),
        {"login": "10004167"},
        "",
        [],
        [],
        None,
        None,
    )

    db_session.refresh(credential)
    assert credential.username == "10004167"
    assert credential.secret_hash == original_hash
    assert auth_flow_service.verify_password("InitialPass123", credential.secret_hash)


def test_generic_edit_preserves_lifecycle_and_commercial_facts(
    db_session,
    subscriber,
    catalog_offer,
):
    subscription = catalog_service.subscriptions.create(
        db_session,
        SubscriptionCreate(
            account_id=subscriber.id,
            offer_id=catalog_offer.id,
            status=SubscriptionStatus.active,
        ),
    )
    subscription.start_at = datetime(2026, 7, 1, tzinfo=UTC)
    subscription.next_billing_at = datetime(2026, 8, 1, tzinfo=UTC)
    db_session.commit()
    original_offer_id = subscription.offer_id
    original_billing_mode = subscription.billing_mode

    web_catalog_subscriptions_service.update_subscription_with_audit(
        db_session,
        str(subscription.id),
        {
            "offer_id": uuid4(),
            "status": SubscriptionStatus.suspended,
            "billing_mode": (
                BillingMode.postpaid
                if original_billing_mode == BillingMode.prepaid
                else BillingMode.prepaid
            ),
            "start_at": datetime(2025, 1, 1, tzinfo=UTC),
            "next_billing_at": datetime(2025, 2, 1, tzinfo=UTC),
            "end_at": datetime(2025, 3, 1, tzinfo=UTC),
            "canceled_at": datetime(2025, 3, 1, tzinfo=UTC),
            "cancel_reason": "bypass lifecycle owner",
            "service_description": "technical metadata remains editable",
        },
        "",
        [],
        [],
        None,
        None,
    )

    db_session.refresh(subscription)
    assert subscription.offer_id == original_offer_id
    assert subscription.status == SubscriptionStatus.active
    assert subscription.billing_mode == original_billing_mode
    assert subscription.start_at.date().isoformat() == "2026-07-01"
    assert subscription.next_billing_at.date().isoformat() == "2026-08-01"
    assert subscription.end_at is None
    assert subscription.canceled_at is None
    assert subscription.cancel_reason is None
    assert subscription.service_description == "technical metadata remains editable"


def test_upsert_access_credential_prefers_active_matching_username(
    db_session,
    subscriber,
):
    active_credential = AccessCredential(
        subscriber_id=subscriber.id,
        username="100015322",
        secret_hash=auth_flow_service.hash_service_secret("OldPass123"),
        is_active=True,
        created_at=datetime.now(UTC) - timedelta(days=1),
    )
    inactive_newer_credential = AccessCredential(
        subscriber_id=subscriber.id,
        username="100015322_1",
        secret_hash=auth_flow_service.hash_service_secret("HistoricalPass123"),
        is_active=False,
        created_at=datetime.now(UTC),
    )
    db_session.add_all([active_credential, inactive_newer_credential])
    db_session.commit()

    web_catalog_subscriptions_service._upsert_access_credential(
        db_session,
        subscriber_id=subscriber.id,
        username="100015322",
        plain_password="NewPass123",
        radius_profile_id=None,
    )

    db_session.refresh(active_credential)
    db_session.refresh(inactive_newer_credential)
    assert active_credential.username == "100015322"
    assert active_credential.is_active is True
    assert auth_flow_service.verify_password(
        "NewPass123", active_credential.secret_hash
    )
    assert inactive_newer_credential.username == "100015322_1"
    assert inactive_newer_credential.is_active is False


def test_upsert_access_credential_rejects_username_owned_by_other_subscriber(
    db_session,
    subscriber,
):
    other = Subscriber(first_name="Other", last_name="User", email="other@example.com")
    db_session.add(other)
    db_session.commit()
    db_session.add(
        AccessCredential(
            subscriber_id=other.id,
            username="100015322",
            secret_hash=auth_flow_service.hash_service_secret("OtherPass123"),
            is_active=True,
        )
    )
    db_session.commit()

    with pytest.raises(ValueError) as exc:
        web_catalog_subscriptions_service._upsert_access_credential(
            db_session,
            subscriber_id=subscriber.id,
            username="100015322",
            plain_password="NewPass123",
            radius_profile_id=None,
        )

    assert str(exc.value) == (
        "Service login 100015322 is already used by another customer or credential."
    )
    assert db_session.query(AccessCredential).count() == 1


def test_upsert_access_credential_stores_service_password_in_reversible_format(
    db_session,
    subscriber,
):
    web_catalog_subscriptions_service._upsert_access_credential(
        db_session,
        subscriber_id=subscriber.id,
        username="10004167",
        plain_password="InitialPass123",
        radius_profile_id=None,
    )

    credential = (
        db_session.query(AccessCredential)
        .filter(AccessCredential.subscriber_id == subscriber.id)
        .one()
    )

    assert credential.secret_hash.startswith(("plain:", "enc:"))
    assert auth_flow_service.verify_password("InitialPass123", credential.secret_hash)


def test_subscription_form_context_exposes_current_service_password(
    db_session,
    subscriber,
):
    web_catalog_subscriptions_service._upsert_access_credential(
        db_session,
        subscriber_id=subscriber.id,
        username="10004167",
        plain_password="VisiblePass123",
        radius_profile_id=None,
    )

    context = web_catalog_subscriptions_service.subscription_form_context(
        db_session,
        {
            "id": "sub-1",
            "subscriber_id": str(subscriber.id),
            "account_id": str(subscriber.id),
            "login": "10004167",
            "service_password": "",
        },
    )

    assert context["current_service_login"] == "10004167"
    assert context["current_service_password"] == "VisiblePass123"


def test_subscription_detail_context_resolves_commercial_policy_from_customer_and_globals(
    db_session,
    subscription,
    subscriber,
):
    db_session.add_all(
        [
            _billing_setting("billing_day", "1"),
            _billing_setting("payment_due_days", "14"),
            _billing_setting("minimum_balance", "50.00"),
        ]
    )
    subscriber.payment_method = "bank_transfer"
    subscriber.billing_day = 6
    subscriber.grace_period_days = 3
    subscriber.min_balance = Decimal("125.00")
    db_session.commit()

    context = web_catalog_subscriptions_service.subscription_detail_context(
        db_session,
        subscription,
    )

    rows = {row["key"]: row for row in context["commercial_policy"]["rows"]}
    assert rows["billing_mode"]["source"] == "Subscription"
    assert rows["contract_term"]["source"] == "Subscription"
    assert rows["payment_method"]["effective"] == "bank_transfer"
    assert rows["payment_method"]["source"] == "Customer override"
    assert rows["billing_day"]["effective"] == "Day 6"
    assert rows["billing_day"]["global"] == "Day 1"
    assert rows["payment_due_days"]["effective"] == "14 day(s)"
    assert rows["payment_due_days"]["source"] == "Global default"
    assert rows["grace_period_days"]["effective"] == "3 day(s)"
    assert rows["min_balance"]["effective"] == "NGN 125.00"
    assert rows["tax_rate"]["effective"] == "Not set"


def test_subscription_form_context_prefers_service_address_tax_for_commercial_policy(
    db_session,
    subscription,
    subscriber,
):
    subscriber_tax = TaxRate(name="Customer VAT", rate=Decimal("0.075"))
    address_tax = TaxRate(name="Service VAT", rate=Decimal("0.050"))
    db_session.add_all([subscriber_tax, address_tax])
    db_session.commit()

    subscriber.tax_rate_id = subscriber_tax.id
    address = Address(
        subscriber_id=subscriber.id,
        address_line1="123 Service Street",
        tax_rate_id=address_tax.id,
    )
    db_session.add(address)
    db_session.commit()

    subscription.service_address_id = address.id
    db_session.commit()

    context = web_catalog_subscriptions_service.subscription_form_context(
        db_session,
        web_catalog_subscriptions_service.edit_form_data(db_session, subscription),
    )

    rows = {row["key"]: row for row in context["commercial_policy"]["rows"]}
    assert rows["tax_rate"]["effective"] == "Service VAT (5.00%)"
    assert rows["tax_rate"]["source"] == "Service address"
    assert rows["tax_rate"]["override"] == "Customer VAT (7.50%)"


def test_subscription_form_context_exposes_plain_offer_labels(
    db_session,
    catalog_offer,
):
    context = web_catalog_subscriptions_service.subscription_form_context(
        db_session,
        {
            "offer_id": str(catalog_offer.id),
        },
    )

    option = next(
        item for item in context["offer_options"] if item["id"] == str(catalog_offer.id)
    )
    assert option["name"] == catalog_offer.name
    assert option["label"].startswith(catalog_offer.name)
    assert isinstance(option["price_summary"], str)


def test_subscription_form_context_exposes_json_safe_ipv4_blocks(db_session):
    context = web_catalog_subscriptions_service.subscription_form_context(
        db_session,
        {},
    )

    assert isinstance(context["ipv4_blocks_json"], str)


def test_subscription_form_context_uses_ipam_ranges_for_ipv4_blocks(
    db_session,
):
    pool, pool_error = web_network_ip_service.create_ip_pool(
        db_session,
        {
            "name": "Kubwa Fallback IP",
            "ip_version": "ipv4",
            "cidr": "10.82.0.0/30",
            "is_active": True,
        },
    )
    assert pool_error is None
    assert pool is not None
    reserved = IPv4Address(
        address="10.82.0.1",
        pool_id=pool.id,
        is_reserved=True,
    )
    db_session.add(reserved)
    db_session.commit()

    context = web_catalog_subscriptions_service.subscription_form_context(
        db_session,
        {},
    )

    options = json.loads(context["ipv4_blocks_json"])
    option = next(item for item in options if item["cidr"] == "10.82.0.0/30")
    assert option["name"] == "Kubwa Fallback IP"
    assert option["id"] == f"pool:{pool.id}:10.82.0.0/30"
    assert "available_ips" not in option
    assert option["display"] == "Kubwa Fallback IP - 10.82.0.0/30"

    available = web_catalog_subscriptions_service.available_ipv4_addresses_for_selector(
        db_session,
        selector=option["id"],
    )
    assert available["addresses"] == ["10.82.0.2"]
    assert available["available_count"] == 1


def test_parse_subscription_form_keeps_single_ipv4_row():
    form = FormData(
        [
            ("ipv4_block_ids", "first-block"),
            ("ipv4_block_ids", "second-block"),
            ("ipv4_addresses", "10.0.0.2"),
            ("ipv4_addresses", "10.0.0.3"),
        ]
    )

    parsed = web_catalog_subscriptions_service.parse_subscription_form(form)

    assert parsed["ipv4_block_ids"] == ["first-block"]
    assert parsed["ipv4_addresses"] == ["10.0.0.2"]


def test_parse_subscription_create_rejects_forged_lifecycle_facts():
    form = FormData(
        {
            "status": "active",
            "start_at": "2025-01-01T00:00",
            "end_at": "2025-12-31T00:00",
            "next_billing_at": "2025-02-01T00:00",
            "canceled_at": "2025-01-02T00:00",
            "cancel_reason": "bypass lifecycle owner",
        }
    )

    parsed = web_catalog_subscriptions_service.parse_subscription_form(form)

    assert parsed["status"] == "pending"
    assert parsed["requested_status"] == "active"
    for field in (
        "start_at",
        "end_at",
        "next_billing_at",
        "canceled_at",
        "cancel_reason",
    ):
        assert parsed[field] == ""


def test_activate_after_create_stages_pending_before_canonical_command():
    payload = {
        "status": "active",
        "start_at": "2025-01-01T00:00:00+00:00",
        "next_billing_at": "2025-02-01T00:00:00+00:00",
    }

    flags = web_catalog_subscriptions_service.apply_create_quick_options(
        payload,
        FormData({"status": "active"}),
    )

    assert flags == (True, False, False)
    assert payload == {"status": "pending"}


def test_subscription_create_activates_through_canonical_lifecycle(
    db_session,
    subscriber,
    catalog_offer,
):
    result = web_catalog_subscription_workflows_service.handle_subscription_create_form(
        db_session,
        form=FormData(
            {
                "account_id": str(subscriber.id),
                "offer_id": str(catalog_offer.id),
                "status": "active",
                "start_at": "2025-01-01T00:00",
                "ipv4_method": "dynamic",
            }
        ),
        request=None,
        actor_id="operator-1",
    )

    created = (
        db_session.query(Subscription)
        .filter(Subscription.subscriber_id == subscriber.id)
        .filter(Subscription.offer_id == catalog_offer.id)
        .one()
    )
    db_session.refresh(created)
    assert created.status == SubscriptionStatus.active
    assert created.start_at is not None
    assert result["redirect_url"].endswith(f"/{subscriber.id}#subscriptions")


def test_subscription_create_keeps_pending_and_redirects_to_record_on_activation_failure(
    db_session,
    subscriber,
    catalog_offer,
    monkeypatch,
):
    captured = {}

    def fail_activation(db, command, **kwargs):
        captured["command"] = command
        return SubscriptionCommandOutcome(
            command=command,
            status=SubscriptionCommandOutcomeStatus.failed,
            message="Provisioning owner unavailable",
            previous_head=str(command.expected_head),
            current_head=str(command.expected_head),
            error_code="command_execution_failed",
        )

    monkeypatch.setattr(
        web_catalog_subscription_workflows_service,
        "execute_subscription_command",
        fail_activation,
    )

    result = web_catalog_subscription_workflows_service.handle_subscription_create_form(
        db_session,
        form=FormData(
            {
                "account_id": str(subscriber.id),
                "offer_id": str(catalog_offer.id),
                "status": "active",
                "ipv4_method": "dynamic",
            }
        ),
        request=None,
        actor_id="operator-2",
    )

    created = (
        db_session.query(Subscription)
        .filter(Subscription.subscriber_id == subscriber.id)
        .filter(Subscription.offer_id == catalog_offer.id)
        .one()
    )
    assert created.status == SubscriptionStatus.pending
    assert captured["command"].expected_head
    assert captured["command"].idempotency_key == (
        f"admin-create-activate:{created.id}"
    )
    assert result["redirect_url"].startswith(
        f"/admin/catalog/subscriptions/{created.id}?error="
    )
    assert "Provisioning+owner+unavailable" in result["redirect_url"]


@pytest.mark.parametrize(
    ("selected_status", "expected_status"),
    [
        ("pending", SubscriptionStatus.pending),
        ("suspended", SubscriptionStatus.suspended),
        ("disabled", SubscriptionStatus.disabled),
        ("canceled", SubscriptionStatus.canceled),
    ],
)
def test_subscription_create_applies_selected_lifecycle_state(
    db_session,
    subscriber,
    catalog_offer,
    selected_status,
    expected_status,
):
    result = web_catalog_subscription_workflows_service.handle_subscription_create_form(
        db_session,
        form=FormData(
            {
                "account_id": str(subscriber.id),
                "offer_id": str(catalog_offer.id),
                "status": selected_status,
                "ipv4_method": "dynamic",
            }
        ),
        request=None,
        actor_id="operator-3",
    )

    created = (
        db_session.query(Subscription)
        .filter(Subscription.subscriber_id == subscriber.id)
        .filter(Subscription.offer_id == catalog_offer.id)
        .one()
    )
    db_session.refresh(created)
    assert created.status == expected_status
    assert result["redirect_url"].endswith(f"/{subscriber.id}#subscriptions")


def test_subscription_detail_context_exposes_events_notifications_and_radius_sync(
    db_session,
    subscription,
    subscriber,
):
    web_catalog_subscriptions_service._upsert_access_credential(
        db_session,
        subscriber_id=subscriber.id,
        username="105000111",
        plain_password="VisiblePass123",
        radius_profile_id=None,
    )
    credential = (
        db_session.query(AccessCredential)
        .filter(AccessCredential.subscriber_id == subscriber.id)
        .one()
    )

    nas_device = NasDevice(
        name="Karu", nas_ip="172.16.0.1", shared_secret="plain:secret"
    )
    radius_server = RadiusServer(name="Primary RADIUS", host="127.0.0.1")
    db_session.add_all([nas_device, radius_server])
    db_session.flush()

    subscription.provisioning_nas_device_id = nas_device.id
    db_session.add(
        RadiusUser(
            subscriber_id=subscriber.id,
            subscription_id=subscription.id,
            access_credential_id=credential.id,
            username=credential.username,
            secret_hash=credential.secret_hash,
            is_active=True,
            last_sync_at=datetime(2026, 3, 21, 10, 0, tzinfo=UTC),
        )
    )
    db_session.add(
        RadiusClient(
            server_id=radius_server.id,
            nas_device_id=nas_device.id,
            client_ip="172.16.0.1",
            shared_secret_hash="hashed",
            description="Karu",
            is_active=True,
        )
    )
    db_session.add(
        EventStore(
            event_id=uuid4(),
            event_type="subscription.suspended",
            payload={
                "from_status": "active",
                "to_status": "suspended",
                "reason": "dunning",
            },
            status=EventStatus.failed,
            subscription_id=subscription.id,
            account_id=subscriber.id,
            failed_handlers=[{"handler": "NotificationHandler", "error": "smtp"}],
        )
    )
    # Get existing template or create test-specific one
    template = (
        db_session.query(NotificationTemplate)
        .filter(
            NotificationTemplate.code == "subscription_suspended",
            NotificationTemplate.channel == NotificationChannel.email,
        )
        .first()
    )
    if template is None:
        template = NotificationTemplate(
            name="Subscription Suspended",
            code="subscription_suspended",
            channel=NotificationChannel.email,
            body="Suspended",
            is_active=True,
        )
        db_session.add(template)
        db_session.flush()
    db_session.add(
        Notification(
            template_id=template.id,
            channel=NotificationChannel.email,
            recipient=subscriber.email,
            subject="Service suspended",
            body="Suspended",
            status=NotificationStatus.queued,
        )
    )
    db_session.commit()

    context = web_catalog_subscriptions_service.subscription_detail_context(
        db_session,
        subscription,
    )

    assert context["domain_events"][0]["event_type"] == "subscription.suspended"
    assert context["domain_events"][0]["failed_handler_text"] == "NotificationHandler"
    assert (
        context["notification_evidence"]["items"][0]["template_code"]
        == "subscription_suspended"
    )
    assert context["radius_sync_evidence"]["internal_user"].username == "105000111"
    assert context["radius_sync_evidence"]["nas_client_count"] == 1


def test_subscription_detail_context_exposes_latest_external_radius_sync_run(
    db_session,
    subscription,
    subscriber,
):
    radius_server = RadiusServer(name="External RADIUS", host="127.0.0.2")
    connector = ConnectorConfig(
        name="External RADIUS DB",
        connector_type=ConnectorType.custom,
        auth_type=ConnectorAuthType.none,
        is_active=True,
    )
    db_session.add_all([radius_server, connector])
    db_session.flush()

    sync_job = RadiusSyncJob(
        name="Nightly external sync",
        server_id=radius_server.id,
        connector_config_id=connector.id,
        sync_users=True,
        sync_nas_clients=True,
        is_active=True,
    )
    db_session.add(sync_job)
    db_session.flush()
    db_session.add(
        RadiusSyncRun(
            job_id=sync_job.id,
            status=RadiusSyncStatus.success,
            details={
                "external_users_synced": 12,
                "external_nas_synced": 4,
                "credentials_scanned": 12,
                "nas_devices_synced": 4,
            },
        )
    )
    db_session.commit()

    context = web_catalog_subscriptions_service.subscription_detail_context(
        db_session,
        subscription,
    )

    assert context["radius_sync_evidence"]["external_job_count"] == 1
    assert (
        context["radius_sync_evidence"]["external_run"].status
        == RadiusSyncStatus.success
    )
    assert context["radius_sync_evidence"]["external_users_synced"] == 12
    assert context["radius_sync_evidence"]["external_nas_synced"] == 4


def test_subscription_detail_context_reads_live_external_radius_rows(
    db_session,
    subscription,
    subscriber,
    tmp_path,
):
    web_catalog_subscriptions_service._upsert_access_credential(
        db_session,
        subscriber_id=subscriber.id,
        username="105000222",
        plain_password="SecretPass123",
        radius_profile_id=None,
    )

    db_path = tmp_path / "radius_external.sqlite"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE radcheck (username TEXT, attribute TEXT, op TEXT, value TEXT)"
    )
    conn.execute(
        "CREATE TABLE radreply (username TEXT, attribute TEXT, op TEXT, value TEXT)"
    )
    conn.execute(
        "CREATE TABLE radusergroup (username TEXT, groupname TEXT, priority INTEGER)"
    )
    conn.execute(
        "INSERT INTO radcheck (username, attribute, op, value) VALUES (?, ?, ?, ?)",
        ("105000222", "Cleartext-Password", ":=", "SecretPass123"),
    )
    conn.execute(
        "INSERT INTO radreply (username, attribute, op, value) VALUES (?, ?, ?, ?)",
        ("105000222", "Framed-Pool", ":=", "pppoe-karu"),
    )
    conn.execute(
        "INSERT INTO radusergroup (username, groupname, priority) VALUES (?, ?, ?)",
        ("105000222", "fiber-1000gb", 0),
    )
    conn.commit()
    conn.close()

    radius_server = RadiusServer(name="SQLite RADIUS", host="127.0.0.3")
    connector = ConnectorConfig(
        name="SQLite external RADIUS",
        connector_type=ConnectorType.custom,
        auth_type=ConnectorAuthType.none,
        base_url=f"sqlite:///{db_path}",
        is_active=True,
    )
    db_session.add_all([radius_server, connector])
    db_session.flush()
    db_session.add(
        RadiusSyncJob(
            name="SQLite sync",
            server_id=radius_server.id,
            connector_config_id=connector.id,
            sync_users=True,
            sync_nas_clients=True,
            is_active=True,
        )
    )
    db_session.commit()

    context = web_catalog_subscriptions_service.subscription_detail_context(
        db_session,
        subscription,
    )

    source = context["external_radius_rows"][0]
    assert source["available"] is True
    assert source["radcheck"][0]["attribute"] == "Cleartext-Password"
    assert source["radcheck"][0]["value"] == "••••••••"
    assert source["radreply"][0]["value"] == "pppoe-karu"
    assert source["radusergroup"][0]["groupname"] == "fiber-1000gb"


def test_send_subscription_credentials_uses_email_and_sms_targets(
    db_session,
    subscriber,
    catalog_offer,
    monkeypatch,
):
    subscriber.email = "primary@example.com"
    subscriber.phone = "+2348000000001"
    db_session.add(
        SubscriberChannel(
            subscriber_id=subscriber.id,
            channel_type=ChannelType.email,
            address="backup@example.com",
        )
    )
    db_session.add(
        SubscriberChannel(
            subscriber_id=subscriber.id,
            channel_type=ChannelType.sms,
            address="+2348000000002",
        )
    )
    db_session.commit()

    subscription = catalog_service.subscriptions.create(
        db_session,
        SubscriptionCreate(
            account_id=subscriber.id,
            offer_id=catalog_offer.id,
        ),
    )
    web_catalog_subscriptions_service._upsert_access_credential(
        db_session,
        subscriber_id=subscriber.id,
        username="10004167",
        plain_password="SendablePass123",
        radius_profile_id=None,
    )

    sent_emails = []
    sent_sms = []

    def _fake_send_email(**kwargs):
        sent_emails.append(kwargs["to_email"])
        return True

    def _fake_send_sms(_db, phone, body, track=True):
        sent_sms.append((phone, body, track))
        return True

    monkeypatch.setattr(
        web_catalog_subscriptions_service.email_service,
        "send_email",
        _fake_send_email,
    )
    monkeypatch.setattr(
        web_catalog_subscriptions_service.sms_service,
        "send_sms",
        _fake_send_sms,
    )

    result = web_catalog_subscriptions_service.send_subscription_credentials(
        db_session,
        subscription_id=str(subscription.id),
    )

    assert result["email_sent"] == 2
    assert result["sms_sent"] == 2
    assert sent_emails == ["primary@example.com", "backup@example.com"]
    assert [row[0] for row in sent_sms] == ["+2348000000001", "+2348000000002"]


def test_force_subscription_reauth_reconciles_disconnects_and_audits(
    db_session,
    subscriber,
    catalog_offer,
    monkeypatch,
):
    nas = NasDevice(
        name="NAS Force Reauth",
        nas_ip="10.20.30.1",
        management_ip="10.20.30.1",
        ip_address="10.20.30.1",
        is_active=True,
    )
    db_session.add(nas)
    db_session.commit()
    subscription = catalog_service.subscriptions.create(
        db_session,
        SubscriptionCreate(
            account_id=subscriber.id,
            offer_id=catalog_offer.id,
            status=SubscriptionStatus.active,
            provisioning_nas_device_id=nas.id,
            login="pppoe-force-1",
        ),
    )
    captured: dict[str, object] = {}

    def fake_reconcile(db, subscription_id):
        from app.services.radius import (
            ConnectivityProjectionDisposition,
            SubscriptionConnectivityOutcome,
        )

        captured["reconcile_subscription_id"] = subscription_id
        return SubscriptionConnectivityOutcome(
            subscription_id=subscription_id,
            disposition=ConnectivityProjectionDisposition.projected,
            external_credentials_synced=1,
            requested_logins=1,
            projected_logins=1,
            projection_targets=1,
        )

    def fake_disconnect(db, subscription_id, reason=None):
        captured["disconnect_subscription_id"] = subscription_id
        captured["disconnect_reason"] = reason
        return 2

    def fake_audit(db, **kwargs):
        captured["audit"] = kwargs

    monkeypatch.setattr(
        "app.services.radius.reconcile_subscription_connectivity", fake_reconcile
    )
    monkeypatch.setattr(
        "app.services.enforcement.disconnect_subscription_sessions", fake_disconnect
    )
    monkeypatch.setattr(
        web_catalog_subscriptions_service, "record_audit_event", fake_audit
    )

    result = web_catalog_subscriptions_service.force_subscription_reauth(
        db_session,
        str(subscription.id),
        request=None,
        actor_id="admin-1",
    )

    assert result["sessions_disconnected"] == 2
    assert captured["reconcile_subscription_id"] == str(subscription.id)
    assert captured["disconnect_subscription_id"] == str(subscription.id)
    assert captured["disconnect_reason"] == "manual_force_reauth"
    audit = captured["audit"]
    assert audit["action"] == "force_reauth"
    assert audit["entity_type"] == "subscription"
    assert audit["entity_id"] == str(subscription.id)
    assert audit["actor_id"] == "admin-1"
    metadata = audit["metadata"]
    assert metadata["subscriber_id"] == str(subscriber.id)
    assert metadata["nas_device_id"] == str(nas.id)
    assert metadata["nas_name"] == "NAS Force Reauth"
    assert metadata["nas_ip"] == "10.20.30.1"
    assert metadata["login"] == "pppoe-force-1"
    assert metadata["sessions_disconnected"] == 2
    assert metadata["radius_reconciled"] is True


def test_create_subscription_with_audit_uses_requested_free_ipv4(
    db_session,
    subscriber,
    catalog_offer,
):
    pool, pool_error = web_network_ip_service.create_ip_pool(
        db_session,
        {
            "name": "Subscription IPv4 Pool",
            "ip_version": "ipv4",
            "cidr": "10.80.0.0/29",
            "is_active": True,
        },
    )
    assert pool_error is None
    assert pool is not None

    block, block_error = web_network_ip_service.create_ip_block(
        db_session,
        {
            "pool_id": str(pool.id),
            "cidr": "10.80.0.0/29",
            "is_active": True,
        },
    )
    assert block_error is None
    assert block is not None

    payload = {
        "account_id": subscriber.id,
        "offer_id": catalog_offer.id,
    }
    form = FormData(
        [
            ("ipv4_method", "permanent_static"),
            ("ipv4_block_ids", str(block.id)),
            ("ipv4_addresses", "10.80.0.5"),
        ]
    )

    created = web_catalog_subscriptions_service.create_subscription_with_audit(
        db_session,
        payload,
        form,
        None,
        None,
    )

    db_session.refresh(created)
    assert created.ipv4_address == "10.80.0.5"

    assignment = (
        db_session.query(IPAssignment)
        .filter(IPAssignment.subscriber_id == created.subscriber_id)
        .one()
    )
    assert assignment.ipv4_address is not None
    assert assignment.ipv4_address.address == "10.80.0.5"


def test_replace_subscription_ipv4_uses_ipam_owner_without_billing(
    db_session,
    subscriber,
    catalog_offer,
):
    pool, pool_error = web_network_ip_service.create_ip_pool(
        db_session,
        {
            "name": "Edit Subscription IPv4 Pool",
            "ip_version": "ipv4",
            "cidr": "10.82.0.0/29",
            "is_active": True,
        },
    )
    assert pool_error is None
    assert pool is not None

    block, block_error = web_network_ip_service.create_ip_block(
        db_session,
        {
            "pool_id": str(pool.id),
            "cidr": "10.82.0.0/29",
            "is_active": True,
        },
    )
    assert block_error is None
    assert block is not None

    subscription = catalog_service.subscriptions.create(
        db_session,
        SubscriptionCreate(
            account_id=subscriber.id,
            offer_id=catalog_offer.id,
        ),
    )
    subscription.status = SubscriptionStatus.active
    subscription.login = "10004167"
    subscription.ipv4_address = "10.82.0.4"
    subscription.billing_mode = BillingMode.postpaid
    catalog_offer.billing_mode = BillingMode.prepaid
    current_address = IPv4Address(
        address="10.82.0.4",
        pool_id=pool.id,
        allocation_type="static",
    )
    target_address = IPv4Address(
        address="10.82.0.5",
        pool_id=pool.id,
        allocation_type="static",
    )
    db_session.add_all([current_address, target_address])
    db_session.flush()
    current_assignment = IPAssignment(
        subscriber_id=subscriber.id,
        subscription_id=subscription.id,
        ip_version=IPVersion.ipv4,
        ipv4_address_id=current_address.id,
        is_active=True,
    )
    ip_add_on = _public_ip_addon(db_session, name="Paid /28 IP", ip_prefix_length=28)
    entitlement = SubscriptionAddOn(
        subscription_id=subscription.id,
        add_on_id=ip_add_on.id,
        quantity=1,
        start_at=datetime(2026, 7, 5, tzinfo=UTC),
    )
    db_session.add_all([current_assignment, entitlement])
    db_session.commit()
    entitlement_id = entitlement.id
    adjustment_count = db_session.query(AccountAdjustment).count()
    invoice_line_count = db_session.query(InvoiceLine).count()

    with (
        patch(
            "app.services.ip_consistency_audit._external_ip_state",
            return_value=({"10004167": "10.82.0.4"}, {"10004167"}, 0),
        ),
        patch(
            "app.services.radius_projection_planner.plan_login_radius_projections",
            return_value={
                "10004167": SimpleNamespace(
                    subscription_id=str(subscription.id),
                    plan=SimpleNamespace(mode="active", write_radreply=True),
                )
            },
        ),
        patch("app.services.ip_assignment_lifecycle.emit_event"),
        patch.object(
            web_catalog_subscriptions_service,
            "update_subscription",
            side_effect=AssertionError("subscription update entered"),
        ),
        patch.object(
            web_catalog_subscriptions_service,
            "sync_public_ip_addon_for_subscription",
            side_effect=AssertionError("billing add-on sync entered"),
        ),
    ):
        replaced_ip = (
            web_catalog_subscriptions_service.replace_subscription_ipv4_with_owner(
                db_session,
                subscription_id=str(subscription.id),
                selector=str(block.id),
                requested_ip="10.82.0.5",
                actor_id="admin-test",
            )
        )

    assert replaced_ip == "10.82.0.5"
    db_session.refresh(subscription)
    db_session.refresh(current_assignment)
    assert subscription.ipv4_address == "10.82.0.5"
    assert current_assignment.is_active is False

    assignment = (
        db_session.query(IPAssignment)
        .filter(IPAssignment.subscriber_id == subscriber.id)
        .filter(IPAssignment.is_active.is_(True))
        .one()
    )
    assert assignment.ipv4_address is not None
    assert assignment.ipv4_address.address == "10.82.0.5"
    db_session.refresh(entitlement)
    assert entitlement.id == entitlement_id
    assert entitlement.quantity == 1
    assert entitlement.end_at is None
    assert db_session.query(AccountAdjustment).count() == adjustment_count
    assert db_session.query(InvoiceLine).count() == invoice_line_count


def test_update_subscription_preserves_ipv4_when_assignment_omitted(
    db_session,
    subscriber,
    catalog_offer,
):
    subscription = catalog_service.subscriptions.create(
        db_session,
        SubscriptionCreate(
            account_id=subscriber.id,
            offer_id=catalog_offer.id,
        ),
    )
    now = datetime.now(UTC)
    subscription.status = SubscriptionStatus.active
    subscription.start_at = now
    subscription.next_billing_at = now + timedelta(days=30)
    subscription.ipv4_address = "10.82.10.5"
    address = IPv4Address(address="10.82.10.5")
    db_session.add(address)
    db_session.flush()
    assignment = IPAssignment(
        subscriber_id=subscriber.id,
        subscription_id=subscription.id,
        ip_version=IPVersion.ipv4,
        ipv4_address_id=address.id,
        is_active=True,
    )
    db_session.add(assignment)
    db_session.commit()

    updated = web_catalog_subscriptions_service.update_subscription_with_audit(
        db_session,
        str(subscription.id),
        {"login": "10004167"},
        "",
        [],
        [],
        None,
        None,
    )

    db_session.refresh(updated)
    db_session.refresh(assignment)
    assert updated.ipv4_address == "10.82.10.5"
    assert assignment.is_active is True


def test_generic_update_rejects_ipv4_assignment_before_technical_write(
    db_session,
    subscriber,
    catalog_offer,
):
    pool, pool_error = web_network_ip_service.create_ip_pool(
        db_session,
        {
            "name": "Placeholder Reject Pool",
            "ip_version": "ipv4",
            "cidr": "10.82.11.0/29",
            "is_active": True,
        },
    )
    assert pool_error is None
    block, block_error = web_network_ip_service.create_ip_block(
        db_session,
        {
            "pool_id": str(pool.id),
            "cidr": "10.82.11.0/29",
            "is_active": True,
        },
    )
    assert block_error is None
    subscription = catalog_service.subscriptions.create(
        db_session,
        SubscriptionCreate(
            account_id=subscriber.id,
            offer_id=catalog_offer.id,
        ),
    )

    original_login = subscription.login
    with pytest.raises(ValueError, match="Use Replace service IPv4"):
        web_catalog_subscriptions_service.update_subscription_with_audit(
            db_session,
            str(subscription.id),
            {"login": "10004167"},
            "",
            [str(block.id)],
            ["0.0.0.0"],
            None,
            None,
        )
    db_session.refresh(subscription)
    assert subscription.login == original_login


def test_generic_update_form_rejects_router_change_before_technical_write(
    db_session,
    subscription,
):
    source_nas = NasDevice(name=f"Source NAS {uuid4().hex[:8]}", is_active=True)
    target_nas = NasDevice(name=f"Target NAS {uuid4().hex[:8]}", is_active=True)
    db_session.add_all([source_nas, target_nas])
    db_session.flush()
    subscription.provisioning_nas_device_id = source_nas.id
    db_session.commit()

    result = web_catalog_subscription_workflows_service.handle_subscription_update_form(
        db_session,
        subscription_id=str(subscription.id),
        form=FormData(
            {
                "subscriber_id": str(subscription.subscriber_id),
                "offer_id": str(subscription.offer_id),
                "provisioning_nas_device_id": str(target_nas.id),
                "status": subscription.status.value,
                "billing_mode": subscription.billing_mode.value,
                "contract_term": subscription.contract_term.value,
            }
        ),
        request=None,
        actor_id="network-operator",
    )

    assert "redirect_url" not in result
    context = result["form_context"]
    assert "Use Move service access" in context["error"]
    db_session.refresh(subscription)
    assert subscription.provisioning_nas_device_id == source_nas.id


def test_edit_form_data_includes_only_current_primary_ipv4_assignment(
    db_session,
    subscriber,
    catalog_offer,
):
    pool, pool_error = web_network_ip_service.create_ip_pool(
        db_session,
        {
            "name": "Existing Assignments Pool",
            "ip_version": "ipv4",
            "cidr": "10.83.0.0/29",
            "is_active": True,
        },
    )
    assert pool_error is None
    assert pool is not None

    first_block, first_error = web_network_ip_service.create_ip_block(
        db_session,
        {
            "pool_id": str(pool.id),
            "cidr": "10.83.0.0/30",
            "is_active": True,
        },
    )
    second_block, second_error = web_network_ip_service.create_ip_block(
        db_session,
        {
            "pool_id": str(pool.id),
            "cidr": "10.83.0.4/30",
            "is_active": True,
        },
    )
    assert first_error is None
    assert second_error is None
    assert first_block is not None
    assert second_block is not None

    subscription = catalog_service.subscriptions.create(
        db_session,
        SubscriptionCreate(
            account_id=subscriber.id,
            offer_id=catalog_offer.id,
        ),
    )

    web_catalog_subscriptions_service._allocate_ipv4_assignments_for_subscription(
        db_session,
        subscription_obj=subscription,
        block_ids=[str(first_block.id), str(second_block.id)],
        selected_ips=["10.83.0.1", "10.83.0.5"],
    )
    subscription.ipv4_address = "10.83.0.1"
    db_session.commit()
    db_session.refresh(subscription)

    form_data = web_catalog_subscriptions_service.edit_form_data(
        db_session, subscription
    )

    assert form_data["ipv4_addresses"] == ["10.83.0.1"]
    assert form_data["ipv4_block_ids"] == [str(first_block.id)]


def test_create_subscription_with_audit_persists_additional_routes(
    db_session,
    subscriber,
    catalog_offer,
):
    _public_ip_addon(db_session, name="/29 IP", ip_prefix_length=29)
    payload = {
        "account_id": subscriber.id,
        "offer_id": catalog_offer.id,
    }
    form = FormData(
        [
            ("additional_route_cidrs", "203.0.113.9/29"),
            ("additional_route_metrics", "2"),
            ("additional_route_cidrs", ""),
            ("additional_route_metrics", ""),
        ]
    )

    created = web_catalog_subscriptions_service.create_subscription_with_audit(
        db_session,
        payload,
        form,
        None,
        None,
    )

    route = (
        db_session.query(SubscriberAdditionalRoute)
        .filter(SubscriberAdditionalRoute.subscriber_id == created.subscriber_id)
        .one()
    )
    assert route.cidr == "203.0.113.8/29"
    assert route.prefix_length == 29
    assert route.metric == 2
    assert route.is_active is True
    assert route.source == "admin_subscription_form"
    assert (
        db_session.query(SubscriptionAddOn)
        .filter(SubscriptionAddOn.subscription_id == created.id)
        .count()
        == 0
    )


def test_create_subscription_with_audit_persists_public_ip_addon(
    db_session,
    subscriber,
    catalog_offer,
):
    add_on = _public_ip_addon(db_session)
    payload = {
        "account_id": subscriber.id,
        "offer_id": catalog_offer.id,
    }
    form = FormData(
        [
            ("ip_addon_id", str(add_on.id)),
            ("ip_addon_quantity", "2"),
        ]
    )

    created = web_catalog_subscriptions_service.create_subscription_with_audit(
        db_session,
        payload,
        form,
        None,
        None,
    )

    sub_addon = (
        db_session.query(SubscriptionAddOn)
        .filter(SubscriptionAddOn.subscription_id == created.id)
        .one()
    )
    assert sub_addon.add_on_id == add_on.id
    assert sub_addon.quantity == 2
    assert sub_addon.end_at is None


def test_subscription_form_context_includes_legacy_extra_ip_addons(
    db_session,
    subscriber,
):
    add_on = _public_ip_addon(
        db_session,
        ip_is_public=False,
        addon_type=AddOnType.extra_ip,
    )
    form = web_catalog_subscriptions_service.default_subscription_form(
        str(subscriber.id), str(subscriber.id)
    )

    context = web_catalog_subscriptions_service.subscription_form_context(
        db_session, form
    )

    assert add_on.id in {option.id for option in context["ip_addon_options"]}


def test_subscription_form_context_bootstraps_ip_addons_from_ipv4_blocks(
    db_session,
    subscriber,
):
    pool, pool_error = web_network_ip_service.create_ip_pool(
        db_session,
        {
            "name": "Public Routed Pool",
            "ip_version": "ipv4",
            "cidr": "203.0.113.0/24",
            "is_active": True,
        },
    )
    assert pool_error is None
    block, block_error = web_network_ip_service.create_ip_block(
        db_session,
        {
            "pool_id": str(pool.id),
            "cidr": "203.0.113.0/24",
            "is_active": True,
        },
    )
    assert block_error is None
    assert block is not None
    form = web_catalog_subscriptions_service.default_subscription_form(
        str(subscriber.id), str(subscriber.id)
    )

    context = web_catalog_subscriptions_service.subscription_form_context(
        db_session, form
    )

    options = context["ip_addon_options"]
    names = {option.name for option in options}
    assert {"/24 IP", "/29 IP", "/30 IP", "/32 IP"}.issubset(names)
    by_name = {option.name: option for option in options}
    assert by_name["/29 IP"].ip_is_public is True
    assert by_name["/29 IP"].addon_type == AddOnType.extra_ip
    assert by_name["/32 IP"].addon_type == AddOnType.static_ip

    route_ranges = context["route_range_options"]
    route_by_cidr = {option["cidr"]: option for option in route_ranges}
    assert "203.0.113.0/24" in route_by_cidr
    assert "children_by_prefix" not in route_by_cidr["203.0.113.0/24"]
    children_29 = web_catalog_subscriptions_service.route_child_options_for_parent(
        db_session,
        parent_cidr="203.0.113.0/24",
        prefix=29,
        current_subscriber_id=subscriber.id,
    )
    children_30 = web_catalog_subscriptions_service.route_child_options_for_parent(
        db_session,
        parent_cidr="203.0.113.0/24",
        prefix=30,
        current_subscriber_id=subscriber.id,
    )
    assert "203.0.113.8/29" in {child["cidr"] for child in children_29}
    assert "203.0.113.8/30" in {child["cidr"] for child in children_30}


def test_route_parent_for_sub24_block_is_not_widened_to_24(db_session, subscriber):
    """A sub-/24 block must be offered as itself, and its children must stay
    inside it — never the enclosing /24 (which would route third-party space)."""
    import ipaddress

    pool, pool_error = web_network_ip_service.create_ip_pool(
        db_session,
        {
            "name": "Small Routed Block",
            "ip_version": "ipv4",
            "cidr": "203.0.113.16/28",
            "is_active": True,
        },
    )
    assert pool_error is None

    parents = web_catalog_subscriptions_service._route_range_parent_options_for_ipam(
        db_session
    )
    parent_cidrs = {p["cidr"] for p in parents}
    assert "203.0.113.16/28" in parent_cidrs
    assert "203.0.113.0/24" not in parent_cidrs

    block_net = ipaddress.ip_network("203.0.113.16/28")
    for prefix in (29, 30, 32):
        children = web_catalog_subscriptions_service.route_child_options_for_parent(
            db_session,
            parent_cidr="203.0.113.16/28",
            prefix=prefix,
            current_subscriber_id=subscriber.id,
        )
        for child in children:
            child_net = ipaddress.ip_network(child["cidr"])
            assert child_net.subnet_of(block_net), (
                f"/{prefix} child {child['cidr']} escapes owned block {block_net}"
            )


def test_normalize_additional_routes_rejects_non_routable_ranges():
    for bad in ["0.0.0.0/0", "224.0.0.0/24", "240.0.0.0/24", "127.0.0.0/24"]:
        with pytest.raises(ValueError):
            web_catalog_subscriptions_service.normalize_additional_routes([bad])


def test_normalize_additional_routes_rejects_overlapping_blocks():
    with pytest.raises(ValueError, match="overlap"):
        web_catalog_subscriptions_service.normalize_additional_routes(
            ["203.0.113.0/28", "203.0.113.4/30"]
        )


def test_normalize_additional_routes_snaps_host_bits_to_network():
    routes = web_catalog_subscriptions_service.normalize_additional_routes(
        ["203.0.113.9/29"]
    )
    assert routes[0][0] == "203.0.113.8/29"


def test_subscription_form_context_prioritizes_160_and_102_ipam_parents(
    db_session,
    subscriber,
):
    for name, cidr in [
        ("Other Public", "203.0.113.0/24"),
        ("Range 160", "160.119.127.0/24"),
        ("Range 102", "102.220.188.0/24"),
    ]:
        pool, pool_error = web_network_ip_service.create_ip_pool(
            db_session,
            {
                "name": name,
                "ip_version": "ipv4",
                "cidr": cidr,
                "is_active": True,
            },
        )
        assert pool_error is None
        assert pool is not None

    form = web_catalog_subscriptions_service.default_subscription_form(
        str(subscriber.id), str(subscriber.id)
    )

    context = web_catalog_subscriptions_service.subscription_form_context(
        db_session, form
    )

    route_cidrs = [option["cidr"] for option in context["route_range_options"]]
    assert route_cidrs[:2] == ["102.220.188.0/24", "160.119.127.0/24"]
    assert route_cidrs.index("203.0.113.0/24") > 1


def test_subscription_form_context_hides_child_routes_assigned_to_other_subscriber(
    db_session,
    subscriber,
):
    other = Subscriber(
        first_name="Other",
        last_name="Subscriber",
        email="other-route@example.com",
    )
    db_session.add(other)
    db_session.flush()
    pool, pool_error = web_network_ip_service.create_ip_pool(
        db_session,
        {
            "name": "Range 160",
            "ip_version": "ipv4",
            "cidr": "160.119.127.0/24",
            "is_active": True,
        },
    )
    assert pool_error is None
    assigned_ip = IPv4Address(
        address="160.119.127.9",
        pool_id=pool.id,
        is_reserved=False,
    )
    db_session.add(assigned_ip)
    db_session.flush()
    db_session.add(
        IPAssignment(
            subscriber_id=other.id,
            ip_version=IPVersion.ipv4,
            ipv4_address_id=assigned_ip.id,
            is_active=True,
        )
    )
    db_session.add(
        SubscriberAdditionalRoute(
            subscriber_id=other.id,
            cidr="160.119.127.4/30",
            prefix_length=30,
            metric=1,
            is_active=True,
            source="test",
        )
    )
    db_session.commit()
    form = web_catalog_subscriptions_service.default_subscription_form(
        str(subscriber.id), str(subscriber.id)
    )

    context = web_catalog_subscriptions_service.subscription_form_context(
        db_session, form
    )

    assert any(
        option["cidr"] == "160.119.127.0/24"
        for option in context["route_range_options"]
    )
    children = web_catalog_subscriptions_service.route_child_options_for_parent(
        db_session,
        parent_cidr="160.119.127.0/24",
        prefix=30,
        current_subscriber_id=subscriber.id,
    )
    child_cidrs = {child["cidr"] for child in children}
    assert "160.119.127.0/30" not in child_cidrs
    assert "160.119.127.4/30" not in child_cidrs
    assert "160.119.127.8/30" not in child_cidrs
    assert "160.119.127.12/30" in child_cidrs


def test_update_subscription_with_audit_syncs_additional_routes(
    db_session,
    subscriber,
    catalog_offer,
):
    _public_ip_addon(db_session, name="/30 IP", ip_prefix_length=30)
    subscription = catalog_service.subscriptions.create(
        db_session,
        SubscriptionCreate(
            account_id=subscriber.id,
            offer_id=catalog_offer.id,
        ),
    )
    existing = SubscriberAdditionalRoute(
        subscriber_id=subscriber.id,
        cidr="203.0.113.8/29",
        prefix_length=29,
        metric=1,
        is_active=True,
        source="test",
    )
    db_session.add(existing)
    db_session.commit()

    web_catalog_subscriptions_service.update_subscription_with_audit(
        db_session,
        str(subscription.id),
        {"login": "10004167"},
        "",
        [],
        [],
        None,
        None,
        additional_route_cidrs=["198.51.100.4/30"],
        additional_route_metrics=["3"],
    )

    db_session.refresh(existing)
    active = (
        db_session.query(SubscriberAdditionalRoute)
        .filter(SubscriberAdditionalRoute.subscriber_id == subscriber.id)
        .filter(SubscriberAdditionalRoute.is_active.is_(True))
        .one()
    )
    assert existing.is_active is False
    assert active.cidr == "198.51.100.4/30"
    assert active.prefix_length == 30
    assert active.metric == 3


def test_update_subscription_with_audit_syncs_public_ip_addon(
    db_session,
    subscriber,
    catalog_offer,
):
    add_on = _public_ip_addon(db_session)
    subscription = catalog_service.subscriptions.create(
        db_session,
        SubscriptionCreate(
            account_id=subscriber.id,
            offer_id=catalog_offer.id,
        ),
    )

    web_catalog_subscriptions_service.update_subscription_with_audit(
        db_session,
        str(subscription.id),
        {"login": "10004167"},
        "",
        [],
        [],
        None,
        None,
        ip_addon_id=str(add_on.id),
        ip_addon_quantity="3",
    )

    sub_addon = (
        db_session.query(SubscriptionAddOn)
        .filter(SubscriptionAddOn.subscription_id == subscription.id)
        .one()
    )
    assert sub_addon.add_on_id == add_on.id
    assert sub_addon.quantity == 3
    assert sub_addon.end_at is None


def test_update_subscription_rejects_additional_route_assigned_to_other_subscriber(
    db_session,
    subscriber,
    catalog_offer,
):
    _public_ip_addon(db_session, name="/30 IP", ip_prefix_length=30)
    other = Subscriber(
        first_name="Other",
        last_name="Subscriber",
        email="other-overlap@example.com",
    )
    db_session.add(other)
    db_session.flush()
    db_session.add(
        SubscriberAdditionalRoute(
            subscriber_id=other.id,
            cidr="160.119.127.4/30",
            prefix_length=30,
            metric=1,
            is_active=True,
            source="test",
        )
    )
    db_session.commit()
    subscription = catalog_service.subscriptions.create(
        db_session,
        SubscriptionCreate(
            account_id=subscriber.id,
            offer_id=catalog_offer.id,
        ),
    )

    with pytest.raises(ValueError, match="already assigned"):
        web_catalog_subscriptions_service.update_subscription_with_audit(
            db_session,
            str(subscription.id),
            {"login": "10004167"},
            "",
            [],
            [],
            None,
            None,
            additional_route_cidrs=["160.119.127.4/30"],
            additional_route_metrics=["1"],
        )


def test_update_subscription_rejects_additional_route_with_assigned_ipam_address(
    db_session,
    subscriber,
    catalog_offer,
):
    _public_ip_addon(db_session, name="/30 IP", ip_prefix_length=30)
    other = Subscriber(
        first_name="Other",
        last_name="Subscriber",
        email="other-ipam-route@example.com",
    )
    db_session.add(other)
    db_session.flush()
    assigned_ip = IPv4Address(
        address="160.119.127.9",
        is_reserved=False,
    )
    db_session.add(assigned_ip)
    db_session.flush()
    db_session.add(
        IPAssignment(
            subscriber_id=other.id,
            ip_version=IPVersion.ipv4,
            ipv4_address_id=assigned_ip.id,
            is_active=True,
        )
    )
    db_session.commit()
    subscription = catalog_service.subscriptions.create(
        db_session,
        SubscriptionCreate(
            account_id=subscriber.id,
            offer_id=catalog_offer.id,
        ),
    )

    with pytest.raises(ValueError, match="reserved or assigned in IPAM"):
        web_catalog_subscriptions_service.update_subscription_with_audit(
            db_session,
            str(subscription.id),
            {"login": "10004167"},
            "",
            [],
            [],
            None,
            None,
            additional_route_cidrs=["160.119.127.8/30"],
            additional_route_metrics=["1"],
        )


def test_update_subscription_allows_existing_route_reserved_guard_rows(
    db_session,
    subscriber,
    catalog_offer,
):
    subscription = catalog_service.subscriptions.create(
        db_session,
        SubscriptionCreate(
            account_id=subscriber.id,
            offer_id=catalog_offer.id,
        ),
    )
    db_session.add(
        SubscriberAdditionalRoute(
            subscriber_id=subscriber.id,
            cidr="160.119.127.12/30",
            prefix_length=30,
            metric=1,
            is_active=True,
            source="test",
        )
    )
    for address, allocation_type in [
        ("160.119.127.12", None),
        ("160.119.127.13", "wan"),
        ("160.119.127.14", "wan"),
        ("160.119.127.15", None),
    ]:
        db_session.add(
            IPv4Address(
                address=address,
                is_reserved=True,
                allocation_type=allocation_type,
            )
        )
    db_session.commit()

    web_catalog_subscriptions_service.update_subscription_with_audit(
        db_session,
        str(subscription.id),
        {"login": "10004167"},
        "",
        [],
        [],
        None,
        None,
        additional_route_cidrs=["160.119.127.12/30"],
        additional_route_metrics=["1"],
    )

    route = (
        db_session.query(SubscriberAdditionalRoute)
        .filter(SubscriberAdditionalRoute.subscriber_id == subscriber.id)
        .filter(SubscriberAdditionalRoute.cidr == "160.119.127.12/30")
        .one()
    )
    assert route.is_active is True


def test_update_subscription_rejects_additional_route_with_unrelated_reserved_ipam_row(
    db_session,
    subscriber,
    catalog_offer,
):
    db_session.add(
        IPv4Address(
            address="160.119.127.18",
            is_reserved=True,
        )
    )
    db_session.commit()
    subscription = catalog_service.subscriptions.create(
        db_session,
        SubscriptionCreate(
            account_id=subscriber.id,
            offer_id=catalog_offer.id,
        ),
    )

    with pytest.raises(ValueError, match="reserved or assigned in IPAM"):
        web_catalog_subscriptions_service.update_subscription_with_audit(
            db_session,
            str(subscription.id),
            {"login": "10004167"},
            "",
            [],
            [],
            None,
            None,
            additional_route_cidrs=["160.119.127.16/30"],
            additional_route_metrics=["1"],
        )


def test_update_subscription_rejects_existing_route_with_management_ipam_row(
    db_session,
    subscriber,
    catalog_offer,
):
    subscription = catalog_service.subscriptions.create(
        db_session,
        SubscriptionCreate(
            account_id=subscriber.id,
            offer_id=catalog_offer.id,
        ),
    )
    db_session.add(
        SubscriberAdditionalRoute(
            subscriber_id=subscriber.id,
            cidr="160.119.127.20/30",
            prefix_length=30,
            metric=1,
            is_active=True,
            source="test",
        )
    )
    db_session.add(
        IPv4Address(
            address="160.119.127.21",
            is_reserved=True,
            allocation_type="management",
        )
    )
    db_session.commit()

    with pytest.raises(ValueError, match="reserved or assigned in IPAM"):
        web_catalog_subscriptions_service.update_subscription_with_audit(
            db_session,
            str(subscription.id),
            {"login": "10004167"},
            "",
            [],
            [],
            None,
            None,
            additional_route_cidrs=["160.119.127.20/30"],
            additional_route_metrics=["1"],
        )


def test_update_subscription_rejects_additional_route_starting_at_dot_zero(
    db_session,
    subscriber,
    catalog_offer,
):
    _public_ip_addon(db_session, name="/30 IP", ip_prefix_length=30)
    subscription = catalog_service.subscriptions.create(
        db_session,
        SubscriptionCreate(
            account_id=subscriber.id,
            offer_id=catalog_offer.id,
        ),
    )

    with pytest.raises(ValueError, match="cannot start at .0"):
        web_catalog_subscriptions_service.update_subscription_with_audit(
            db_session,
            str(subscription.id),
            {"login": "10004167"},
            "",
            [],
            [],
            None,
            None,
            additional_route_cidrs=["160.119.127.0/30"],
            additional_route_metrics=["1"],
        )


def test_update_subscription_requires_routes_to_match_public_ip_addon_prefix(
    db_session,
    subscriber,
    catalog_offer,
):
    add_on = _public_ip_addon(
        db_session,
        name="/30 IP",
        ip_prefix_length=30,
    )
    subscription = catalog_service.subscriptions.create(
        db_session,
        SubscriptionCreate(
            account_id=subscriber.id,
            offer_id=catalog_offer.id,
        ),
    )

    with pytest.raises(ValueError, match="/30 add-on"):
        web_catalog_subscriptions_service.update_subscription_with_audit(
            db_session,
            str(subscription.id),
            {"login": "10004167"},
            "",
            [],
            [],
            None,
            None,
            additional_route_cidrs=["160.119.127.0/29"],
            additional_route_metrics=["1"],
            ip_addon_id=str(add_on.id),
            ip_addon_quantity="1",
        )


def test_update_subscription_refreshes_radius_session_for_additional_ip_change(
    db_session,
    subscriber,
    catalog_offer,
):
    add_on = _public_ip_addon(
        db_session,
        name="/30 IP",
        ip_prefix_length=30,
    )
    subscription = catalog_service.subscriptions.create(
        db_session,
        SubscriptionCreate(
            account_id=subscriber.id,
            offer_id=catalog_offer.id,
        ),
    )

    with patch.object(
        web_catalog_subscriptions_service,
        "_refresh_subscription_radius_session",
    ) as refresh:
        web_catalog_subscriptions_service.update_subscription_with_audit(
            db_session,
            str(subscription.id),
            {"login": "10004167"},
            "",
            [],
            [],
            None,
            None,
            additional_route_cidrs=["160.119.127.4/30"],
            additional_route_metrics=["1"],
            ip_addon_id=str(add_on.id),
            ip_addon_quantity="1",
        )

    refresh.assert_called_once_with(
        db_session,
        str(subscription.id),
        reason="additional_ip_update",
    )


def test_update_subscription_with_audit_preserves_routes_when_not_supplied(
    db_session,
    subscriber,
    catalog_offer,
):
    subscription = catalog_service.subscriptions.create(
        db_session,
        SubscriptionCreate(
            account_id=subscriber.id,
            offer_id=catalog_offer.id,
        ),
    )
    route = SubscriberAdditionalRoute(
        subscriber_id=subscriber.id,
        cidr="203.0.113.8/29",
        prefix_length=29,
        metric=1,
        is_active=True,
        source="test",
    )
    db_session.add(route)
    db_session.commit()

    web_catalog_subscriptions_service.update_subscription_with_audit(
        db_session,
        str(subscription.id),
        {"login": "10004167"},
        "",
        [],
        [],
        None,
        None,
    )

    db_session.refresh(route)
    assert route.is_active is True


def test_update_subscription_with_audit_preserves_public_ip_addon_when_not_supplied(
    db_session,
    subscriber,
    catalog_offer,
):
    add_on = _public_ip_addon(db_session)
    subscription = catalog_service.subscriptions.create(
        db_session,
        SubscriptionCreate(
            account_id=subscriber.id,
            offer_id=catalog_offer.id,
        ),
    )
    sub_addon = SubscriptionAddOn(
        subscription_id=subscription.id,
        add_on_id=add_on.id,
        quantity=1,
    )
    db_session.add(sub_addon)
    db_session.commit()

    web_catalog_subscriptions_service.update_subscription_with_audit(
        db_session,
        str(subscription.id),
        {"login": "10004167"},
        "",
        [],
        [],
        None,
        None,
    )

    db_session.refresh(sub_addon)
    assert sub_addon.end_at is None


def test_edit_form_data_includes_active_additional_routes(
    db_session,
    subscriber,
    catalog_offer,
):
    subscription = catalog_service.subscriptions.create(
        db_session,
        SubscriptionCreate(
            account_id=subscriber.id,
            offer_id=catalog_offer.id,
        ),
    )
    db_session.add(
        SubscriberAdditionalRoute(
            subscriber_id=subscriber.id,
            cidr="203.0.113.8/29",
            prefix_length=29,
            metric=2,
            is_active=True,
            source="test",
        )
    )
    db_session.add(
        SubscriberAdditionalRoute(
            subscriber_id=subscriber.id,
            cidr="198.51.100.0/30",
            prefix_length=30,
            metric=1,
            is_active=False,
            source="test",
        )
    )
    db_session.commit()

    form_data = web_catalog_subscriptions_service.edit_form_data(
        db_session, subscription
    )

    assert form_data["additional_route_cidrs"] == ["203.0.113.8/29"]
    assert form_data["additional_route_metrics"] == ["2"]


def test_additional_route_update_creates_matching_public_ip_addon(
    db_session,
    subscriber,
    catalog_offer,
):
    add_on = _public_ip_addon(
        db_session,
        name="/30 IP",
        ip_prefix_length=30,
    )
    subscription = catalog_service.subscriptions.create(
        db_session,
        SubscriptionCreate(
            account_id=subscriber.id,
            offer_id=catalog_offer.id,
        ),
    )

    web_catalog_subscriptions_service.update_subscription_with_audit(
        db_session,
        str(subscription.id),
        {"login": "10004167"},
        "",
        [],
        [],
        None,
        None,
        additional_route_cidrs=["160.119.125.24/30"],
        additional_route_metrics=["1"],
        ip_addon_id=str(add_on.id),
        ip_addon_quantity="1",
    )

    sub_addon = (
        db_session.query(SubscriptionAddOn)
        .filter(SubscriptionAddOn.subscription_id == subscription.id)
        .filter(SubscriptionAddOn.add_on_id == add_on.id)
        .filter(SubscriptionAddOn.end_at.is_(None))
        .one()
    )
    assert sub_addon.quantity == 1


def test_additional_route_update_without_addon_does_not_create_billing_addon(
    db_session,
    subscriber,
    catalog_offer,
):
    _public_ip_addon(
        db_session,
        name="/30 IP",
        ip_prefix_length=30,
    )
    subscription = catalog_service.subscriptions.create(
        db_session,
        SubscriptionCreate(
            account_id=subscriber.id,
            offer_id=catalog_offer.id,
        ),
    )

    web_catalog_subscriptions_service.update_subscription_with_audit(
        db_session,
        str(subscription.id),
        {"login": "10004167"},
        "",
        [],
        [],
        None,
        None,
        additional_route_cidrs=["160.119.125.24/30"],
        additional_route_metrics=["1"],
        ip_addon_id="",
        ip_addon_quantity="1",
    )

    route = (
        db_session.query(SubscriberAdditionalRoute)
        .filter(SubscriberAdditionalRoute.subscriber_id == subscriber.id)
        .one()
    )
    assert route.cidr == "160.119.125.24/30"
    assert (
        db_session.query(SubscriptionAddOn)
        .filter(SubscriptionAddOn.subscription_id == subscription.id)
        .count()
        == 0
    )


def test_additional_route_update_sets_public_ip_addon_quantity_by_prefix(
    db_session,
    subscriber,
    catalog_offer,
):
    add_on = _public_ip_addon(
        db_session,
        name="/30 IP",
        ip_prefix_length=30,
    )
    subscription = catalog_service.subscriptions.create(
        db_session,
        SubscriptionCreate(
            account_id=subscriber.id,
            offer_id=catalog_offer.id,
        ),
    )

    web_catalog_subscriptions_service.update_subscription_with_audit(
        db_session,
        str(subscription.id),
        {"login": "10004167"},
        "",
        [],
        [],
        None,
        None,
        additional_route_cidrs=["160.119.125.24/30", "160.119.125.28/30"],
        additional_route_metrics=["1", "1"],
        ip_addon_id=str(add_on.id),
        ip_addon_quantity="2",
    )

    sub_addon = (
        db_session.query(SubscriptionAddOn)
        .filter(SubscriptionAddOn.subscription_id == subscription.id)
        .filter(SubscriptionAddOn.add_on_id == add_on.id)
        .filter(SubscriptionAddOn.end_at.is_(None))
        .one()
    )
    assert sub_addon.quantity == 2


def test_removing_additional_route_ends_public_ip_addon(
    db_session,
    subscriber,
    catalog_offer,
):
    add_on = _public_ip_addon(
        db_session,
        name="/30 IP",
        ip_prefix_length=30,
    )
    subscription = catalog_service.subscriptions.create(
        db_session,
        SubscriptionCreate(
            account_id=subscriber.id,
            offer_id=catalog_offer.id,
        ),
    )
    web_catalog_subscriptions_service.update_subscription_with_audit(
        db_session,
        str(subscription.id),
        {"login": "10004167"},
        "",
        [],
        [],
        None,
        None,
        additional_route_cidrs=["160.119.125.24/30"],
        additional_route_metrics=["1"],
        ip_addon_id=str(add_on.id),
        ip_addon_quantity="1",
    )

    web_catalog_subscriptions_service.update_subscription_with_audit(
        db_session,
        str(subscription.id),
        {"login": "10004167"},
        "",
        [],
        [],
        None,
        None,
        additional_route_cidrs=[""],
        additional_route_metrics=[""],
        ip_addon_id=str(add_on.id),
        ip_addon_quantity="1",
    )

    sub_addon = (
        db_session.query(SubscriptionAddOn)
        .filter(SubscriptionAddOn.subscription_id == subscription.id)
        .filter(SubscriptionAddOn.add_on_id == add_on.id)
        .one()
    )
    assert sub_addon.end_at is not None


def test_additional_route_requires_priced_public_ip_addon(
    db_session,
    subscriber,
    catalog_offer,
):
    add_on = _public_ip_addon(
        db_session,
        name="/30 IP",
        ip_prefix_length=30,
        with_price=False,
    )
    subscription = catalog_service.subscriptions.create(
        db_session,
        SubscriptionCreate(
            account_id=subscriber.id,
            offer_id=catalog_offer.id,
        ),
    )

    with pytest.raises(ValueError) as exc:
        web_catalog_subscriptions_service.update_subscription_with_audit(
            db_session,
            str(subscription.id),
            {"login": "10004167"},
            "",
            [],
            [],
            None,
            None,
            additional_route_cidrs=["160.119.125.24/30"],
            additional_route_metrics=["1"],
            ip_addon_id=str(add_on.id),
            ip_addon_quantity="1",
        )

    assert str(exc.value) == (
        "Additional routed IP block 160.119.125.24/30 requires an active /30 "
        "public IP add-on with a recurring price."
    )


def test_subscription_detail_context_includes_active_additional_routes(
    db_session,
    subscriber,
    catalog_offer,
):
    subscription = catalog_service.subscriptions.create(
        db_session,
        SubscriptionCreate(
            account_id=subscriber.id,
            offer_id=catalog_offer.id,
        ),
    )
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

    context = web_catalog_subscriptions_service.subscription_detail_context(
        db_session, subscription
    )

    assert [route.cidr for route in context["active_additional_routes"]] == [
        "102.220.189.10/32"
    ]


def test_edit_form_data_includes_active_public_ip_addon(
    db_session,
    subscriber,
    catalog_offer,
):
    add_on = _public_ip_addon(db_session)
    subscription = catalog_service.subscriptions.create(
        db_session,
        SubscriptionCreate(
            account_id=subscriber.id,
            offer_id=catalog_offer.id,
        ),
    )
    db_session.add(
        SubscriptionAddOn(
            subscription_id=subscription.id,
            add_on_id=add_on.id,
            quantity=4,
        )
    )
    db_session.commit()

    form_data = web_catalog_subscriptions_service.edit_form_data(
        db_session, subscription
    )

    assert form_data["ip_addon_id"] == str(add_on.id)
    assert form_data["ip_addon_quantity"] == "4"


def test_generic_update_cannot_deallocate_ipv4_assignments(
    db_session,
    subscriber,
    catalog_offer,
):
    pool, pool_error = web_network_ip_service.create_ip_pool(
        db_session,
        {
            "name": "Deallocation Pool",
            "ip_version": "ipv4",
            "cidr": "10.84.0.0/29",
            "is_active": True,
        },
    )
    assert pool_error is None
    assert pool is not None

    first_block, first_error = web_network_ip_service.create_ip_block(
        db_session,
        {
            "pool_id": str(pool.id),
            "cidr": "10.84.0.0/30",
            "is_active": True,
        },
    )
    second_block, second_error = web_network_ip_service.create_ip_block(
        db_session,
        {
            "pool_id": str(pool.id),
            "cidr": "10.84.0.4/30",
            "is_active": True,
        },
    )
    assert first_error is None
    assert second_error is None
    assert first_block is not None
    assert second_block is not None

    subscription = catalog_service.subscriptions.create(
        db_session,
        SubscriptionCreate(
            account_id=subscriber.id,
            offer_id=catalog_offer.id,
        ),
    )

    web_catalog_subscriptions_service._allocate_ipv4_assignments_for_subscription(
        db_session,
        subscription_obj=subscription,
        block_ids=[str(first_block.id), str(second_block.id)],
        selected_ips=["10.84.0.1", "10.84.0.5"],
    )
    subscription.ipv4_address = "10.84.0.1"
    db_session.commit()
    db_session.refresh(subscription)

    original_login = subscription.login
    with pytest.raises(ValueError, match="Use Replace service IPv4"):
        web_catalog_subscriptions_service.update_subscription_with_audit(
            db_session,
            str(subscription.id),
            {"login": "10004167"},
            "",
            [str(first_block.id)],
            ["10.84.0.1"],
            None,
            None,
        )

    db_session.refresh(subscription)
    assert subscription.login == original_login
    assert subscription.ipv4_address == "10.84.0.1"

    active_assignments = (
        db_session.query(IPAssignment)
        .filter(IPAssignment.subscriber_id == subscription.subscriber_id)
        .filter(IPAssignment.is_active.is_(True))
        .all()
    )
    assert len(active_assignments) == 2
    assert {
        assignment.ipv4_address.address
        for assignment in active_assignments
        if assignment.ipv4_address is not None
    } == {"10.84.0.1", "10.84.0.5"}


@patch("app.services.radius.reconcile_subscription_connectivity")
def test_create_subscription_with_audit_reconciles_active_subscription_after_credential_sync(
    reconcile_subscription_connectivity,
    db_session,
    subscriber,
    catalog_offer,
):
    subscriber.subscriber_number = "SUB-000002"
    db_session.add(subscriber)
    db_session.commit()
    payload = {
        "account_id": subscriber.id,
        "offer_id": catalog_offer.id,
        "status": "active",
    }
    form = FormData(
        [
            ("service_password", "password"),
        ]
    )

    created = web_catalog_subscriptions_service.create_subscription_with_audit(
        db_session,
        payload,
        form,
        None,
        None,
    )

    assert reconcile_subscription_connectivity.call_count >= 1
    reconcile_subscription_connectivity.assert_any_call(
        db_session,
        str(created.id),
    )


def test_create_subscription_with_audit_prefers_generated_pppoe_credential_when_enabled(
    db_session,
    subscriber,
    catalog_offer,
):
    subscriber.subscriber_number = "SUB-000001"
    db_session.add(subscriber)
    db_session.add_all(
        [
            _radius_setting(
                "pppoe_auto_generate_enabled",
                SettingValueType.boolean,
                value_text="true",
                value_json=True,
            ),
            _radius_setting(
                "pppoe_username_prefix",
                SettingValueType.string,
                value_text="1050",
            ),
            _radius_setting(
                "pppoe_username_padding",
                SettingValueType.integer,
                value_text="5",
            ),
            _radius_setting(
                "pppoe_username_start",
                SettingValueType.integer,
                value_text="1",
            ),
        ]
    )
    db_session.add(DocumentSequence(key="pppoe_username", next_value=1))
    db_session.commit()

    payload = {
        "account_id": subscriber.id,
        "offer_id": catalog_offer.id,
        "status": "active",
    }
    form = FormData([])

    created = web_catalog_subscriptions_service.create_subscription_with_audit(
        db_session,
        payload,
        form,
        None,
        None,
    )

    db_session.refresh(created)
    credential = (
        db_session.query(AccessCredential)
        .filter(AccessCredential.subscriber_id == subscriber.id)
        .filter(AccessCredential.is_active.is_(True))
        .one()
    )

    assert credential.username == "10000001"
    assert created.login == "10000001"
    assert created.login == web_catalog_subscriptions_service._generated_service_login(
        subscriber
    )


def test_ensure_ipv4_blocks_allocatable_rejects_duplicate_manual_ipv4_selection(
    db_session,
):
    pool, pool_error = web_network_ip_service.create_ip_pool(
        db_session,
        {
            "name": "Duplicate IPv4 Pool",
            "ip_version": "ipv4",
            "cidr": "10.81.0.0/29",
            "is_active": True,
        },
    )
    assert pool_error is None
    assert pool is not None

    first_block, first_error = web_network_ip_service.create_ip_block(
        db_session,
        {
            "pool_id": str(pool.id),
            "cidr": "10.81.0.0/30",
            "is_active": True,
        },
    )
    second_block, second_error = web_network_ip_service.create_ip_block(
        db_session,
        {
            "pool_id": str(pool.id),
            "cidr": "10.81.0.4/30",
            "is_active": True,
        },
    )
    assert first_error is None
    assert second_error is None
    assert first_block is not None
    assert second_block is not None

    with pytest.raises(ValueError, match="selected more than once"):
        web_catalog_subscriptions_service.ensure_ipv4_blocks_allocatable(
            db_session,
            [str(first_block.id), str(second_block.id)],
            ["10.81.0.5", "10.81.0.5"],
        )


def test_bulk_update_status_reports_partial_success(
    db_session, subscriber, catalog_offer
):
    from app.schemas.catalog import SubscriptionCreate
    from app.services import catalog as catalog_service
    from app.services.web_catalog_subscriptions import bulk_update_status

    active = catalog_service.subscriptions.create(
        db_session,
        SubscriptionCreate(
            account_id=subscriber.id,
            offer_id=catalog_offer.id,
            status=SubscriptionStatus.active,
        ),
    )
    from app.models.subscriber import Subscriber

    other = Subscriber(
        first_name="Bulk",
        last_name="Partial",
        email=f"bulk-{uuid4().hex[:8]}@t.example",
    )
    db_session.add(other)
    db_session.commit()
    pending = catalog_service.subscriptions.create(
        db_session,
        SubscriptionCreate(
            account_id=other.id,
            offer_id=catalog_offer.id,
            status=SubscriptionStatus.pending,
        ),
    )
    active_id = str(active.id)
    pending_id = str(pending.id)
    bogus = str(uuid4())

    result = bulk_update_status(
        db_session,
        f"{active.id},{pending.id},{bogus}",
        target_status=SubscriptionStatus.suspended,
        allowed_from=[SubscriptionStatus.active],
        request=None,
        actor_id=None,
    )

    assert result["changed"] == 1
    assert pending_id in result["skipped_ids"]
    assert bogus in result["failed_ids"]
    outcome = result["outcomes"][0]
    assert outcome["subscription_id"] == active_id
    assert outcome["kind"] == "suspend"
    assert outcome["status"] == "applied"
    assert outcome["artifact_ids"]
    db_session.refresh(active)
    assert active.status == SubscriptionStatus.suspended


def _make_active_and_suspended(db_session, subscriber, offer):
    """Create one active + one suspended subscription on ``offer``."""
    active = catalog_service.subscriptions.create(
        db_session,
        SubscriptionCreate(
            account_id=subscriber.id,
            offer_id=offer.id,
            status=SubscriptionStatus.active,
        ),
    )
    other = Subscriber(
        first_name="Susp",
        last_name="Ended",
        email=f"susp-{uuid4().hex[:8]}@t.example",
    )
    db_session.add(other)
    db_session.commit()
    suspended = catalog_service.subscriptions.create(
        db_session,
        SubscriptionCreate(
            account_id=other.id,
            offer_id=offer.id,
            status=SubscriptionStatus.suspended,
        ),
    )
    return active, suspended


def test_bulk_change_plan_excludes_suspended_by_default(
    db_session, subscriber, catalog_offer
):
    from app.models.catalog import AccessType, PriceBasis, ServiceType
    from app.schemas.catalog import CatalogOfferCreate
    from app.services.web_catalog_subscriptions import bulk_change_plan

    # Commercial family does not decide delivery; catalog compatibility does.
    catalog_offer.plan_family = "std"
    target = catalog_service.offers.create(
        db_session,
        CatalogOfferCreate(
            name="Target Plan",
            code=f"TGT-{uuid4().hex[:6]}",
            service_type=ServiceType.residential,
            access_type=AccessType.fiber,
            price_basis=PriceBasis.flat,
            plan_family="std",
        ),
    )
    db_session.commit()
    active, suspended = _make_active_and_suspended(
        db_session, subscriber, catalog_offer
    )

    result = bulk_change_plan(
        db_session,
        f"{active.id},{suspended.id}",
        str(target.id),
        request=None,
        actor_id=None,
        effective_timing="next_cycle",
    )

    assert result["changed"] == 1
    assert str(suspended.id) in result["skipped_ids"]
    db_session.refresh(active)
    db_session.refresh(suspended)
    assert active.offer_id == catalog_offer.id
    assert suspended.offer_id == catalog_offer.id


def test_bulk_change_plan_include_suspended_opt_in(
    db_session, subscriber, catalog_offer
):
    from app.models.catalog import AccessType, PriceBasis, ServiceType
    from app.schemas.catalog import CatalogOfferCreate
    from app.services.web_catalog_subscriptions import bulk_change_plan

    # Commercial family does not decide delivery; catalog compatibility does.
    catalog_offer.plan_family = "std"
    target = catalog_service.offers.create(
        db_session,
        CatalogOfferCreate(
            name="Target Plan",
            code=f"TGT-{uuid4().hex[:6]}",
            service_type=ServiceType.residential,
            access_type=AccessType.fiber,
            price_basis=PriceBasis.flat,
            plan_family="std",
        ),
    )
    db_session.commit()
    active, suspended = _make_active_and_suspended(
        db_session, subscriber, catalog_offer
    )

    result = bulk_change_plan(
        db_session,
        f"{active.id},{suspended.id}",
        str(target.id),
        request=None,
        actor_id=None,
        effective_timing="next_cycle",
        include_suspended=True,
    )

    assert result["changed"] == 2
    assert result["skipped_ids"] == []
    db_session.refresh(active)
    db_session.refresh(suspended)
    assert active.offer_id == catalog_offer.id
    assert suspended.offer_id == catalog_offer.id


def test_bulk_tariff_change_preview_excludes_suspended_by_default(
    db_session, subscriber, catalog_offer
):
    from app.services.bulk_tariff_change import bulk_tariff_change

    active, _suspended = _make_active_and_suspended(
        db_session, subscriber, catalog_offer
    )

    preview = bulk_tariff_change.preview(
        db_session,
        source_offer_id=str(catalog_offer.id),
        target_offer_id=str(catalog_offer.id),
    )

    assert preview["total_count"] == 1
    assert preview["include_suspended"] is False
    ids = {str(s.id) for s in preview["affected_subscriptions"]}
    assert ids == {str(active.id)}


def test_bulk_tariff_change_include_suspended_opt_in(
    db_session, subscriber, catalog_offer
):
    from app.services.bulk_tariff_change import bulk_tariff_change

    active, suspended = _make_active_and_suspended(
        db_session, subscriber, catalog_offer
    )

    preview = bulk_tariff_change.preview(
        db_session,
        source_offer_id=str(catalog_offer.id),
        target_offer_id=str(catalog_offer.id),
        include_suspended=True,
    )

    assert preview["total_count"] == 2
    assert preview["include_suspended"] is True
    ids = {str(s.id) for s in preview["affected_subscriptions"]}
    assert ids == {str(active.id), str(suspended.id)}
