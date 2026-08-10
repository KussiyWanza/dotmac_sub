from types import SimpleNamespace

import pytest
from fastapi import Request

from app.models.catalog import (
    AccessType,
    CatalogOffer,
    OfferStatus,
    PriceBasis,
    ServiceType,
    Subscription,
    SubscriptionStatus,
)
from app.models.network import OntAssignment, OntUnit
from app.models.subscriber import Subscriber
from app.services.customer_device_commands import (
    CustomerDeviceCommandError,
    reboot_subscription_device,
    update_subscription_wifi,
)
from app.services.customer_portal_flow_services import get_service_detail
from app.web.customer.branding import get_customer_templates


def _active_subscription_with_ont(db_session):
    subscriber = Subscriber(
        first_name="Portal",
        last_name="User",
        email="portal-device@example.com",
    )
    offer = CatalogOffer(
        name="Portal Fiber",
        service_type=ServiceType.residential,
        access_type=AccessType.fiber,
        price_basis=PriceBasis.flat,
        status=OfferStatus.active,
        is_active=True,
    )
    db_session.add_all([subscriber, offer])
    db_session.flush()
    subscription = Subscription(
        subscriber_id=subscriber.id,
        offer_id=offer.id,
        status=SubscriptionStatus.active,
    )
    ont = OntUnit(
        serial_number="PORTAL-ONT-001",
        is_active=True,
        desired_config={"wifi": {"ssid": "DesiredSSID"}},
    )
    db_session.add_all([subscription, ont])
    db_session.flush()
    db_session.add(
        OntAssignment(
            ont_unit_id=ont.id,
            subscriber_id=subscriber.id,
            subscription_id=subscription.id,
            active=True,
            wifi_ssid="LegacySSID",
        )
    )
    db_session.commit()
    return subscriber, subscription, ont


def test_uisp_ont_does_not_expose_huawei_customer_actions(db_session):
    from app.models.network import OLTDevice
    from app.models.uisp_control import (
        UispDeviceIntent,
        UispIntentStatus,
        UispIntentTargetType,
    )

    subscriber, subscription, ont = _active_subscription_with_ont(db_session)
    olt = OLTDevice(
        name="UF-OLT-PORTAL",
        vendor="ubiquiti",
        uisp_device_id="uisp-olt-portal",
    )
    db_session.add(olt)
    db_session.flush()
    ont.olt_device_id = olt.id
    ont.uisp_device_id = "uisp-onu-portal"
    db_session.add(
        UispDeviceIntent(
            target_type=UispIntentTargetType.ont,
            target_id=ont.id,
            subscription_id=subscription.id,
            uisp_device_id=ont.uisp_device_id,
            desired_state={"wifi": {"ssid": "Portal"}},
            status=UispIntentStatus.manual_required,
        )
    )
    db_session.commit()

    detail = get_service_detail(
        db_session,
        {"account_id": str(subscriber.id)},
        str(subscription.id),
    )

    assert detail is not None
    assert detail["can_reboot_ont"] is False
    assert detail["can_update_wifi"] is False
    assert detail["uisp_control_status"] == "manual_required"


def test_service_detail_exposes_customer_reboot_when_ont_is_linked(db_session):
    subscriber, subscription, ont = _active_subscription_with_ont(db_session)

    detail = get_service_detail(
        db_session,
        {"account_id": str(subscriber.id)},
        str(subscription.id),
    )

    assert detail is not None
    assert detail["can_reboot_ont"] is True
    assert detail["can_update_wifi"] is True
    assert detail["customer_wifi_ssid"] == "DesiredSSID"
    assert detail["customer_ont"].id == ont.id


def test_service_detail_renders_desired_wifi_name(db_session):
    subscriber, subscription, _ont = _active_subscription_with_ont(db_session)
    detail = get_service_detail(
        db_session,
        {"account_id": str(subscriber.id)},
        str(subscription.id),
    )
    assert detail is not None

    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": f"/portal/services/{subscription.id}",
            "query_string": b"",
            "headers": [],
            "scheme": "https",
            "server": ("testserver", 443),
            "client": ("127.0.0.1", 12345),
        }
    )
    request.state.csrf_token = "test-csrf"
    html = (
        get_customer_templates()
        .env.get_template("customer/services/detail.html")
        .render(
            request=request,
            customer={"read_only": False},
            active_page="services",
            portal_name="Dotmac",
            sidebar_stats={},
            **detail,
        )
    )

    assert 'name="ssid"' in html
    assert 'value="DesiredSSID"' in html
    assert "LegacySSID" not in html


def test_customer_reboot_delegates_to_tracked_ont_action(db_session, monkeypatch):
    subscriber, subscription, ont = _active_subscription_with_ont(db_session)
    calls = []

    def fake_execute_reboot(db, ont_id):
        calls.append(ont_id)
        return SimpleNamespace(success=True, message="TR-069 reboot sent")

    monkeypatch.setattr(
        "app.services.customer_device_commands.OntActions.reboot",
        staticmethod(fake_execute_reboot),
    )

    outcome = reboot_subscription_device(
        db_session,
        subscriber_id=subscriber.id,
        subscription_id=subscription.id,
        actor_id="customer-user-1",
    )

    assert outcome.success is True
    assert outcome.message == "TR-069 reboot sent"
    assert calls == [str(ont.id)]


def test_customer_wifi_update_delegates_to_reconciled_wifi_action(
    db_session, monkeypatch
):
    from app.services.network.ont_features import OntFeatureService

    subscriber, subscription, ont = _active_subscription_with_ont(db_session)
    calls = []

    def fake_set_wifi_config(db, ont_id, *, ssid=None, password=None, **_):
        calls.append((ont_id, ssid, password))
        return SimpleNamespace(success=True, message="WiFi updated")

    monkeypatch.setattr(
        OntFeatureService, "set_wifi_config", staticmethod(fake_set_wifi_config)
    )

    outcome = update_subscription_wifi(
        db_session,
        subscriber_id=subscriber.id,
        subscription_id=subscription.id,
        actor_id="customer-user-1",
        ssid="NewSSID",
        password="Secret123",
    )

    assert outcome.success is True
    assert outcome.message == "WiFi updated"
    assert calls == [(str(ont.id), "NewSSID", "Secret123")]


def test_customer_wifi_update_rejects_invalid_password(db_session):
    subscriber, subscription, _ont = _active_subscription_with_ont(db_session)

    with pytest.raises(CustomerDeviceCommandError) as exc:
        update_subscription_wifi(
            db_session,
            subscriber_id=subscriber.id,
            subscription_id=subscription.id,
            actor_id="customer-user-1",
            ssid="NewSSID",
            password="short",
        )
    assert exc.value.code == "invalid_wifi_password"


def test_customer_reboot_blocked_during_cooldown(db_session, monkeypatch):
    """A recent reboot operation on the same ONT blocks another customer
    reboot until the cooldown elapses (default 300s)."""
    from app.models.network_operation import (
        NetworkOperation,
        NetworkOperationStatus,
        NetworkOperationTargetType,
        NetworkOperationType,
    )

    subscriber, subscription, ont = _active_subscription_with_ont(db_session)
    db_session.add(
        NetworkOperation(
            operation_type=NetworkOperationType.ont_reboot,
            target_type=NetworkOperationTargetType.ont,
            target_id=ont.id,
            status=NetworkOperationStatus.succeeded,
        )
    )
    db_session.commit()

    calls = []
    monkeypatch.setattr(
        "app.services.customer_device_commands.OntActions.reboot",
        lambda *a, **k: (
            calls.append(1) or SimpleNamespace(success=True, message="sent")
        ),
    )

    with pytest.raises(CustomerDeviceCommandError) as exc:
        reboot_subscription_device(
            db_session,
            subscriber_id=subscriber.id,
            subscription_id=subscription.id,
            actor_id="customer-user-1",
        )
    assert exc.value.code == "reboot_cooldown"
    assert calls == []


def test_customer_reboot_allowed_after_cooldown(db_session, monkeypatch):
    from datetime import UTC, datetime, timedelta

    from app.models.network_operation import (
        NetworkOperation,
        NetworkOperationStatus,
        NetworkOperationTargetType,
        NetworkOperationType,
    )

    subscriber, subscription, ont = _active_subscription_with_ont(db_session)
    op = NetworkOperation(
        operation_type=NetworkOperationType.ont_reboot,
        target_type=NetworkOperationTargetType.ont,
        target_id=ont.id,
        status=NetworkOperationStatus.succeeded,
    )
    db_session.add(op)
    db_session.commit()
    # Age the operation past the default 300s cooldown.
    op.created_at = datetime.now(UTC) - timedelta(seconds=301)
    db_session.commit()

    monkeypatch.setattr(
        "app.services.customer_device_commands.OntActions.reboot",
        lambda *a, **k: SimpleNamespace(success=True, message="sent"),
    )

    outcome = reboot_subscription_device(
        db_session,
        subscriber_id=subscriber.id,
        subscription_id=subscription.id,
        actor_id="customer-user-1",
    )
    assert outcome.success is True


def test_failed_reboot_does_not_arm_cooldown(db_session, monkeypatch):
    """A reboot that errored never disrupted the device — it must not lock
    the customer out with 'restarted recently'."""
    from app.models.network_operation import (
        NetworkOperation,
        NetworkOperationStatus,
        NetworkOperationTargetType,
        NetworkOperationType,
    )

    subscriber, subscription, ont = _active_subscription_with_ont(db_session)
    db_session.add(
        NetworkOperation(
            operation_type=NetworkOperationType.ont_reboot,
            target_type=NetworkOperationTargetType.ont,
            target_id=ont.id,
            status=NetworkOperationStatus.failed,
        )
    )
    db_session.commit()

    monkeypatch.setattr(
        "app.services.customer_device_commands.OntActions.reboot",
        lambda *a, **k: SimpleNamespace(success=True, message="sent"),
    )

    outcome = reboot_subscription_device(
        db_session,
        subscriber_id=subscriber.id,
        subscription_id=subscription.id,
        actor_id="customer-user-1",
    )
    assert outcome.success is True
