from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from app.models.billing import Invoice, InvoiceLine, InvoiceStatus
from app.models.catalog import (
    AccessCredential,
    BillingMode,
    CatalogOffer,
    OfferRadiusProfile,
    RadiusProfile,
    Subscription,
    SubscriptionStatus,
)
from app.models.enforcement_lock import EnforcementLock, EnforcementReason
from app.models.fup_state import FupActionStatus, FupState
from app.services.owner_commands import CommandContext
from app.services.subscription_correction import (
    CorrectSubscriptionCommand,
    SubscriptionCorrectionError,
    correct_subscription,
    list_correction_candidates,
    preview_subscription_correction,
)
from app.services.web_catalog_subscription_workflows import (
    _subscription_correction_action_forms,
)


def _fixture(db_session, subscriber, catalog_offer):
    subscriber.billing_enabled = True
    wrong_offer = catalog_offer
    target_offer = CatalogOffer(
        name="Unlimited Lite",
        code=f"LITE-{uuid4().hex[:8]}",
        service_type=wrong_offer.service_type,
        access_type=wrong_offer.access_type,
        price_basis=wrong_offer.price_basis,
        billing_mode=BillingMode.postpaid,
    )
    profile = RadiusProfile(
        name="Unlimited Lite 15 Mbps",
        code=f"LITE-15-{uuid4().hex[:8]}",
        download_speed=15000,
        upload_speed=15000,
        is_active=True,
    )
    db_session.add_all([target_offer, profile])
    db_session.flush()
    db_session.add(OfferRadiusProfile(offer_id=target_offer.id, profile_id=profile.id))
    target = Subscription(
        subscriber_id=subscriber.id,
        offer_id=target_offer.id,
        status=SubscriptionStatus.stopped,
        billing_mode=BillingMode.postpaid,
        login="correction-test-login",
    )
    db_session.add(target)
    db_session.flush()
    wrong = Subscription(
        subscriber_id=subscriber.id,
        offer_id=wrong_offer.id,
        status=SubscriptionStatus.active,
        billing_mode=BillingMode.postpaid,
        login="correction-test-login",
    )
    db_session.add(wrong)
    db_session.flush()
    credential = AccessCredential(
        subscriber_id=subscriber.id,
        subscription_id=wrong.id,
        username="correction-test-login",
        secret_hash="{noop}test-only",
        radius_profile_id=None,
        is_active=True,
    )
    wrong_fup = FupState(
        subscription_id=wrong.id,
        offer_id=wrong.offer_id,
        action_status=FupActionStatus.throttled,
        throttle_profile_id=profile.id,
        last_evaluated_at=datetime(2026, 7, 31, tzinfo=UTC),
    )
    db_session.add_all([credential, wrong_fup])
    db_session.commit()
    return wrong, target, credential, profile


def _command(
    wrong_id, target_id, fingerprint, *, key: str
) -> CorrectSubscriptionCommand:
    command_id = uuid4()
    return CorrectSubscriptionCommand(
        context=CommandContext(
            command_id=command_id,
            correlation_id=command_id,
            actor="admin:test-operator",
            scope="catalog:write",
            reason="Correct mistaken active subscription",
            idempotency_key=key,
        ),
        active_subscription_id=wrong_id,
        target_subscription_id=target_id,
        preview_fingerprint=fingerprint,
    )


def test_candidates_and_preview_show_exact_restoration_consequences(
    db_session, subscriber, catalog_offer
):
    wrong, target, credential, profile = _fixture(db_session, subscriber, catalog_offer)

    candidates = list_correction_candidates(db_session, wrong.id)
    preview = preview_subscription_correction(
        db_session,
        active_subscription_id=wrong.id,
        target_subscription_id=target.id,
    )

    assert [candidate.subscription_id for candidate in candidates] == [target.id]
    assert preview.eligible is True
    assert preview.active_offer_name == catalog_offer.name
    assert preview.target_offer_name == "Unlimited Lite"
    assert preview.target_created_at == target.created_at
    assert preview.credential_id == credential.id
    assert preview.target_radius_profile_id == profile.id
    assert preview.target_speed_label == "15 Mbps down / 15 Mbps up"
    assert preview.active_fup_status is FupActionStatus.throttled
    assert preview.active_invoice_line_count == 0


def test_admin_projection_uses_exact_shared_action_form(
    db_session, subscriber, catalog_offer
):
    wrong, target, _credential, _profile = _fixture(
        db_session, subscriber, catalog_offer
    )

    forms = _subscription_correction_action_forms(db_session, str(wrong.id))

    assert len(forms) == 1
    form = forms[0]
    assert form.allowed is True
    assert form.confirmation is not None
    assert form.fields == ()
    assert form.action_url.endswith(f"/{wrong.id}/correction/execute")
    hidden = {item.key: item.value for item in form.hidden_values}
    assert hidden["target_subscription_id"] == str(target.id)
    assert hidden["preview_fingerprint"]
    assert hidden["idempotency_key"].startswith("subscription-correction:")
    assert "15 Mbps down / 15 Mbps up" in str(form.impact)
    assert "no automatic credit" in str(form.impact)
    assert str(target.id) in form.description
    assert target.created_at.isoformat() in form.description
    assert str(target.id)[:8] in form.title
    assert form.title.startswith("Correct mistake:")


@pytest.mark.parametrize(
    ("field", "value", "issue_code"),
    [
        ("ipv4_address", "not-an-ip", "target_ipv4_invalid"),
        ("ipv6_address", "10.10.10.10", "target_ipv6_invalid"),
    ],
)
def test_preview_blocks_invalid_target_ip_projection_evidence(
    db_session, subscriber, catalog_offer, field, value, issue_code
):
    wrong, target, _credential, _profile = _fixture(
        db_session, subscriber, catalog_offer
    )
    setattr(target, field, value)
    db_session.commit()

    preview = preview_subscription_correction(
        db_session,
        active_subscription_id=wrong.id,
        target_subscription_id=target.id,
    )

    assert preview.eligible is False
    assert issue_code in {issue.code for issue in preview.issues}


def test_preview_blocks_mismatched_pppoe_identity_and_unconfigured_speed(
    db_session, subscriber, catalog_offer
):
    wrong, target, credential, profile = _fixture(db_session, subscriber, catalog_offer)
    credential.username = "different-login"
    profile.download_speed = None
    profile.upload_speed = None
    profile.mikrotik_rate_limit = None
    db_session.commit()

    preview = preview_subscription_correction(
        db_session,
        active_subscription_id=wrong.id,
        target_subscription_id=target.id,
    )

    assert preview.eligible is False
    assert {
        "credential_active_login_mismatch",
        "credential_target_login_mismatch",
        "radius_profile_speed_unconfigured",
    }.issubset({issue.code for issue in preview.issues})


def test_preview_blocks_active_target_enforcement_lock(
    db_session, subscriber, catalog_offer
):
    wrong, target, _credential, _profile = _fixture(
        db_session, subscriber, catalog_offer
    )
    db_session.add(
        EnforcementLock(
            subscription_id=target.id,
            subscriber_id=subscriber.id,
            reason=EnforcementReason.admin,
            source="admin:test",
            is_active=True,
        )
    )
    db_session.commit()

    preview = preview_subscription_correction(
        db_session,
        active_subscription_id=wrong.id,
        target_subscription_id=target.id,
    )

    assert preview.eligible is False
    assert preview.target_lock_reasons == ("admin",)
    assert "target_enforcement_lock_present" in {issue.code for issue in preview.issues}


def test_newer_stopped_sibling_is_not_offered_as_a_correction_target(
    db_session, subscriber, catalog_offer
):
    wrong, target, _credential, _profile = _fixture(
        db_session, subscriber, catalog_offer
    )
    newer = Subscription(
        subscriber_id=subscriber.id,
        offer_id=catalog_offer.id,
        status=SubscriptionStatus.stopped,
        billing_mode=BillingMode.postpaid,
    )
    db_session.add(newer)
    db_session.flush()

    candidates = list_correction_candidates(db_session, wrong.id)
    crafted_preview = preview_subscription_correction(
        db_session,
        active_subscription_id=wrong.id,
        target_subscription_id=newer.id,
    )

    assert [item.subscription_id for item in candidates] == [target.id]
    assert "target_not_prior" in {issue.code for issue in crafted_preview.issues}


def test_correction_is_atomic_and_rebinds_credential_profile_and_fup(
    db_session, subscriber, catalog_offer, monkeypatch
):
    wrong, target, credential, profile = _fixture(db_session, subscriber, catalog_offer)
    wrong_id, target_id = wrong.id, target.id
    preview = preview_subscription_correction(
        db_session,
        active_subscription_id=wrong.id,
        target_subscription_id=target.id,
    )
    db_session.commit()

    # Delivery is separately tested by the event handlers. Keep this owner test
    # focused on the committed authoritative state.
    monkeypatch.setattr(
        "app.services.events.dispatcher.dispatch_pending_events_after_commit",
        lambda *_args, **_kwargs: None,
        raising=False,
    )
    outcome = correct_subscription(
        db_session,
        _command(wrong_id, target_id, preview.fingerprint, key=f"test:{uuid4()}"),
    )

    db_session.refresh(wrong)
    db_session.refresh(target)
    db_session.refresh(credential)
    state = db_session.get(FupState, wrong.id)
    assert outcome.replayed is False
    assert wrong.status is SubscriptionStatus.canceled
    assert target.status is SubscriptionStatus.active
    assert credential.subscription_id == target.id
    assert credential.radius_profile_id == profile.id
    assert credential.pre_throttle_radius_profile_id is None
    if state is not None:
        assert state.action_status is FupActionStatus.none
        assert state.throttle_profile_id is None


def test_preview_blocks_any_existing_financial_history(
    db_session, subscriber, catalog_offer
):
    wrong, target, _credential, _profile = _fixture(
        db_session, subscriber, catalog_offer
    )
    invoice = Invoice(
        account_id=subscriber.id,
        invoice_number=f"TEST-{uuid4().hex[:8]}",
        status=InvoiceStatus.issued,
        subtotal=Decimal("56437.50"),
        total=Decimal("56437.50"),
        balance_due=Decimal("56437.50"),
    )
    db_session.add(invoice)
    db_session.flush()
    db_session.add(
        InvoiceLine(
            invoice_id=invoice.id,
            subscription_id=wrong.id,
            description="Mistaken plan",
            amount=Decimal("56437.50"),
            unit_price=Decimal("56437.50"),
        )
    )
    db_session.commit()

    preview = preview_subscription_correction(
        db_session,
        active_subscription_id=wrong.id,
        target_subscription_id=target.id,
    )

    assert preview.eligible is False
    assert preview.active_invoice_line_count == 1
    assert preview.active_invoice_statuses == ("issued",)
    assert {issue.code for issue in preview.issues} == {"financial_history_present"}


def test_execute_rejects_stale_preview_without_partial_change(
    db_session, subscriber, catalog_offer
):
    wrong, target, credential, _profile = _fixture(
        db_session, subscriber, catalog_offer
    )
    wrong_id, target_id = wrong.id, target.id
    preview = preview_subscription_correction(
        db_session,
        active_subscription_id=wrong.id,
        target_subscription_id=target.id,
    )
    credential.subscription_id = target.id
    db_session.commit()

    with pytest.raises(SubscriptionCorrectionError) as rejected:
        correct_subscription(
            db_session,
            _command(
                wrong_id,
                target_id,
                preview.fingerprint,
                key=f"test:{uuid4()}",
            ),
        )

    assert rejected.value.code == "access.subscription_correction.preview_changed"
    db_session.refresh(wrong)
    db_session.refresh(target)
    assert wrong.status is SubscriptionStatus.active
    assert target.status is SubscriptionStatus.stopped
