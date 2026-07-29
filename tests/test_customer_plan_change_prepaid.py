from __future__ import annotations

import importlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi import HTTPException, Request
from sqlalchemy import select

from app.models.audit import AuditEvent
from app.models.billing import (
    AccountAdjustment,
    Invoice,
    InvoiceStatus,
    LedgerEntry,
    LedgerEntryType,
    LedgerSource,
    Payment,
    PaymentAllocation,
    PaymentStatus,
)
from app.models.catalog import (
    AccessCredential,
    AccessType,
    BillingCycle,
    BillingMode,
    CatalogOffer,
    OfferPrice,
    OfferRadiusProfile,
    OfferStatus,
    PriceBasis,
    PriceType,
    RadiusProfile,
    ServiceType,
    Subscription,
    SubscriptionStatus,
)
from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.qualification import BuildoutStatus, CoverageArea
from app.models.radius import RadiusUser
from app.models.subscriber import Address, AddressType
from app.models.subscription_change import (
    SubscriptionChangeExecutionState,
    SubscriptionChangeRequest,
)
from app.models.subscription_engine import SettingValueType
from app.services.billing._common import get_account_credit_balance
from app.services.customer_portal_context import get_available_portal_offers
from app.services.customer_portal_flow_changes import (
    confirm_service_change,
    get_plan_change_quote,
)
from app.services.subscription_change_execution import (
    RemoteProvisionActionStatus,
    SubscriptionChangeExecutionError,
    finalize_verified_remote_reprovision,
    provision_and_verify_remote_change,
    settle_relocation_payment,
)


def _make_offer(
    db_session,
    *,
    name: str,
    amount: Decimal,
    plan_family: str | None,
    currency: str = "NGN",
    billing_mode: BillingMode = BillingMode.prepaid,
    service_type: ServiceType = ServiceType.residential,
    show_on_customer_portal: bool = True,
    is_active: bool = True,
    status: OfferStatus = OfferStatus.active,
    access_type: AccessType = AccessType.fiber,
    speed_download_mbps: int | None = None,
    speed_upload_mbps: int | None = None,
) -> CatalogOffer:
    offer = CatalogOffer(
        name=name,
        code=name.lower().replace(" ", "-"),
        service_type=service_type,
        access_type=access_type,
        speed_download_mbps=speed_download_mbps,
        speed_upload_mbps=speed_upload_mbps,
        price_basis=PriceBasis.flat,
        billing_cycle=BillingCycle.monthly,
        billing_mode=billing_mode,
        plan_family=plan_family,
        show_on_customer_portal=show_on_customer_portal,
        is_active=is_active,
        status=status,
    )
    db_session.add(offer)
    db_session.flush()
    db_session.add(
        OfferPrice(
            offer_id=offer.id,
            price_type=PriceType.recurring,
            amount=amount,
            currency=currency,
            billing_cycle=BillingCycle.monthly,
            is_active=True,
        )
    )
    db_session.commit()
    db_session.refresh(offer)
    return offer


def _make_subscription(
    db_session,
    subscriber,
    offer: CatalogOffer,
    *,
    next_billing_at: datetime,
    start_at: datetime,
) -> Subscription:
    subscription = Subscription(
        subscriber_id=subscriber.id,
        offer_id=offer.id,
        status=SubscriptionStatus.active,
        billing_mode=offer.billing_mode,
        start_at=start_at,
        next_billing_at=next_billing_at,
    )
    db_session.add(subscription)
    db_session.commit()
    db_session.refresh(subscription)
    return subscription


def _freeze_subscription_now(monkeypatch, frozen: datetime) -> None:
    class _FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return frozen.replace(tzinfo=None)
            return frozen.astimezone(tz)

    subscriptions_service = importlib.import_module(
        "app.services.catalog.subscriptions"
    )

    monkeypatch.setattr(subscriptions_service, "datetime", _FrozenDateTime)


def _stub_plan_change_side_effects(
    monkeypatch, event_types: list | None = None
) -> None:
    import app.services.enforcement as enforcement_service
    import app.services.radius as radius_service

    subscriptions_service = importlib.import_module(
        "app.services.catalog.subscriptions"
    )

    monkeypatch.setattr(
        subscriptions_service,
        "_sync_credentials_to_radius",
        lambda db, subscriber_id: None,
    )
    monkeypatch.setattr(
        enforcement_service,
        "update_subscription_sessions",
        lambda db, subscription_id, reason=None: None,
        raising=False,
    )
    monkeypatch.setattr(
        radius_service,
        "reconcile_subscription_connectivity",
        lambda db, subscription_id: radius_service.SubscriptionConnectivityOutcome(
            subscription_id=str(subscription_id),
            disposition=radius_service.ConnectivityProjectionDisposition.projected,
            requested_logins=1,
            projected_logins=1,
            projection_targets=1,
        ),
        raising=False,
    )
    monkeypatch.setattr(
        subscriptions_service,
        "emit_event",
        (
            lambda db, event_type, payload, **kwargs: (
                event_types.append(event_type) if event_types is not None else None
            )
        ),
    )


def _confirmation_kwargs(db_session, subscription, target_offer) -> dict[str, object]:
    from app.services.prepaid_plan_changes import resolve_prepaid_plan_change

    decision = resolve_prepaid_plan_change(
        db_session, subscription, str(target_offer.id)
    )
    return {
        "preview_fingerprint": decision.fingerprint,
        "preview_effective_at": decision.effective_at,
        "idempotency_key": f"test-plan-{uuid4()}",
        "confirmation_origin": "test",
    }


def test_get_available_portal_offers_ignores_plan_family_but_keeps_compatibility(
    db_session, subscriber
):
    current_offer = _make_offer(
        db_session,
        name="Unlimited Basic",
        amount=Decimal("100.00"),
        plan_family="unlimited",
    )
    allowed_offer = _make_offer(
        db_session,
        name="Unlimited Plus",
        amount=Decimal("150.00"),
        plan_family="unlimited",
    )
    cross_family_offer = _make_offer(
        db_session,
        name="Dedicated 1",
        amount=Decimal("300.00"),
        plan_family="dedicated",
    )
    _make_offer(
        db_session,
        name="Unlimited Postpaid",
        amount=Decimal("150.00"),
        plan_family="unlimited",
        billing_mode=BillingMode.postpaid,
    )
    subscription = _make_subscription(
        db_session,
        subscriber,
        current_offer,
        next_billing_at=datetime(2026, 6, 1, tzinfo=UTC),
        start_at=datetime(2026, 5, 1, tzinfo=UTC),
    )

    offers = get_available_portal_offers(db_session, subscription)

    assert {str(offer.id) for offer in offers} == {
        str(current_offer.id),
        str(allowed_offer.id),
        str(cross_family_offer.id),
    }


def test_zero_price_offer_is_not_available_for_customer_plan_change(
    db_session, subscriber
):
    current_offer = _make_offer(
        db_session,
        name="Unlimited Paid",
        amount=Decimal("100.00"),
        plan_family="unlimited",
    )
    zero_price_offer = _make_offer(
        db_session,
        name="Unlimited Internal",
        amount=Decimal("0.00"),
        plan_family="unlimited",
    )
    subscription = _make_subscription(
        db_session,
        subscriber,
        current_offer,
        next_billing_at=datetime(2026, 6, 1, tzinfo=UTC),
        start_at=datetime(2026, 5, 1, tzinfo=UTC),
    )

    offers = get_available_portal_offers(db_session, subscription)
    assert zero_price_offer.id not in {offer.id for offer in offers}

    with pytest.raises(ValueError, match="not available for self-service change"):
        confirm_service_change(
            db_session,
            {"account_id": str(subscriber.id), "subscriber_id": str(subscriber.id)},
            str(subscription.id),
            str(zero_price_offer.id),
            preview_fingerprint="x" * 64,
            preview_effective_at=datetime(2026, 5, 15, tzinfo=UTC),
            idempotency_key=f"test-plan-{uuid4()}",
            confirmation_origin="test",
        )

    db_session.refresh(subscription)
    assert subscription.offer_id == current_offer.id


def test_change_plan_page_classifies_cross_family_from_network_intent(
    db_session, subscriber
):
    from app.services import customer_portal_flow_changes as flow

    current_offer = _make_offer(
        db_session,
        name="Unlimited Basic",
        amount=Decimal("100.00"),
        plan_family="unlimited",
    )
    instant_offer = _make_offer(
        db_session,
        name="Unlimited Plus",
        amount=Decimal("150.00"),
        plan_family="unlimited",
    )
    cross_family_offer = _make_offer(
        db_session,
        name="Dedicated 1",
        amount=Decimal("300.00"),
        plan_family="dedicated",
    )
    subscription = _make_subscription(
        db_session,
        subscriber,
        current_offer,
        next_billing_at=datetime(2026, 6, 1, tzinfo=UTC),
        start_at=datetime(2026, 5, 1, tzinfo=UTC),
    )

    page = flow.get_change_plan_page(
        db_session,
        {"account_id": str(subscriber.id)},
        str(subscription.id),
    )

    assert page is not None
    assert {str(offer.id) for offer in page["available_offers"]} == {
        str(current_offer.id),
        str(instant_offer.id),
        str(cross_family_offer.id),
    }
    assert (
        page["available_offer_delivery_modes"][str(cross_family_offer.id)]
        == "commercial_only"
    )


@pytest.mark.parametrize(
    ("target_access", "target_speed", "expected_mode"),
    [
        (AccessType.fiber, 100, "remote_reprovision"),
        (AccessType.fixed_wireless, None, "field_migration"),
    ],
)
def test_confirm_service_change_queues_delivery_without_ticket_or_plan_swap(
    db_session,
    subscriber,
    target_access,
    target_speed,
    expected_mode,
):
    current_offer = _make_offer(
        db_session,
        name="Current Fibre",
        amount=Decimal("100.00"),
        plan_family="unlimited",
    )
    target_offer = _make_offer(
        db_session,
        name=f"Target {expected_mode}",
        amount=Decimal("150.00"),
        plan_family="dedicated",
        access_type=target_access,
        speed_download_mbps=target_speed,
    )
    subscription = _make_subscription(
        db_session,
        subscriber,
        current_offer,
        next_billing_at=datetime(2026, 6, 1, tzinfo=UTC),
        start_at=datetime(2026, 5, 1, tzinfo=UTC),
    )
    if expected_mode == "remote_reprovision":
        profile = RadiusProfile(
            name="Target remote profile",
            download_speed=target_speed * 1000,
            upload_speed=target_speed * 1000,
        )
        db_session.add(profile)
        db_session.flush()
        db_session.add(
            OfferRadiusProfile(offer_id=target_offer.id, profile_id=profile.id)
        )
        db_session.add(
            AccessCredential(
                subscriber_id=subscriber.id,
                subscription_id=subscription.id,
                username=f"remote-{subscription.id}",
                is_active=True,
            )
        )
        db_session.commit()

    confirmation = _confirmation_kwargs(db_session, subscription, target_offer)
    result = confirm_service_change(
        db_session,
        {"account_id": str(subscriber.id), "subscriber_id": str(subscriber.id)},
        str(subscription.id),
        str(target_offer.id),
        **confirmation,
    )
    replay = confirm_service_change(
        db_session,
        {"account_id": str(subscriber.id), "subscriber_id": str(subscriber.id)},
        str(subscription.id),
        str(target_offer.id),
        **confirmation,
    )

    db_session.refresh(subscription)
    request = db_session.get(SubscriptionChangeRequest, result["change_request_id"])
    assert result["status"] == "scheduled"
    assert result["delivery_mode"] == expected_mode
    assert replay["replayed"] is True
    assert replay["change_request_id"] == result["change_request_id"]
    assert subscription.offer_id == current_offer.id
    assert request is not None
    assert request.status.value == "pending"
    assert request.confirmation_snapshot["delivery_mode"] == expected_mode
    assert request.confirmation_snapshot["delivery_state"] == "awaiting_verification"
    if expected_mode == "remote_reprovision":
        assert request.execution_state == SubscriptionChangeExecutionState.provisioning
        assert request.remote_radius_profile_id == profile.id


def test_remote_reprovision_finalizes_only_after_exact_fresh_radius_observation(
    db_session, subscriber, monkeypatch
):
    _stub_plan_change_side_effects(monkeypatch)
    current_offer = _make_offer(
        db_session,
        name="Remote Current",
        amount=Decimal("100.00"),
        plan_family="fiber",
        speed_download_mbps=50,
    )
    target_offer = _make_offer(
        db_session,
        name="Remote Target",
        amount=Decimal("150.00"),
        plan_family="fiber",
        speed_download_mbps=100,
    )
    profile = RadiusProfile(
        name="Remote 100M",
        download_speed=100000,
        upload_speed=100000,
    )
    db_session.add(profile)
    db_session.flush()
    db_session.add(OfferRadiusProfile(offer_id=target_offer.id, profile_id=profile.id))
    subscription = _make_subscription(
        db_session,
        subscriber,
        current_offer,
        next_billing_at=datetime(2026, 6, 1, tzinfo=UTC),
        start_at=datetime(2026, 5, 1, tzinfo=UTC),
    )
    credential = AccessCredential(
        subscriber_id=subscriber.id,
        subscription_id=subscription.id,
        username=f"remote-verify-{subscription.id}",
        is_active=True,
    )
    db_session.add(credential)
    db_session.commit()

    result = confirm_service_change(
        db_session,
        {"account_id": str(subscriber.id), "subscriber_id": str(subscriber.id)},
        str(subscription.id),
        str(target_offer.id),
        **_confirmation_kwargs(db_session, subscription, target_offer),
    )
    request = db_session.get(SubscriptionChangeRequest, result["change_request_id"])
    assert request is not None
    with pytest.raises(
        SubscriptionChangeExecutionError,
        match="exact target RADIUS profile has not been observed",
    ):
        finalize_verified_remote_reprovision(
            db_session, request_id=request.id, actor_id="radius-reconciler"
        )

    observed_at = request.remote_reprovision_requested_at + timedelta(seconds=1)
    radius_user = RadiusUser(
        subscriber_id=subscriber.id,
        subscription_id=subscription.id,
        access_credential_id=credential.id,
        username=credential.username,
        radius_profile_id=profile.id,
        is_active=True,
        last_sync_at=observed_at,
    )
    db_session.add(radius_user)
    db_session.commit()

    finalized = finalize_verified_remote_reprovision(
        db_session, request_id=request.id, actor_id="radius-reconciler"
    )
    db_session.refresh(subscription)
    assert finalized.execution_state == SubscriptionChangeExecutionState.completed
    assert finalized.remote_radius_user_id == radius_user.id
    assert subscription.offer_id == target_offer.id


def test_operator_remote_provision_projects_then_verifies_exact_profile(
    db_session, subscriber, monkeypatch
):
    _stub_plan_change_side_effects(monkeypatch)
    current_offer = _make_offer(
        db_session,
        name="Unlimited Basic",
        amount=Decimal("100.00"),
        plan_family="unlimited",
        speed_download_mbps=10,
    )
    target_offer = _make_offer(
        db_session,
        name="Unlimited Elite",
        amount=Decimal("150.00"),
        plan_family="unlimited",
        speed_download_mbps=50,
    )
    profile = RadiusProfile(
        name="Unlimited Elite",
        download_speed=50000,
        upload_speed=50000,
    )
    db_session.add(profile)
    db_session.flush()
    db_session.add(OfferRadiusProfile(offer_id=target_offer.id, profile_id=profile.id))
    subscription = _make_subscription(
        db_session,
        subscriber,
        current_offer,
        next_billing_at=datetime(2026, 6, 1, tzinfo=UTC),
        start_at=datetime(2026, 5, 1, tzinfo=UTC),
    )
    credential = AccessCredential(
        subscriber_id=subscriber.id,
        subscription_id=subscription.id,
        username=f"operator-remote-{subscription.id}",
        is_active=True,
    )
    db_session.add(credential)
    db_session.commit()
    confirmation = confirm_service_change(
        db_session,
        {"account_id": str(subscriber.id), "subscriber_id": str(subscriber.id)},
        str(subscription.id),
        str(target_offer.id),
        **_confirmation_kwargs(db_session, subscription, target_offer),
    )
    change = db_session.get(
        SubscriptionChangeRequest, confirmation["change_request_id"]
    )
    assert change is not None

    def _project(_db, subscription_id):
        assert subscription_id == str(subscription.id)
        radius_user = _db.scalar(
            select(RadiusUser).where(RadiusUser.access_credential_id == credential.id)
        )
        if radius_user is None:
            radius_user = RadiusUser(
                subscriber_id=subscriber.id,
                subscription_id=subscription.id,
                access_credential_id=credential.id,
                username=credential.username,
                is_active=True,
            )
            _db.add(radius_user)
        radius_user.radius_profile_id = profile.id
        radius_user.last_sync_at = change.remote_reprovision_requested_at + timedelta(
            seconds=1
        )
        _db.flush()
        from app.services.radius import (
            ConnectivityProjectionDisposition,
            SubscriptionConnectivityOutcome,
        )

        return SubscriptionConnectivityOutcome(
            subscription_id=subscription_id,
            disposition=ConnectivityProjectionDisposition.projected,
            projected_logins=1,
            projection_targets=1,
        )

    monkeypatch.setattr(
        "app.services.radius.reconcile_subscription_connectivity", _project
    )
    outcome = provision_and_verify_remote_change(
        db_session,
        request_id=change.id,
        subscription_id=subscription.id,
        account_id=subscriber.id,
        actor_id="network-operator",
        idempotency_key=f"remote-plan-change:{change.id}",
        reason="Operator requested RADIUS provisioning and verification",
    )

    db_session.refresh(subscription)
    db_session.refresh(change)
    assert outcome.status == RemoteProvisionActionStatus.completed
    assert outcome.target_offer_name == "Unlimited Elite"
    assert outcome.target_profile_name == "Unlimited Elite"
    assert subscription.offer_id == target_offer.id
    assert change.execution_state == SubscriptionChangeExecutionState.completed
    assert change.reconciliation_actor_id == "network-operator"
    assert change.reconciled_at is not None


def test_operator_remote_provision_fails_closed_when_projection_target_unavailable(
    db_session, subscriber, monkeypatch
):
    _stub_plan_change_side_effects(monkeypatch)
    current_offer = _make_offer(
        db_session,
        name="Current",
        amount=Decimal("100.00"),
        plan_family="unlimited",
        speed_download_mbps=10,
    )
    target_offer = _make_offer(
        db_session,
        name="Target",
        amount=Decimal("150.00"),
        plan_family="unlimited",
        speed_download_mbps=50,
    )
    profile = RadiusProfile(name="Target", download_speed=50000, upload_speed=50000)
    db_session.add(profile)
    db_session.flush()
    db_session.add(OfferRadiusProfile(offer_id=target_offer.id, profile_id=profile.id))
    subscription = _make_subscription(
        db_session,
        subscriber,
        current_offer,
        next_billing_at=datetime(2026, 6, 1, tzinfo=UTC),
        start_at=datetime(2026, 5, 1, tzinfo=UTC),
    )
    db_session.add(
        AccessCredential(
            subscriber_id=subscriber.id,
            subscription_id=subscription.id,
            username=f"operator-failure-{subscription.id}",
            is_active=True,
        )
    )
    db_session.commit()
    confirmation = confirm_service_change(
        db_session,
        {"account_id": str(subscriber.id), "subscriber_id": str(subscriber.id)},
        str(subscription.id),
        str(target_offer.id),
        **_confirmation_kwargs(db_session, subscription, target_offer),
    )
    change = db_session.get(
        SubscriptionChangeRequest, confirmation["change_request_id"]
    )
    assert change is not None
    from app.services.radius import (
        ConnectivityProjectionDisposition,
        SubscriptionConnectivityOutcome,
    )

    monkeypatch.setattr(
        "app.services.radius.reconcile_subscription_connectivity",
        lambda _db, subscription_id: SubscriptionConnectivityOutcome(
            subscription_id=subscription_id,
            disposition=ConnectivityProjectionDisposition.target_unavailable,
        ),
    )

    with pytest.raises(
        SubscriptionChangeExecutionError,
        match="No active RADIUS projection target is available",
    ):
        provision_and_verify_remote_change(
            db_session,
            request_id=change.id,
            subscription_id=subscription.id,
            account_id=subscriber.id,
            actor_id="network-operator",
            idempotency_key=f"remote-plan-change:{change.id}",
            reason="Operator requested RADIUS provisioning and verification",
        )
    assert subscription.offer_id == current_offer.id


def test_wireless_address_relocation_is_qualified_priced_and_awaits_payment(
    db_session, subscriber
):
    offer = _make_offer(
        db_session,
        name="Wireless 50",
        amount=Decimal("18500.00"),
        plan_family="wireless",
        access_type=AccessType.fixed_wireless,
        speed_download_mbps=50,
    )
    fee_offer = _make_offer(
        db_session,
        name="Wireless relocation",
        amount=Decimal("0.00"),
        plan_family="field_fee",
        access_type=AccessType.fixed_wireless,
        show_on_customer_portal=False,
    )
    db_session.add(
        OfferPrice(
            offer_id=fee_offer.id,
            price_type=PriceType.one_time,
            amount=Decimal("25000.00"),
            currency="NGN",
            is_active=True,
        )
    )
    current_address = Address(
        subscriber_id=subscriber.id,
        address_type=AddressType.service,
        label="Current site",
        address_line1="1 Current Street",
        city="Abuja",
        region="FCT",
        latitude=9.05,
        longitude=7.48,
        is_primary=True,
    )
    target_address = Address(
        subscriber_id=subscriber.id,
        address_type=AddressType.service,
        label="New site",
        address_line1="2 New Street",
        city="Abuja",
        region="FCT",
        latitude=9.06,
        longitude=7.49,
    )
    db_session.add_all([current_address, target_address])
    db_session.add(
        CoverageArea(
            name="Abuja wireless",
            buildout_status=BuildoutStatus.ready,
            serviceable=True,
            geometry_geojson={
                "type": "Polygon",
                "coordinates": [
                    [
                        [7.0, 8.5],
                        [8.0, 8.5],
                        [8.0, 9.5],
                        [7.0, 9.5],
                        [7.0, 8.5],
                    ]
                ],
            },
            constraints={"allowed_tech": ["fixed_wireless"]},
        )
    )
    db_session.add(
        DomainSetting(
            domain=SettingDomain.projects,
            key="wireless_relocation_offer_id",
            value_type=SettingValueType.string,
            value_text=str(fee_offer.id),
            is_active=True,
        )
    )
    db_session.commit()
    subscription = _make_subscription(
        db_session,
        subscriber,
        offer,
        next_billing_at=datetime(2026, 6, 1, tzinfo=UTC),
        start_at=datetime(2026, 5, 1, tzinfo=UTC),
    )
    subscription.service_address_id = current_address.id
    db_session.commit()

    quote = get_plan_change_quote(
        db_session,
        {"account_id": str(subscriber.id)},
        str(subscription.id),
        str(offer.id),
        target_service_address_id=str(target_address.id),
    )

    assert quote is not None
    assert quote["delivery_mode"] == "field_migration"
    assert quote["field_delivery_quote"]["qualification_status"] == "eligible"
    assert quote["field_delivery_quote"]["fee_amount"] == 25000.0
    assert quote["field_delivery_quote"]["eligible"] is True

    result = confirm_service_change(
        db_session,
        {"account_id": str(subscriber.id), "subscriber_id": str(subscriber.id)},
        str(subscription.id),
        str(offer.id),
        target_service_address_id=str(target_address.id),
        preview_fingerprint=quote["preview_fingerprint"],
        field_quote_fingerprint=quote["field_delivery_quote"]["preview_fingerprint"],
        preview_effective_at=datetime.fromisoformat(quote["preview_effective_at"]),
        idempotency_key=f"wireless-relocation-{uuid4()}",
        confirmation_origin="test",
    )

    request = db_session.get(SubscriptionChangeRequest, result["change_request_id"])
    db_session.refresh(subscription)
    assert result["status"] == "scheduled"
    assert request is not None
    assert request.target_service_address_id == target_address.id
    assert request.service_qualification_id is not None
    assert request.field_fee_offer_id == fee_offer.id
    assert request.field_fee_amount == Decimal("25000.00")
    assert request.execution_state == SubscriptionChangeExecutionState.awaiting_payment
    assert request.field_fee_invoice_id is not None
    invoice = db_session.get(Invoice, request.field_fee_invoice_id)
    assert invoice is not None
    assert invoice.total == Decimal("25000.00")
    assert invoice.currency == "NGN"
    assert invoice.metadata_["payment_flow"] == "subscription_relocation"
    assert invoice.metadata_["subscription_change_request_id"] == str(request.id)
    assert request.confirmation_snapshot["delivery_state"] == "awaiting_payment"
    assert subscription.service_address_id == current_address.id
    assert subscription.offer_id == offer.id

    payment = Payment(
        account_id=subscriber.id,
        amount=Decimal("25000.00"),
        currency="NGN",
        status=PaymentStatus.succeeded,
        paid_at=datetime.now(UTC),
        is_active=True,
    )
    db_session.add(payment)
    db_session.flush()
    db_session.add(
        PaymentAllocation(
            payment_id=payment.id,
            invoice_id=invoice.id,
            amount=Decimal("25000.00"),
            is_active=True,
        )
    )
    invoice.status = InvoiceStatus.paid
    invoice.balance_due = Decimal("0.00")
    db_session.commit()

    fulfillment = settle_relocation_payment(
        db_session, request_id=request.id, payment_id=payment.id
    )
    db_session.commit()
    db_session.refresh(request)
    db_session.refresh(subscription)
    assert fulfillment.replayed is False
    assert request.execution_state == (
        SubscriptionChangeExecutionState.fulfillment_released
    )
    assert request.field_fee_payment_id == payment.id
    assert request.service_order_id == fulfillment.service_order_id
    assert request.work_order_id == fulfillment.work_order_id
    assert subscription.service_address_id == current_address.id
    assert subscription.offer_id == offer.id


def test_legacy_plan_migration_ticket_route_is_retired():
    from app.services import customer_portal_flow_changes as flow
    from app.web.customer import router as customer_router

    paths = {getattr(route, "path", "") for route in customer_router.routes}
    assert "/services/{subscription_id}/migration-request" not in paths
    assert not hasattr(flow, "request_plan_migration")


def test_validate_plan_change_allows_cross_family_compatible_change(
    db_session, subscriber, monkeypatch
):
    from app.schemas.catalog import SubscriptionUpdate
    from app.services import catalog as catalog_service

    current_offer = _make_offer(
        db_session,
        name="Unlimited Basic",
        amount=Decimal("100.00"),
        plan_family="unlimited",
    )
    target_offer = _make_offer(
        db_session,
        name="Dedicated 1",
        amount=Decimal("300.00"),
        plan_family="dedicated",
    )
    subscription = _make_subscription(
        db_session,
        subscriber,
        current_offer,
        next_billing_at=datetime(2026, 6, 1, tzinfo=UTC),
        start_at=datetime(2026, 5, 1, tzinfo=UTC),
    )

    _stub_plan_change_side_effects(monkeypatch)
    catalog_service.subscriptions.update(
        db_session,
        str(subscription.id),
        SubscriptionUpdate(offer_id=target_offer.id),
        skip_proration_artifacts=True,
    )

    db_session.refresh(subscription)
    assert subscription.offer_id == target_offer.id


def test_prepaid_upgrade_returns_insufficient_balance_without_mutation(
    db_session, subscriber, monkeypatch
):
    current_offer = _make_offer(
        db_session,
        name="Unlimited Basic",
        amount=Decimal("100.00"),
        plan_family="unlimited",
    )
    target_offer = _make_offer(
        db_session,
        name="Unlimited Plus",
        amount=Decimal("200.00"),
        plan_family="unlimited",
    )
    subscription = _make_subscription(
        db_session,
        subscriber,
        current_offer,
        next_billing_at=datetime(2026, 6, 1, 0, 0, tzinfo=UTC),
        start_at=datetime(2026, 5, 1, 0, 0, tzinfo=UTC),
    )
    _freeze_subscription_now(monkeypatch, datetime(2026, 5, 16, 12, 0, tzinfo=UTC))

    import app.services.subscription_changes as change_service

    create_mock = Mock()
    apply_mock = Mock()
    monkeypatch.setattr(
        change_service.subscription_change_requests, "create", create_mock
    )
    monkeypatch.setattr(
        change_service.subscription_change_requests, "apply", apply_mock
    )

    confirmation = _confirmation_kwargs(db_session, subscription, target_offer)
    # The customer can spend time reading the preview. Confirmation must reuse
    # its frozen pricing timestamp instead of recalculating three seconds later.
    _freeze_subscription_now(monkeypatch, datetime(2026, 5, 16, 12, 0, 3, tzinfo=UTC))

    result = confirm_service_change(
        db_session,
        {
            "account_id": str(subscriber.id),
            "subscriber_id": str(subscriber.id),
        },
        str(subscription.id),
        str(target_offer.id),
        **confirmation,
    )

    db_session.refresh(subscription)
    assert result["success"] is False
    assert result["reason"] == "insufficient_prepaid_funding"
    assert result["required_amount"] == Decimal("50.00")
    assert result["prepaid_funding_before"] == Decimal("0.00")
    assert result["shortfall"] == Decimal("50.00")
    assert subscription.offer_id == current_offer.id
    assert db_session.query(LedgerEntry).count() == 0
    create_mock.assert_not_called()
    apply_mock.assert_not_called()


def test_proration_uses_exact_cycle_seconds(db_session, subscriber, monkeypatch):
    from app.services.catalog.subscriptions import _calculate_proration

    current_offer = _make_offer(
        db_session,
        name="Unlimited Basic",
        amount=Decimal("100.00"),
        plan_family="unlimited",
    )
    target_offer = _make_offer(
        db_session,
        name="Unlimited Plus",
        amount=Decimal("200.00"),
        plan_family="unlimited",
    )
    subscription = _make_subscription(
        db_session,
        subscriber,
        current_offer,
        next_billing_at=datetime(2026, 6, 1, 0, 0, tzinfo=UTC),
        start_at=datetime(2026, 5, 1, 0, 0, tzinfo=UTC),
    )
    _freeze_subscription_now(monkeypatch, datetime(2026, 5, 16, 12, 0, tzinfo=UTC))

    proration = _calculate_proration(db_session, subscription, str(target_offer.id))

    assert proration["remaining_ratio"] == Decimal("0.5")
    assert proration["total_cycle_seconds"] == 2678400
    assert proration["remaining_cycle_seconds"] == 1339200
    assert proration["credit_amount"] == Decimal("50.00")
    assert proration["charge_amount"] == Decimal("100.00")
    assert proration["net_amount"] == Decimal("50.00")

    from app.services.prepaid_plan_changes import resolve_prepaid_plan_change

    first = resolve_prepaid_plan_change(
        db_session,
        subscription,
        str(target_offer.id),
        effective_at=datetime(2026, 5, 16, 12, 0, 0, tzinfo=UTC),
    )
    tampered = resolve_prepaid_plan_change(
        db_session,
        subscription,
        str(target_offer.id),
        effective_at=datetime(2026, 5, 16, 12, 0, 3, tzinfo=UTC),
    )
    assert first.fingerprint != tampered.fingerprint


def test_prepaid_upgrade_with_exact_funding_preserves_anniversary_and_posts_debit(
    db_session, subscriber, monkeypatch
):
    current_offer = _make_offer(
        db_session,
        name="Unlimited Basic",
        amount=Decimal("100.00"),
        plan_family="unlimited",
    )
    target_offer = _make_offer(
        db_session,
        name="Unlimited Plus",
        amount=Decimal("200.00"),
        plan_family="unlimited",
    )
    next_billing_at = datetime(2026, 6, 1, 0, 0, tzinfo=UTC)
    subscription = _make_subscription(
        db_session,
        subscriber,
        current_offer,
        next_billing_at=next_billing_at,
        start_at=datetime(2026, 5, 1, 0, 0, tzinfo=UTC),
    )
    _freeze_subscription_now(monkeypatch, datetime(2026, 5, 16, 12, 0, tzinfo=UTC))
    _stub_plan_change_side_effects(monkeypatch)

    db_session.add(
        LedgerEntry(
            account_id=subscriber.id,
            entry_type=LedgerEntryType.credit,
            source=LedgerSource.payment,
            amount=Decimal("50.00"),
            currency="NGN",
            memo="Prepaid funding",
        )
    )
    db_session.commit()

    confirmation = _confirmation_kwargs(db_session, subscription, target_offer)
    _freeze_subscription_now(monkeypatch, datetime(2026, 5, 16, 12, 0, 3, tzinfo=UTC))
    result = confirm_service_change(
        db_session,
        {
            "account_id": str(subscriber.id),
            "subscriber_id": str(subscriber.id),
        },
        str(subscription.id),
        str(target_offer.id),
        **confirmation,
    )

    db_session.refresh(subscription)
    debits = (
        db_session.query(LedgerEntry)
        .filter(LedgerEntry.account_id == subscriber.id)
        .filter(LedgerEntry.entry_type == LedgerEntryType.debit)
        .all()
    )

    assert result["success"] is True
    assert subscription.offer_id == target_offer.id
    assert subscription.next_billing_at.replace(tzinfo=UTC) == next_billing_at
    assert len(debits) == 1
    assert debits[0].amount == Decimal("50.00")
    adjustment = db_session.query(AccountAdjustment).one()
    assert adjustment.origin == "prepaid_plan_change"
    assert adjustment.origin_ref == f"{subscription.id}:{target_offer.id}"
    assert adjustment.ledger_entry_id == debits[0].id
    change_request = db_session.get(
        SubscriptionChangeRequest, result["change_request_id"]
    )
    assert change_request is not None
    assert change_request.confirmation_preview_fingerprint
    assert change_request.confirmation_idempotency_key
    assert change_request.confirmation_snapshot["prepaid_funding_before"] == "50.00"
    assert change_request.confirmation_snapshot["postpaid_receivables"] == "0.00"
    assert change_request.confirmation_snapshot["ledger_entry_type"] == "debit"
    assert change_request.confirmation_snapshot["ledger_source"] == "adjustment"
    assert change_request.confirmation_snapshot["effective_at"] == (
        "2026-05-16T12:00:00+00:00"
    )
    assert change_request.account_adjustment_id == adjustment.id
    assert change_request.ledger_entry_id == debits[0].id
    assert change_request.credit_note_id is None
    audit = (
        db_session.query(AuditEvent)
        .filter(AuditEvent.action == "confirm_immediate_plan_change")
        .filter(AuditEvent.entity_id == str(change_request.id))
        .one()
    )
    assert audit.metadata_["preview_fingerprint"] == (
        change_request.confirmation_preview_fingerprint
    )
    assert audit.metadata_["account_adjustment_id"] == str(adjustment.id)
    assert audit.metadata_["ledger_entry_id"] == str(debits[0].id)
    assert get_account_credit_balance(db_session, str(subscriber.id)) == Decimal("0.00")
    assert db_session.query(Invoice).count() == 0


def test_plan_change_confirmation_rejects_stale_financial_position(
    db_session, subscriber, monkeypatch
):
    _stub_plan_change_side_effects(monkeypatch)
    current_offer = _make_offer(
        db_session,
        name="Stale Basic",
        amount=Decimal("100.00"),
        plan_family="unlimited",
    )
    target_offer = _make_offer(
        db_session,
        name="Stale Plus",
        amount=Decimal("200.00"),
        plan_family="unlimited",
    )
    subscription = _make_subscription(
        db_session,
        subscriber,
        current_offer,
        next_billing_at=datetime(2026, 6, 1, tzinfo=UTC),
        start_at=datetime(2026, 5, 1, tzinfo=UTC),
    )
    _freeze_subscription_now(monkeypatch, datetime(2026, 5, 16, 12, tzinfo=UTC))
    db_session.add(
        LedgerEntry(
            account_id=subscriber.id,
            entry_type=LedgerEntryType.credit,
            source=LedgerSource.payment,
            amount=Decimal("50.00"),
            currency="NGN",
            memo="Previewed funding",
        )
    )
    db_session.commit()
    confirmation = _confirmation_kwargs(db_session, subscription, target_offer)
    db_session.add(
        LedgerEntry(
            account_id=subscriber.id,
            entry_type=LedgerEntryType.credit,
            source=LedgerSource.payment,
            amount=Decimal("1.00"),
            currency="NGN",
            memo="Financial position changed after preview",
        )
    )
    db_session.commit()

    with pytest.raises(HTTPException) as exc:
        confirm_service_change(
            db_session,
            {"account_id": str(subscriber.id), "subscriber_id": str(subscriber.id)},
            str(subscription.id),
            str(target_offer.id),
            **confirmation,
        )

    assert exc.value.status_code == 409
    db_session.refresh(subscription)
    assert subscription.offer_id == current_offer.id
    assert db_session.query(AccountAdjustment).count() == 0
    assert db_session.query(SubscriptionChangeRequest).count() == 0


def test_prepaid_downgrade_links_credit_note_and_exact_ledger_evidence(
    db_session, subscriber, monkeypatch
):
    from app.models.billing import CreditNote
    from app.models.domain_settings import DomainSetting, SettingDomain
    from app.models.subscription_engine import SettingValueType

    _stub_plan_change_side_effects(monkeypatch)
    db_session.add(
        DomainSetting(
            domain=SettingDomain.billing,
            key="refund_policy",
            value_type=SettingValueType.string,
            value_text="prorated",
            is_active=True,
        )
    )
    db_session.commit()
    current_offer = _make_offer(
        db_session,
        name="Credit Plus",
        amount=Decimal("200.00"),
        plan_family="unlimited",
    )
    target_offer = _make_offer(
        db_session,
        name="Credit Basic",
        amount=Decimal("100.00"),
        plan_family="unlimited",
    )
    subscription = _make_subscription(
        db_session,
        subscriber,
        current_offer,
        next_billing_at=datetime(2026, 6, 1, tzinfo=UTC),
        start_at=datetime(2026, 5, 1, tzinfo=UTC),
    )
    _freeze_subscription_now(monkeypatch, datetime(2026, 5, 16, 12, tzinfo=UTC))

    result = confirm_service_change(
        db_session,
        {"account_id": str(subscriber.id), "subscriber_id": str(subscriber.id)},
        str(subscription.id),
        str(target_offer.id),
        **_confirmation_kwargs(db_session, subscription, target_offer),
    )

    change_request = db_session.query(SubscriptionChangeRequest).one()
    credit_note = db_session.query(CreditNote).one()
    assert result["credit_note_id"] == str(credit_note.id)
    assert change_request.credit_note_id == credit_note.id
    assert change_request.account_adjustment_id is None
    assert change_request.ledger_entry_id == credit_note.funding_ledger_entry_id
    assert change_request.confirmation_snapshot["ledger_entry_type"] == "credit"
    assert change_request.confirmation_snapshot["ledger_source"] == "credit_note"
    assert change_request.confirmation_snapshot["ledger_amount"] == "50.00"


def test_plan_change_confirmation_idempotently_replays_exact_evidence(
    db_session, subscriber, monkeypatch
):
    _stub_plan_change_side_effects(monkeypatch)
    current_offer = _make_offer(
        db_session,
        name="Replay Basic",
        amount=Decimal("100.00"),
        plan_family="unlimited",
    )
    target_offer = _make_offer(
        db_session,
        name="Replay Plus",
        amount=Decimal("200.00"),
        plan_family="unlimited",
    )
    subscription = _make_subscription(
        db_session,
        subscriber,
        current_offer,
        next_billing_at=datetime(2026, 6, 1, tzinfo=UTC),
        start_at=datetime(2026, 5, 1, tzinfo=UTC),
    )
    _freeze_subscription_now(monkeypatch, datetime(2026, 5, 16, 12, tzinfo=UTC))
    db_session.add(
        LedgerEntry(
            account_id=subscriber.id,
            entry_type=LedgerEntryType.credit,
            source=LedgerSource.payment,
            amount=Decimal("50.00"),
            currency="NGN",
            memo="Replay funding",
        )
    )
    db_session.commit()
    confirmation = _confirmation_kwargs(db_session, subscription, target_offer)

    first = confirm_service_change(
        db_session,
        {"account_id": str(subscriber.id), "subscriber_id": str(subscriber.id)},
        str(subscription.id),
        str(target_offer.id),
        **confirmation,
    )
    second = confirm_service_change(
        db_session,
        {"account_id": str(subscriber.id), "subscriber_id": str(subscriber.id)},
        str(subscription.id),
        str(target_offer.id),
        **confirmation,
    )

    assert second["replayed"] is True
    assert second["change_request_id"] == first["change_request_id"]
    assert second["account_adjustment_id"] == first["account_adjustment_id"]
    assert second["ledger_entry_id"] == first["ledger_entry_id"]
    assert db_session.query(SubscriptionChangeRequest).count() == 1
    assert db_session.query(AccountAdjustment).count() == 1
    assert (
        db_session.query(LedgerEntry)
        .filter(LedgerEntry.entry_type == LedgerEntryType.debit)
        .count()
        == 1
    )


def test_confirm_service_change_emits_single_upgrade_event(
    db_session, subscriber, monkeypatch
):
    from app.services.events.types import EventType

    current_offer = _make_offer(
        db_session,
        name="Unlimited Basic",
        amount=Decimal("100.00"),
        plan_family="unlimited",
    )
    target_offer = _make_offer(
        db_session,
        name="Unlimited Plus",
        amount=Decimal("200.00"),
        plan_family="unlimited",
    )
    subscription = _make_subscription(
        db_session,
        subscriber,
        current_offer,
        next_billing_at=datetime(2026, 6, 1, 0, 0, tzinfo=UTC),
        start_at=datetime(2026, 5, 1, 0, 0, tzinfo=UTC),
    )
    _freeze_subscription_now(monkeypatch, datetime(2026, 5, 16, 12, 0, tzinfo=UTC))
    emitted: list = []
    _stub_plan_change_side_effects(monkeypatch, emitted)

    db_session.add(
        LedgerEntry(
            account_id=subscriber.id,
            entry_type=LedgerEntryType.credit,
            source=LedgerSource.payment,
            amount=Decimal("50.00"),
            currency="NGN",
            memo="Prepaid funding",
        )
    )
    db_session.commit()

    confirm_service_change(
        db_session,
        {
            "account_id": str(subscriber.id),
            "subscriber_id": str(subscriber.id),
        },
        str(subscription.id),
        str(target_offer.id),
        **_confirmation_kwargs(db_session, subscription, target_offer),
    )

    assert emitted.count(EventType.subscription_upgraded) == 1


def test_no_provisioning_before_payment_coverage(db_session, subscriber, monkeypatch):
    current_offer = _make_offer(
        db_session,
        name="Unlimited Basic",
        amount=Decimal("100.00"),
        plan_family="unlimited",
    )
    target_offer = _make_offer(
        db_session,
        name="Unlimited Plus",
        amount=Decimal("200.00"),
        plan_family="unlimited",
    )
    subscription = _make_subscription(
        db_session,
        subscriber,
        current_offer,
        next_billing_at=datetime(2026, 6, 1, 0, 0, tzinfo=UTC),
        start_at=datetime(2026, 5, 1, 0, 0, tzinfo=UTC),
    )
    _freeze_subscription_now(monkeypatch, datetime(2026, 5, 16, 12, 0, tzinfo=UTC))

    import app.services.subscription_changes as change_service

    apply_mock = Mock()
    monkeypatch.setattr(
        change_service.subscription_change_requests, "apply", apply_mock
    )

    result = confirm_service_change(
        db_session,
        {
            "account_id": str(subscriber.id),
            "subscriber_id": str(subscriber.id),
        },
        str(subscription.id),
        str(target_offer.id),
        **_confirmation_kwargs(db_session, subscription, target_offer),
    )

    assert result["success"] is False
    apply_mock.assert_not_called()


def test_change_plan_page_does_not_price_catalog_upfront(
    db_session, subscriber, monkeypatch
):
    """The page must not compute a proration quote per offer (it timed out)."""
    from app.services import customer_portal_flow_changes as flow

    current_offer = _make_offer(
        db_session,
        name="Unlimited Basic",
        amount=Decimal("100.00"),
        plan_family="unlimited",
    )
    _make_offer(
        db_session,
        name="Unlimited Plus",
        amount=Decimal("200.00"),
        plan_family="unlimited",
    )
    subscription = _make_subscription(
        db_session,
        subscriber,
        current_offer,
        next_billing_at=datetime(2026, 6, 1, tzinfo=UTC),
        start_at=datetime(2026, 5, 1, tzinfo=UTC),
    )

    calls: list = []
    monkeypatch.setattr(
        flow, "_build_plan_change_quote", lambda *a, **k: calls.append(1)
    )
    page = flow.get_change_plan_page(
        db_session, {"account_id": str(subscriber.id)}, str(subscription.id)
    )

    assert page is not None
    assert page["available_offer_change_quotes"] == {}
    assert calls == []  # no upfront per-offer quote computation


def test_change_plan_page_surfaces_missing_funding_baseline_without_guessing(
    db_session, subscriber, monkeypatch
):
    from app.services import customer_portal_flow_changes as flow
    from app.services.prepaid_funding_reconstruction import (
        PrepaidFundingBaselineMissingError,
    )

    current_offer = _make_offer(
        db_session,
        name="Baseline Review Plan",
        amount=Decimal("100.00"),
        plan_family="unlimited",
    )
    subscription = _make_subscription(
        db_session,
        subscriber,
        current_offer,
        next_billing_at=datetime(2026, 6, 1, tzinfo=UTC),
        start_at=datetime(2026, 5, 1, tzinfo=UTC),
    )
    monkeypatch.setattr(
        flow,
        "_customer_financial_position",
        Mock(side_effect=PrepaidFundingBaselineMissingError("baseline missing")),
    )

    page = flow.get_change_plan_page(
        db_session,
        {"account_id": str(subscriber.id)},
        str(subscription.id),
    )

    assert page is not None
    assert page["financial_position_unavailable"] is True
    assert page["prepaid_funding"] is None
    assert page["postpaid_receivables"] is None
    assert page["collection_blocking_balance"] is None
    assert page["form_contract"]["submittable"] is False
    assert {item.key for item in page["form_contract"]["unmet_prerequisites"]} == {
        "financial_position_available"
    }


def test_customer_change_quote_maps_missing_funding_baseline_to_conflict(
    db_session, monkeypatch
):
    from app.services.prepaid_funding_reconstruction import (
        PrepaidFundingBaselineMissingError,
    )
    from app.web.customer import routes

    monkeypatch.setattr(
        routes,
        "get_current_customer_from_request",
        lambda _request, _db: {"account_id": str(uuid4())},
    )
    monkeypatch.setattr(
        routes.customer_portal,
        "get_plan_change_quote",
        Mock(side_effect=PrepaidFundingBaselineMissingError("baseline missing")),
    )

    response = routes.customer_change_plan_quote(
        Request({"type": "http", "method": "GET", "path": "/", "headers": []}),
        uuid4(),
        str(uuid4()),
        db=db_session,
    )

    assert response.status_code == 409
    assert b'"error":"financial_position_unavailable"' in response.body


def test_reseller_change_quote_maps_missing_funding_baseline_to_conflict(
    db_session, monkeypatch
):
    from app.services import web_reseller_routes as routes
    from app.services.prepaid_funding_reconstruction import (
        PrepaidFundingBaselineMissingError,
    )

    reseller = Mock(id=uuid4())
    account = Mock(id=uuid4())
    monkeypatch.setattr(
        routes,
        "_require_reseller_context",
        lambda _request, _db: {"reseller": reseller},
    )
    monkeypatch.setattr(
        routes.reseller_portal,
        "owned_account",
        lambda _db, _reseller_id, _account_id: account,
    )
    monkeypatch.setattr(
        routes.customer_portal_flow_changes,
        "get_plan_change_quote",
        Mock(side_effect=PrepaidFundingBaselineMissingError("baseline missing")),
    )

    response = routes.reseller_service_change_quote(
        Request({"type": "http", "method": "GET", "path": "/", "headers": []}),
        db_session,
        str(account.id),
        str(uuid4()),
        str(uuid4()),
        None,
    )

    assert response.status_code == 409
    assert b'"error":"financial_position_unavailable"' in response.body


def test_admin_change_quote_maps_missing_funding_baseline_to_conflict(
    db_session, monkeypatch
):
    from app.services.prepaid_funding_reconstruction import (
        PrepaidFundingBaselineMissingError,
    )
    from app.web.admin import catalog as routes

    monkeypatch.setattr(
        routes.web_catalog_subscription_workflows_service,
        "change_plan_quote_response",
        Mock(side_effect=PrepaidFundingBaselineMissingError("baseline missing")),
    )

    response = routes.subscription_change_plan_quote(
        str(uuid4()),
        str(uuid4()),
        db=db_session,
    )

    assert response.status_code == 409
    assert b'"error_code":"financial_position_unavailable"' in response.body


def test_get_plan_change_quote_returns_single_quote(
    db_session, subscriber, monkeypatch
):
    from app.services import customer_portal_flow_changes as flow

    current_offer = _make_offer(
        db_session,
        name="Unlimited Basic",
        amount=Decimal("100.00"),
        plan_family="unlimited",
    )
    target_offer = _make_offer(
        db_session,
        name="Unlimited Plus",
        amount=Decimal("200.00"),
        plan_family="unlimited",
    )
    subscription = _make_subscription(
        db_session,
        subscriber,
        current_offer,
        next_billing_at=datetime(2026, 6, 1, tzinfo=UTC),
        start_at=datetime(2026, 5, 1, tzinfo=UTC),
    )
    _freeze_subscription_now(monkeypatch, datetime(2026, 5, 16, 12, 0, tzinfo=UTC))

    quote = flow.get_plan_change_quote(
        db_session,
        {"account_id": str(subscriber.id)},
        str(subscription.id),
        str(target_offer.id),
    )

    assert quote is not None and quote != {}
    assert "required_amount" in quote
    assert len(str(quote["preview_fingerprint"])) == 64
    assert quote["ledger_entry_type"] == "debit"
    assert quote["ledger_source"] == "adjustment"
    assert quote["access_consequence"] == "none_plan_change_only"


def test_get_plan_change_quote_enforces_ownership(db_session, subscriber):
    from app.services import customer_portal_flow_changes as flow

    current_offer = _make_offer(
        db_session,
        name="Unlimited Basic",
        amount=Decimal("100.00"),
        plan_family="unlimited",
    )
    target_offer = _make_offer(
        db_session,
        name="Unlimited Plus",
        amount=Decimal("200.00"),
        plan_family="unlimited",
    )
    subscription = _make_subscription(
        db_session,
        subscriber,
        current_offer,
        next_billing_at=datetime(2026, 6, 1, tzinfo=UTC),
        start_at=datetime(2026, 5, 1, tzinfo=UTC),
    )

    # A different account must not get a quote for this subscription.
    quote = flow.get_plan_change_quote(
        db_session,
        {"account_id": str(uuid4())},
        str(subscription.id),
        str(target_offer.id),
    )
    assert quote is None


def test_available_portal_offers_eager_loads_prices(db_session, subscriber):
    """Prices must be eager-loaded so the per-offer price summary is not an N+1."""
    from sqlalchemy import inspect as sa_inspect

    from app.services.customer_portal_context import get_available_portal_offers

    current_offer = _make_offer(
        db_session,
        name="Unlimited Basic",
        amount=Decimal("100.00"),
        plan_family="unlimited",
    )
    _make_offer(
        db_session,
        name="Unlimited Plus",
        amount=Decimal("150.00"),
        plan_family="unlimited",
    )
    subscription = _make_subscription(
        db_session,
        subscriber,
        current_offer,
        next_billing_at=datetime(2026, 6, 1, tzinfo=UTC),
        start_at=datetime(2026, 5, 1, tzinfo=UTC),
    )

    db_session.expire_all()  # force a fresh load so the selectinload is exercised
    offers = get_available_portal_offers(db_session, subscription)

    assert offers
    assert all("prices" not in sa_inspect(o).unloaded for o in offers)


# ---------------------------------------------------------------------------
# Reseller availability scoping
# ---------------------------------------------------------------------------


def _reseller(db_session, name):
    from app.models.subscriber import Reseller

    reseller = Reseller(name=name)
    db_session.add(reseller)
    db_session.commit()
    return reseller


def _restrict_to(db_session, offer, reseller):
    from app.models.offer_availability import OfferResellerAvailability

    db_session.add(
        OfferResellerAvailability(offer_id=offer.id, reseller_id=reseller.id)
    )
    db_session.commit()


def test_reseller_restricted_offer_hidden_from_other_resellers_customer(
    db_session, subscriber
):
    reseller_a = _reseller(db_session, "Partner A")
    reseller_b = _reseller(db_session, "Partner B")
    subscriber.reseller_id = reseller_b.id
    db_session.commit()

    current = _make_offer(
        db_session,
        name="Unlimited Basic",
        amount=Decimal("100"),
        plan_family="unlimited",
    )
    restricted = _make_offer(
        db_session,
        name="Unlimited Partner",
        amount=Decimal("80"),
        plan_family="unlimited",
    )
    _restrict_to(db_session, restricted, reseller_a)
    subscription = _make_subscription(
        db_session,
        subscriber,
        current,
        next_billing_at=datetime(2026, 6, 1, tzinfo=UTC),
        start_at=datetime(2026, 5, 1, tzinfo=UTC),
    )

    offers = get_available_portal_offers(db_session, subscription)

    ids = {str(o.id) for o in offers}
    assert str(restricted.id) not in ids
    assert str(current.id) in ids


def test_reseller_restricted_offer_visible_to_member(db_session, subscriber):
    reseller_a = _reseller(db_session, "Partner A2")
    subscriber.reseller_id = reseller_a.id
    db_session.commit()

    current = _make_offer(
        db_session,
        name="Unlimited Basic2",
        amount=Decimal("100"),
        plan_family="unlimited",
    )
    restricted = _make_offer(
        db_session,
        name="Unlimited Partner2",
        amount=Decimal("80"),
        plan_family="unlimited",
    )
    _restrict_to(db_session, restricted, reseller_a)
    subscription = _make_subscription(
        db_session,
        subscriber,
        current,
        next_billing_at=datetime(2026, 6, 1, tzinfo=UTC),
        start_at=datetime(2026, 5, 1, tzinfo=UTC),
    )

    offers = get_available_portal_offers(db_session, subscription)

    assert str(restricted.id) in {str(o.id) for o in offers}


def test_restricted_offer_hidden_without_subscriber_context(db_session):
    reseller_a = _reseller(db_session, "Partner A3")
    restricted = _make_offer(
        db_session,
        name="Unlimited Partner3",
        amount=Decimal("80"),
        plan_family="unlimited",
    )
    _restrict_to(db_session, restricted, reseller_a)
    _make_offer(
        db_session,
        name="Unlimited Open3",
        amount=Decimal("90"),
        plan_family="unlimited",
    )

    offers = get_available_portal_offers(db_session)

    names = {o.name for o in offers}
    assert "Unlimited Partner3" not in names
    assert "Unlimited Open3" in names


def test_restrict_mode_reseller_sees_only_assigned_offers(db_session, subscriber):
    # C-2: a reseller flagged restrict_to_assigned_offers sees ONLY offers
    # assigned to it — unrestricted offers are hidden too.
    reseller = _reseller(db_session, "Restricted Partner")
    reseller.restrict_to_assigned_offers = True
    subscriber.reseller_id = reseller.id
    db_session.commit()

    assigned = _make_offer(
        db_session,
        name="Assigned Only",
        amount=Decimal("100"),
        plan_family="unlimited",
    )
    _make_offer(
        db_session,
        name="Open Unrestricted",
        amount=Decimal("90"),
        plan_family="unlimited",
    )
    _restrict_to(db_session, assigned, reseller)

    offers = get_available_portal_offers(db_session, subscriber_id=subscriber.id)

    names = {o.name for o in offers}
    assert names == {"Assigned Only"}


def test_non_restrict_reseller_still_sees_unrestricted_offers(db_session, subscriber):
    # Flag explicitly False (open) behaves exactly like today.
    reseller = _reseller(db_session, "Open Partner")
    reseller.restrict_to_assigned_offers = False
    subscriber.reseller_id = reseller.id
    db_session.commit()

    assigned = _make_offer(
        db_session,
        name="Assigned Open Partner",
        amount=Decimal("100"),
        plan_family="unlimited",
    )
    _make_offer(
        db_session,
        name="Unrestricted Open Partner",
        amount=Decimal("90"),
        plan_family="unlimited",
    )
    _restrict_to(db_session, assigned, reseller)

    offers = get_available_portal_offers(db_session, subscriber_id=subscriber.id)

    names = {o.name for o in offers}
    assert "Assigned Open Partner" in names
    assert "Unrestricted Open Partner" in names


def test_global_default_flip_flows_through_when_flag_null(
    db_session, subscriber, monkeypatch
):
    # Per-reseller flag NULL (inherit): flipping the global default to
    # "not open" puts the reseller into restrict mode.
    import app.services.customer_portal_context as cpc

    reseller = _reseller(db_session, "Inherit Partner")
    assert reseller.restrict_to_assigned_offers is None
    subscriber.reseller_id = reseller.id
    db_session.commit()

    assigned = _make_offer(
        db_session,
        name="Assigned Inherit",
        amount=Decimal("100"),
        plan_family="unlimited",
    )
    _make_offer(
        db_session,
        name="Unrestricted Inherit",
        amount=Decimal("90"),
        plan_family="unlimited",
    )
    _restrict_to(db_session, assigned, reseller)

    # Global default open (today's behavior) → unrestricted offer visible.
    monkeypatch.setattr(cpc, "_reseller_default_catalog_open", lambda db: True)
    open_names = {
        o.name
        for o in get_available_portal_offers(db_session, subscriber_id=subscriber.id)
    }
    assert "Unrestricted Inherit" in open_names
    assert "Assigned Inherit" in open_names

    # Flip global default to restrict → only assigned offer visible.
    monkeypatch.setattr(cpc, "_reseller_default_catalog_open", lambda db: False)
    restricted_names = {
        o.name
        for o in get_available_portal_offers(db_session, subscriber_id=subscriber.id)
    }
    assert restricted_names == {"Assigned Inherit"}


def test_archived_status_offer_hidden_even_when_is_active_drifted(
    db_session, subscriber
):
    """status=archived must hide an offer from the portal even if is_active
    drifted to True (the edit form historically set only status)."""
    current_offer = _make_offer(
        db_session,
        name="Unlimited Basic",
        amount=Decimal("100.00"),
        plan_family="unlimited",
    )
    _make_offer(
        db_session,
        name="Unlimited Retired",
        amount=Decimal("150.00"),
        plan_family="unlimited",
        status=OfferStatus.archived,
        is_active=True,
    )
    subscription = _make_subscription(
        db_session,
        subscriber,
        current_offer,
        next_billing_at=datetime(2026, 6, 1, tzinfo=UTC),
        start_at=datetime(2026, 5, 1, tzinfo=UTC),
    )

    offers = get_available_portal_offers(db_session, subscription)

    assert {str(offer.id) for offer in offers} == {str(current_offer.id)}


def test_submit_change_plan_accepts_cross_family_when_catalog_compatible(
    db_session, subscriber, monkeypatch
):
    from app.services import customer_portal_flow_changes as flow
    from app.services import subscription_changes as change_service

    current_offer = _make_offer(
        db_session,
        name="Unlimited Basic",
        amount=Decimal("100.00"),
        plan_family="unlimited",
    )
    cross_family_offer = _make_offer(
        db_session,
        name="Dedicated 1",
        amount=Decimal("300.00"),
        plan_family="dedicated",
    )
    subscription = _make_subscription(
        db_session,
        subscriber,
        current_offer,
        next_billing_at=datetime(2026, 7, 1, tzinfo=UTC),
        start_at=datetime(2026, 6, 1, tzinfo=UTC),
    )
    created = []
    monkeypatch.setattr(
        change_service.subscription_change_requests,
        "create",
        lambda **kwargs: created.append(kwargs),
    )
    customer = {"account_id": str(subscriber.id), "subscriber_id": str(subscriber.id)}

    result = flow.submit_change_plan(
        db_session,
        customer,
        str(subscription.id),
        str(cross_family_offer.id),
        "2099-01-01",
    )

    assert result == {"success": True}
    assert created[0]["new_offer_id"] == str(cross_family_offer.id)


def test_submit_change_plan_accepts_compatible_offer(
    db_session, subscriber, monkeypatch
):
    from app.services import customer_portal_flow_changes as flow
    from app.services import subscription_changes as change_service

    current_offer = _make_offer(
        db_session,
        name="Unlimited Basic",
        amount=Decimal("100.00"),
        plan_family="unlimited",
    )
    target_offer = _make_offer(
        db_session,
        name="Unlimited Plus",
        amount=Decimal("150.00"),
        plan_family="unlimited",
    )
    subscription = _make_subscription(
        db_session,
        subscriber,
        current_offer,
        next_billing_at=datetime(2026, 7, 1, tzinfo=UTC),
        start_at=datetime(2026, 6, 1, tzinfo=UTC),
    )
    created = []
    monkeypatch.setattr(
        change_service.subscription_change_requests,
        "create",
        lambda **kwargs: created.append(kwargs),
    )
    customer = {"account_id": str(subscriber.id), "subscriber_id": str(subscriber.id)}

    result = flow.submit_change_plan(
        db_session,
        customer,
        str(subscription.id),
        str(target_offer.id),
        "2099-01-01",
    )
    assert result == {"success": True}
    assert len(created) == 1
    assert created[0]["new_offer_id"] == str(target_offer.id)


def test_confirm_service_change_rejects_archived_offer(
    db_session, subscriber, monkeypatch
):
    """The instant web path must reject an archived-but-is_active offer — it
    now gates through get_available_portal_offers (status==active), like the
    deferred path, instead of only checking is_active."""
    _stub_plan_change_side_effects(monkeypatch)
    now = datetime(2026, 3, 15, tzinfo=UTC)
    _freeze_subscription_now(monkeypatch, now)

    current_offer = _make_offer(
        db_session,
        name="Unlimited Basic",
        amount=Decimal("100.00"),
        plan_family="unlimited",
    )
    # Archived target: still is_active=True, same family/service/billing, but
    # status=archived → must be refused.
    archived = _make_offer(
        db_session,
        name="Unlimited Legacy",
        amount=Decimal("50.00"),
        plan_family="unlimited",
        is_active=True,
        status=OfferStatus.archived,
    )
    subscription = _make_subscription(
        db_session,
        subscriber,
        current_offer,
        next_billing_at=now + timedelta(days=20),
        start_at=now - timedelta(days=10),
    )

    with pytest.raises(ValueError, match="not available for self-service change"):
        confirm_service_change(
            db_session,
            {"account_id": str(subscriber.id), "subscriber_id": str(subscriber.id)},
            str(subscription.id),
            str(archived.id),
            preview_fingerprint="x" * 64,
            idempotency_key=f"test-plan-{uuid4()}",
            confirmation_origin="test",
        )

    db_session.refresh(subscription)
    assert subscription.offer_id == current_offer.id  # unchanged


def test_get_available_portal_offers_allows_unclassified_family(db_session, subscriber):
    current_offer = _make_offer(
        db_session, name="Unclassified A", amount=Decimal("100.00"), plan_family=None
    )
    target_offer = _make_offer(
        db_session, name="Unclassified B", amount=Decimal("150.00"), plan_family=None
    )
    subscription = _make_subscription(
        db_session,
        subscriber,
        current_offer,
        next_billing_at=datetime(2026, 6, 1, tzinfo=UTC),
        start_at=datetime(2026, 5, 1, tzinfo=UTC),
    )

    offers = get_available_portal_offers(db_session, subscription)

    assert {str(offer.id) for offer in offers} == {
        str(current_offer.id),
        str(target_offer.id),
    }


def test_plan_change_refreshes_unit_price(db_session, subscriber, monkeypatch):
    """Changing the offer refreshes subscription.unit_price to the new offer's
    recurring price (#10), so billing summaries don't keep showing the old
    plan's price after a change."""
    from app.schemas.catalog import SubscriptionUpdate
    from app.services import catalog as catalog_service

    _stub_plan_change_side_effects(monkeypatch)

    current_offer = _make_offer(
        db_session,
        name="Unlimited 100",
        amount=Decimal("100.00"),
        plan_family="unlimited",
    )
    target_offer = _make_offer(
        db_session,
        name="Unlimited 200",
        amount=Decimal("200.00"),
        plan_family="unlimited",
    )
    subscription = _make_subscription(
        db_session,
        subscriber,
        current_offer,
        next_billing_at=datetime(2026, 6, 1, tzinfo=UTC),
        start_at=datetime(2026, 5, 1, tzinfo=UTC),
    )

    catalog_service.subscriptions.update(
        db_session,
        str(subscription.id),
        SubscriptionUpdate(offer_id=target_offer.id),
        skip_proration_artifacts=True,
    )
    db_session.refresh(subscription)
    assert subscription.unit_price == Decimal("200.00")


def test_admin_prepaid_upgrade_rejects_insufficient_balance(
    db_session, subscriber, monkeypatch
):
    from app.schemas.catalog import SubscriptionUpdate
    from app.services import catalog as catalog_service

    current_offer = _make_offer(
        db_session,
        name="Admin Prepaid 100",
        amount=Decimal("100.00"),
        plan_family="unlimited",
    )
    target_offer = _make_offer(
        db_session,
        name="Admin Prepaid 200",
        amount=Decimal("200.00"),
        plan_family="unlimited",
    )
    subscription = _make_subscription(
        db_session,
        subscriber,
        current_offer,
        next_billing_at=datetime(2026, 6, 1, 0, 0, tzinfo=UTC),
        start_at=datetime(2026, 5, 1, 0, 0, tzinfo=UTC),
    )
    _freeze_subscription_now(monkeypatch, datetime(2026, 5, 16, 12, 0, tzinfo=UTC))

    with pytest.raises(HTTPException) as exc:
        catalog_service.subscriptions.update(
            db_session,
            str(subscription.id),
            SubscriptionUpdate(offer_id=target_offer.id),
            plan_change_preview_fingerprint=_confirmation_kwargs(
                db_session, subscription, target_offer
            )["preview_fingerprint"],
        )

    db_session.refresh(subscription)
    assert exc.value.status_code == 400
    assert exc.value.detail["code"] == "insufficient_prepaid_funding"
    assert exc.value.detail["required_amount"] == "50.00"
    assert exc.value.detail["prepaid_funding_before"] == "0.00"
    assert subscription.offer_id == current_offer.id
    assert (
        db_session.query(LedgerEntry)
        .filter(LedgerEntry.account_id == subscriber.id)
        .filter(LedgerEntry.entry_type == LedgerEntryType.debit)
        .count()
        == 0
    )


def test_immediate_prepaid_change_rejects_cross_currency_catalog(
    db_session, subscriber, monkeypatch
):
    from app.schemas.catalog import SubscriptionUpdate
    from app.services import catalog as catalog_service

    current_offer = _make_offer(
        db_session,
        name="NGN Plan",
        amount=Decimal("100.00"),
        plan_family="unlimited",
        currency="NGN",
    )
    target_offer = _make_offer(
        db_session,
        name="USD Plan",
        amount=Decimal("200.00"),
        plan_family="unlimited",
        currency="USD",
    )
    subscription = _make_subscription(
        db_session,
        subscriber,
        current_offer,
        next_billing_at=datetime(2026, 6, 1, tzinfo=UTC),
        start_at=datetime(2026, 5, 1, tzinfo=UTC),
    )
    _freeze_subscription_now(monkeypatch, datetime(2026, 5, 16, 12, tzinfo=UTC))
    db_session.add(
        LedgerEntry(
            account_id=subscriber.id,
            entry_type=LedgerEntryType.credit,
            source=LedgerSource.payment,
            amount=Decimal("500.00"),
            currency="NGN",
            memo="Prepaid funding",
        )
    )
    db_session.commit()

    with pytest.raises(HTTPException) as exc:
        catalog_service.subscriptions.update(
            db_session,
            str(subscription.id),
            SubscriptionUpdate(offer_id=target_offer.id),
            plan_change_preview_fingerprint=_confirmation_kwargs(
                db_session, subscription, target_offer
            )["preview_fingerprint"],
        )

    assert exc.value.detail["code"] == "catalog_currency_mismatch"


def test_admin_prepaid_upgrade_with_funding_writes_evidenced_debit(
    db_session, subscriber, monkeypatch
):
    from app.schemas.catalog import SubscriptionUpdate
    from app.services import catalog as catalog_service

    _stub_plan_change_side_effects(monkeypatch)
    current_offer = _make_offer(
        db_session,
        name="Admin Covered 100",
        amount=Decimal("100.00"),
        plan_family="unlimited",
    )
    target_offer = _make_offer(
        db_session,
        name="Admin Covered 200",
        amount=Decimal("200.00"),
        plan_family="unlimited",
    )
    subscription = _make_subscription(
        db_session,
        subscriber,
        current_offer,
        next_billing_at=datetime(2026, 6, 1, 0, 0, tzinfo=UTC),
        start_at=datetime(2026, 5, 1, 0, 0, tzinfo=UTC),
    )
    _freeze_subscription_now(monkeypatch, datetime(2026, 5, 16, 12, 0, tzinfo=UTC))
    db_session.add(
        LedgerEntry(
            account_id=subscriber.id,
            entry_type=LedgerEntryType.credit,
            source=LedgerSource.payment,
            amount=Decimal("50.00"),
            currency="NGN",
            memo="Prepaid funding",
        )
    )
    db_session.commit()

    catalog_service.subscriptions.update(
        db_session,
        str(subscription.id),
        SubscriptionUpdate(offer_id=target_offer.id),
        plan_change_preview_fingerprint=_confirmation_kwargs(
            db_session, subscription, target_offer
        )["preview_fingerprint"],
    )

    db_session.refresh(subscription)
    debits = (
        db_session.query(LedgerEntry)
        .filter(LedgerEntry.account_id == subscriber.id)
        .filter(LedgerEntry.entry_type == LedgerEntryType.debit)
        .all()
    )
    assert subscription.offer_id == target_offer.id
    assert len(debits) == 1
    assert debits[0].amount == Decimal("50.00")
    adjustment = db_session.query(AccountAdjustment).one()
    assert adjustment.ledger_entry_id == debits[0].id
    assert adjustment.prepaid_funding_before == Decimal("50.00")
    assert adjustment.prepaid_funding_after == Decimal("0.00")


def test_prepaid_plan_change_adjustment_is_idempotent_within_transaction(
    db_session, subscriber, monkeypatch
):
    from app.services.prepaid_plan_changes import (
        prepare_immediate_prepaid_plan_change,
    )

    current_offer = _make_offer(
        db_session,
        name="Idempotent 100",
        amount=Decimal("100.00"),
        plan_family="unlimited",
    )
    target_offer = _make_offer(
        db_session,
        name="Idempotent 200",
        amount=Decimal("200.00"),
        plan_family="unlimited",
    )
    subscription = _make_subscription(
        db_session,
        subscriber,
        current_offer,
        next_billing_at=datetime(2026, 6, 1, 0, 0, tzinfo=UTC),
        start_at=datetime(2026, 5, 1, 0, 0, tzinfo=UTC),
    )
    _freeze_subscription_now(monkeypatch, datetime(2026, 5, 16, 12, 0, tzinfo=UTC))
    db_session.add(
        LedgerEntry(
            account_id=subscriber.id,
            entry_type=LedgerEntryType.credit,
            source=LedgerSource.payment,
            amount=Decimal("50.00"),
            currency="NGN",
            memo="Prepaid funding",
        )
    )
    db_session.commit()

    preview_fingerprint = _confirmation_kwargs(db_session, subscription, target_offer)[
        "preview_fingerprint"
    ]

    first = prepare_immediate_prepaid_plan_change(
        db_session,
        subscription,
        target_offer,
        old_offer_name=current_offer.name,
        operation_key="same-request",
        expected_preview_fingerprint=preview_fingerprint,
    )
    second = prepare_immediate_prepaid_plan_change(
        db_session,
        subscription,
        target_offer,
        old_offer_name=current_offer.name,
        operation_key="same-request",
        expected_preview_fingerprint=preview_fingerprint,
    )

    assert first.ledger_entry is not None
    assert second.replayed is True
    assert second.ledger_entry.id == first.ledger_entry.id
    assert db_session.query(AccountAdjustment).count() == 1
    assert (
        db_session.query(LedgerEntry)
        .filter(LedgerEntry.account_id == subscriber.id)
        .filter(LedgerEntry.entry_type == LedgerEntryType.debit)
        .count()
        == 1
    )


def _add_overdue_invoice(db_session, subscriber, amount: Decimal) -> None:
    from app.models.billing import Invoice, InvoiceStatus

    db_session.add(
        Invoice(
            account_id=subscriber.id,
            status=InvoiceStatus.overdue,
            currency="NGN",
            subtotal=amount,
            total=amount,
            balance_due=amount,
            is_active=True,
        )
    )
    db_session.commit()


def test_admin_prepaid_change_rejects_overdue_debt_even_with_funding(
    db_session, subscriber, monkeypatch
):
    from app.schemas.catalog import SubscriptionUpdate
    from app.services import catalog as catalog_service

    current_offer = _make_offer(
        db_session,
        name="Debt Guard 100",
        amount=Decimal("100.00"),
        plan_family="unlimited",
    )
    target_offer = _make_offer(
        db_session,
        name="Debt Guard 200",
        amount=Decimal("200.00"),
        plan_family="unlimited",
    )
    subscription = _make_subscription(
        db_session,
        subscriber,
        current_offer,
        next_billing_at=datetime(2026, 6, 1, tzinfo=UTC),
        start_at=datetime(2026, 5, 1, tzinfo=UTC),
    )
    _freeze_subscription_now(monkeypatch, datetime(2026, 5, 16, 12, tzinfo=UTC))
    db_session.add(
        LedgerEntry(
            account_id=subscriber.id,
            entry_type=LedgerEntryType.credit,
            source=LedgerSource.payment,
            amount=Decimal("500.00"),
            currency="NGN",
            memo="Prepaid funding",
        )
    )
    db_session.commit()
    _add_overdue_invoice(db_session, subscriber, Decimal("25.00"))

    with pytest.raises(HTTPException) as exc:
        catalog_service.subscriptions.update(
            db_session,
            str(subscription.id),
            SubscriptionUpdate(offer_id=target_offer.id),
            plan_change_preview_fingerprint=_confirmation_kwargs(
                db_session, subscription, target_offer
            )["preview_fingerprint"],
        )

    db_session.refresh(subscription)
    assert exc.value.detail["code"] == "collection_blocking_balance"
    assert subscription.offer_id == current_offer.id
    assert (
        db_session.query(LedgerEntry)
        .filter(LedgerEntry.account_id == subscriber.id)
        .filter(LedgerEntry.entry_type == LedgerEntryType.debit)
        .count()
        == 0
    )


def test_plan_change_blocked_when_account_in_arrears(
    db_session, subscriber, monkeypatch
):
    """An account with an overdue balance cannot self-service change plans
    (policy: block-until-settled). Covers POSTPAID too — the old gate only
    looked at prepaid funding and never considered debt (account
    100000016 could upgrade while owing)."""
    _stub_plan_change_side_effects(monkeypatch)
    current_offer = _make_offer(
        db_session,
        name="Unlimited Basic",
        amount=Decimal("100.00"),
        plan_family="unlimited",
        billing_mode=BillingMode.postpaid,
    )
    target_offer = _make_offer(
        db_session,
        name="Unlimited Plus",
        amount=Decimal("200.00"),
        plan_family="unlimited",
        billing_mode=BillingMode.postpaid,
    )
    subscription = _make_subscription(
        db_session,
        subscriber,
        current_offer,
        next_billing_at=datetime(2026, 7, 1, tzinfo=UTC),
        start_at=datetime(2026, 6, 1, tzinfo=UTC),
    )
    _add_overdue_invoice(db_session, subscriber, Decimal("5000.00"))

    with pytest.raises(ValueError, match="overdue balance"):
        confirm_service_change(
            db_session,
            {"account_id": str(subscriber.id), "subscriber_id": str(subscriber.id)},
            str(subscription.id),
            str(target_offer.id),
            preview_fingerprint="x" * 64,
            idempotency_key=f"test-plan-{uuid4()}",
            confirmation_origin="test",
        )

    db_session.refresh(subscription)
    assert subscription.offer_id == current_offer.id  # unchanged


def test_postpaid_plan_change_applies_when_no_arrears(
    db_session, subscriber, monkeypatch
):
    """With no overdue balance, a postpaid change still auto-applies."""
    _stub_plan_change_side_effects(monkeypatch)
    subscriber.billing_mode = BillingMode.postpaid
    db_session.commit()
    current_offer = _make_offer(
        db_session,
        name="Unlimited Basic",
        amount=Decimal("100.00"),
        plan_family="unlimited",
        billing_mode=BillingMode.postpaid,
    )
    target_offer = _make_offer(
        db_session,
        name="Unlimited Plus",
        amount=Decimal("200.00"),
        plan_family="unlimited",
        billing_mode=BillingMode.postpaid,
    )
    subscription = _make_subscription(
        db_session,
        subscriber,
        current_offer,
        next_billing_at=datetime(2026, 7, 1, tzinfo=UTC),
        start_at=datetime(2026, 6, 1, tzinfo=UTC),
    )

    result = confirm_service_change(
        db_session,
        {"account_id": str(subscriber.id), "subscriber_id": str(subscriber.id)},
        str(subscription.id),
        str(target_offer.id),
        **_confirmation_kwargs(db_session, subscription, target_offer),
    )
    db_session.refresh(subscription)
    assert result["success"] is True
    assert subscription.offer_id == target_offer.id
    change_request = db_session.query(SubscriptionChangeRequest).one()
    assert change_request.confirmation_preview_fingerprint
    assert change_request.confirmation_snapshot["billing_mode"] == "postpaid"
    assert change_request.confirmation_snapshot["ledger_entry_type"] is None
    assert change_request.account_adjustment_id is None
    assert change_request.credit_note_id is None
    assert change_request.ledger_entry_id is None


def test_change_plan_page_flags_arrears(db_session, subscriber):
    """The change-plan page context surfaces arrears so the UI can show a
    'settle first' notice and hide the plan picker (#30 follow-up)."""
    from app.services.customer_portal_flow_changes import get_change_plan_page

    current_offer = _make_offer(
        db_session,
        name="Unlimited Basic",
        amount=Decimal("100.00"),
        plan_family="unlimited",
    )
    subscription = _make_subscription(
        db_session,
        subscriber,
        current_offer,
        next_billing_at=datetime(2026, 7, 1, tzinfo=UTC),
        start_at=datetime(2026, 6, 1, tzinfo=UTC),
    )
    cust = {"account_id": str(subscriber.id), "subscriber_id": str(subscriber.id)}

    page = get_change_plan_page(db_session, cust, str(subscription.id))
    assert page["in_arrears"] is False
    assert page["arrears_amount"] == 0.0

    _add_overdue_invoice(db_session, subscriber, Decimal("5000.00"))
    page = get_change_plan_page(db_session, cust, str(subscription.id))
    assert page["in_arrears"] is True
    assert page["arrears_amount"] == 5000.0


def test_admin_change_plan_quote_response(db_session, subscriber):
    from app.services.web_catalog_subscription_workflows import (
        change_plan_quote_response,
    )

    now = datetime.now(UTC)
    source = _make_offer(
        db_session, name="Quote Src", amount=Decimal("20000.00"), plan_family="u"
    )
    target = _make_offer(
        db_session, name="Quote Tgt", amount=Decimal("35000.00"), plan_family="u"
    )
    sub = _make_subscription(
        db_session,
        subscriber,
        source,
        start_at=now - timedelta(days=10),
        next_billing_at=now + timedelta(days=20),
    )

    payload = change_plan_quote_response(
        db_session, subscription_id=str(sub.id), target_offer_id=str(target.id)
    )

    quote = payload["quote"]
    assert payload["target_offer_name"] == "Quote Tgt"
    assert quote["days_in_cycle"] > 0
    assert quote["days_remaining"] > 0
    assert quote["charge_amount"] > 0
    # Upgrading mid-cycle must cost something net.
    assert quote["net_amount"] > 0
    assert len(quote["preview_fingerprint"]) == 64
    assert quote["ledger_entry_type"] == "debit"
    assert quote["ledger_source"] == "adjustment"
    assert quote["ledger_amount"] == quote["net_amount"]


def test_admin_change_plan_quote_unknown_offer_404(db_session, subscriber):
    from app.services.web_catalog_subscription_workflows import (
        change_plan_quote_response,
    )

    now = datetime.now(UTC)
    source = _make_offer(
        db_session, name="Quote Only", amount=Decimal("20000.00"), plan_family="u"
    )
    sub = _make_subscription(
        db_session,
        subscriber,
        source,
        start_at=now - timedelta(days=1),
        next_billing_at=now + timedelta(days=29),
    )

    with pytest.raises(HTTPException) as exc:
        change_plan_quote_response(
            db_session, subscription_id=str(sub.id), target_offer_id=str(uuid4())
        )
    assert exc.value.status_code == 404


def test_execution_reconciliation_inspection_has_reviewed_head(db_session, subscriber):
    from app.models.subscription_change import (
        SubscriptionChangeExecutionState,
        SubscriptionChangeRequest,
        SubscriptionChangeStatus,
    )
    from app.services.subscription_change_execution import (
        inspect_execution_chain_reconciliation,
    )

    current = _make_offer(
        db_session,
        name="Reconcile Current",
        amount=Decimal("100.00"),
        plan_family="reconciliation",
    )
    target = _make_offer(
        db_session,
        name="Reconcile Target",
        amount=Decimal("150.00"),
        plan_family="reconciliation",
    )
    subscription = _make_subscription(
        db_session,
        subscriber,
        current,
        start_at=datetime.now(UTC) - timedelta(days=1),
        next_billing_at=datetime.now(UTC) + timedelta(days=29),
    )
    change = SubscriptionChangeRequest(
        subscription_id=subscription.id,
        current_offer_id=current.id,
        requested_offer_id=target.id,
        effective_date=datetime.now(UTC).date(),
        status=SubscriptionChangeStatus.applied,
        execution_state=SubscriptionChangeExecutionState.completed,
        is_active=True,
    )
    db_session.add(change)
    db_session.commit()

    inspection = inspect_execution_chain_reconciliation(db_session)

    item = next(value for value in inspection.items if value.request_id == change.id)
    assert len(item.reviewed_head) == 64
    assert [finding.code for finding in item.findings] == [
        "completed_subscription_drift"
    ]
    assert item.findings[0].repairable is False


def test_execution_reconciliation_replays_durable_operator_evidence(
    db_session, subscriber
):
    import hashlib

    from app.models.subscription_change import (
        SubscriptionChangeExecutionState,
        SubscriptionChangeRequest,
        SubscriptionChangeStatus,
    )
    from app.services.subscription_change_execution import reconcile_execution_chain

    current = _make_offer(
        db_session,
        name="Replay Current",
        amount=Decimal("100.00"),
        plan_family="reconciliation",
    )
    target = _make_offer(
        db_session,
        name="Replay Target",
        amount=Decimal("150.00"),
        plan_family="reconciliation",
    )
    subscription = _make_subscription(
        db_session,
        subscriber,
        current,
        start_at=datetime.now(UTC) - timedelta(days=1),
        next_billing_at=datetime.now(UTC) + timedelta(days=29),
    )
    key = "service-change-repair-replay-key"
    head = "a" * 64
    change = SubscriptionChangeRequest(
        subscription_id=subscription.id,
        current_offer_id=current.id,
        requested_offer_id=target.id,
        effective_date=datetime.now(UTC).date(),
        status=SubscriptionChangeStatus.applied,
        execution_state=SubscriptionChangeExecutionState.completed,
        reconciliation_idempotency_key_hash=hashlib.sha256(key.encode()).hexdigest(),
        reconciliation_reviewed_head=head,
        reconciliation_actor_id="operator-1",
        reconciliation_reason="Reviewed canonical execution evidence",
        reconciled_at=datetime.now(UTC),
        is_active=True,
    )
    db_session.add(change)
    db_session.commit()

    outcome = reconcile_execution_chain(
        db_session,
        request_id=change.id,
        expected_head=head,
        idempotency_key=key,
        actor_id="operator-1",
        reason="Reviewed canonical execution evidence",
    )

    assert outcome.replayed is True
    assert outcome.request_id == change.id


def test_execution_reconciliation_repairs_settled_before_fulfillment(
    db_session, subscriber
):
    from app.models.billing import (
        Invoice,
        InvoiceStatus,
        Payment,
        PaymentAllocation,
        PaymentStatus,
    )
    from app.models.subscription_change import (
        SubscriptionChangeExecutionState,
        SubscriptionChangeRequest,
        SubscriptionChangeStatus,
    )
    from app.services.subscription_change_execution import (
        inspect_execution_chain_reconciliation,
        reconcile_execution_chain,
    )

    current = _make_offer(
        db_session,
        name="Interrupted Current",
        amount=Decimal("100.00"),
        plan_family="reconciliation",
    )
    target = _make_offer(
        db_session,
        name="Interrupted Target",
        amount=Decimal("150.00"),
        plan_family="reconciliation",
    )
    subscription = _make_subscription(
        db_session,
        subscriber,
        current,
        start_at=datetime.now(UTC) - timedelta(days=1),
        next_billing_at=datetime.now(UTC) + timedelta(days=29),
    )
    invoice = Invoice(
        account_id=subscriber.id,
        status=InvoiceStatus.paid,
        currency="NGN",
        subtotal=Decimal("25000.00"),
        total=Decimal("25000.00"),
        balance_due=Decimal("0.00"),
    )
    payment = Payment(
        account_id=subscriber.id,
        amount=Decimal("25000.00"),
        currency="NGN",
        status=PaymentStatus.succeeded,
        paid_at=datetime.now(UTC),
        is_active=True,
    )
    db_session.add_all([invoice, payment])
    db_session.flush()
    db_session.add(
        PaymentAllocation(
            payment_id=payment.id,
            invoice_id=invoice.id,
            amount=Decimal("25000.00"),
            is_active=True,
        )
    )
    change = SubscriptionChangeRequest(
        subscription_id=subscription.id,
        current_offer_id=current.id,
        requested_offer_id=target.id,
        effective_date=datetime.now(UTC).date(),
        status=SubscriptionChangeStatus.pending,
        execution_state=SubscriptionChangeExecutionState.payment_settled,
        field_fee_amount=Decimal("25000.00"),
        field_fee_currency="NGN",
        field_fee_invoice_id=invoice.id,
        field_fee_payment_id=payment.id,
        confirmation_snapshot={"delivery_mode": "field_migration"},
        is_active=True,
    )
    db_session.add(change)
    db_session.commit()
    inspection = inspect_execution_chain_reconciliation(db_session)
    item = next(value for value in inspection.items if value.request_id == change.id)
    assert [(finding.code, finding.repairable) for finding in item.findings] == [
        ("settled_not_released", True)
    ]

    outcome = reconcile_execution_chain(
        db_session,
        request_id=change.id,
        expected_head=item.reviewed_head,
        idempotency_key="repair-settled-before-fulfillment",
        actor_id="operator-1",
        reason="Payment settled before worker interruption",
    )

    db_session.refresh(change)
    assert outcome.replayed is False
    assert change.service_order_id is not None
    assert change.work_order_id is not None
    assert change.execution_state == (
        SubscriptionChangeExecutionState.fulfillment_released
    )
    assert change.reconciliation_reviewed_head == item.reviewed_head
