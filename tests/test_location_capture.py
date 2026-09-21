"""Location capture: the three surfaces drive the reconciler, honestly.

Field arrival, portal, and agent all reach ``geocode_reconciler`` through
``location_capture``. These prove the wiring: a clean pin captures, a
disagreement does not, the feature gate holds, and the prompt respects both
completeness and the snooze (payment overriding it).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.models.catalog import (
    AccessType,
    BillingCycle,
    BillingMode,
    CatalogOffer,
    PriceBasis,
    ServiceType,
    Subscription,
    SubscriptionStatus,
)
from app.models.location_capture_prompt import LocationCapturePromptState
from app.models.subscriber import Subscriber, SubscriberCategory
from app.models.subscriber_field_verification import SubscriberFieldVerification
from app.services import control_registry
from app.services import geocode_reconciler as gr
from app.services import location_capture as lc


def _offer(db) -> CatalogOffer:
    offer = CatalogOffer(
        name=f"Plan {uuid.uuid4().hex[:5]}",
        code=f"P-{uuid.uuid4().hex[:6]}",
        service_type=ServiceType.residential,
        access_type=AccessType.fiber,
        price_basis=PriceBasis.flat,
        billing_cycle=BillingCycle.monthly,
        billing_mode=BillingMode.prepaid,
        speed_download_mbps=100,
        speed_upload_mbps=100,
        is_active=True,
    )
    db.add(offer)
    db.commit()
    return offer


def _subscriber(db, *, region=None, **kwargs) -> Subscriber:
    subscriber = Subscriber(
        first_name="A",
        last_name="B",
        email=f"s-{uuid.uuid4().hex[:8]}@x.io",
        subscriber_number=f"S-{uuid.uuid4().hex[:6]}",
        account_number=f"ACC-{uuid.uuid4().hex[:6]}",
        region=region,
        **kwargs,
    )
    subscriber.category = SubscriberCategory.residential
    db.add(subscriber)
    db.commit()
    return subscriber


def _subscribe(db, subscriber, offer, *, status=SubscriptionStatus.active):
    db.add(
        Subscription(
            subscriber_id=subscriber.id,
            offer_id=offer.id,
            status=status,
            billing_mode=BillingMode.prepaid,
        )
    )
    db.commit()


def _ledger_rows(db, subscriber_id, key: str) -> list[SubscriberFieldVerification]:
    return (
        db.query(SubscriberFieldVerification)
        .filter(SubscriberFieldVerification.subscriber_id == subscriber_id)
        .filter(SubscriberFieldVerification.field_key == key)
        .all()
    )


@pytest.fixture
def _lagos_geo(monkeypatch):
    """Nominatim returns Lagos/Eti-Osa; the LGA validates against reference
    data. capture() then reconciles state (agree) and lga (agree)."""
    monkeypatch.setattr(
        gr,
        "reverse",
        lambda db, lat, lng: gr.GeocodeResult(
            state="Lagos", lga="Eti-Osa", postcode=None, town="Lekki"
        ),
    )
    monkeypatch.setattr(
        gr, "_validated_lga", lambda state, lga: "Eti-Osa" if lga else None
    )


# ── capture() adjudicates ────────────────────────────────────────────────────


def test_clean_pin_captures_state_and_lga(db_session, monkeypatch, _lagos_geo):
    monkeypatch.setattr(control_registry, "is_enabled", lambda db, key: True)
    sub = _subscriber(db_session)
    result = lc.capture(
        db_session,
        str(sub.id),
        lat=6.43,
        lng=3.42,
        accuracy_m=20.0,
        source=gr.SOURCE_CUSTOMER_PORTAL,
        claimed_state="Lagos",
        claimed_lga="Eti-Osa",
    )
    keys = {k.value for k in result.captured_keys}
    assert "state" in keys and "lga" in keys
    assert _ledger_rows(db_session, sub.id, "state")
    assert _ledger_rows(db_session, sub.id, "lga")


def test_disagreement_writes_no_ledger_row(db_session, monkeypatch, _lagos_geo):
    """The customer claims Kano, the pin says Lagos: nothing is captured for a
    field in conflict — it is flagged for a human instead."""
    monkeypatch.setattr(control_registry, "is_enabled", lambda db, key: True)
    sub = _subscriber(db_session)
    result = lc.capture(
        db_session,
        str(sub.id),
        lat=6.43,
        lng=3.42,
        accuracy_m=20.0,
        source=gr.SOURCE_CUSTOMER_PORTAL,
        claimed_state="Kano",
    )
    assert gr.FieldKey.state not in result.captured_keys
    assert not _ledger_rows(db_session, sub.id, "state")
    assert any(f.key is gr.FieldKey.state for f in result.reconciliation.needs_human)


def test_direct_capture_is_inert_when_master_gate_is_off(
    db_session, monkeypatch, _lagos_geo
):
    monkeypatch.setattr(control_registry, "is_enabled", lambda db, key: False)
    sub = _subscriber(db_session)
    with pytest.raises(lc.LocationCaptureDisabled):
        lc.capture(
            db_session,
            str(sub.id),
            lat=6.43,
            lng=3.42,
            source=gr.SOURCE_CUSTOMER_PORTAL,
            claimed_state="Lagos",
        )
    assert not _ledger_rows(db_session, sub.id, "state")


def test_prompt_capture_requires_the_prompt_subcontrol(
    db_session, monkeypatch, _lagos_geo
):
    monkeypatch.setattr(
        control_registry,
        "is_enabled",
        lambda db, key: key == "loyalty.campaigns",
    )
    sub = _subscriber(db_session)
    with pytest.raises(lc.LocationCaptureDisabled):
        lc.capture(
            db_session,
            str(sub.id),
            lat=6.43,
            lng=3.42,
            source=gr.SOURCE_AGENT,
        )


# ── field arrival ────────────────────────────────────────────────────────────


def test_field_arrival_captures_when_enabled(db_session, monkeypatch, _lagos_geo):
    monkeypatch.setattr(control_registry, "is_enabled", lambda db, key: True)
    sub = _subscriber(db_session)
    result = lc.capture_from_field_arrival(
        db_session,
        subscriber_id=str(sub.id),
        lat=6.43,
        lng=3.42,
        accuracy_m=15.0,
        technician_actor_id="tech-1",
        technician_name="Tech One",
    )
    assert result is not None
    rows = _ledger_rows(db_session, sub.id, "state")
    assert rows and rows[0].source == gr.SOURCE_FIELD_GPS


def test_field_arrival_is_inert_when_gate_off(db_session, monkeypatch, _lagos_geo):
    monkeypatch.setattr(control_registry, "is_enabled", lambda db, key: False)
    sub = _subscriber(db_session)
    result = lc.capture_from_field_arrival(
        db_session,
        subscriber_id=str(sub.id),
        lat=6.43,
        lng=3.42,
        accuracy_m=15.0,
        technician_actor_id="tech-1",
        technician_name="Tech One",
    )
    assert result is None
    assert not _ledger_rows(db_session, sub.id, "state")


def test_field_arrival_needs_a_fix(db_session, monkeypatch):
    monkeypatch.setattr(control_registry, "is_enabled", lambda db, key: True)
    sub = _subscriber(db_session)
    assert (
        lc.capture_from_field_arrival(
            db_session,
            subscriber_id=str(sub.id),
            lat=None,
            lng=None,
            accuracy_m=None,
            technician_actor_id="tech-1",
            technician_name="Tech One",
        )
        is None
    )


def test_field_arrival_rolls_back_only_the_failed_capture(db_session, monkeypatch):
    sub = _subscriber(db_session)

    def fail_after_write(db, subscriber_id, **kwargs):
        db.add(
            SubscriberFieldVerification(
                subscriber_id=sub.id,
                field_key="state",
                value="Lagos",
                source=gr.SOURCE_FIELD_GPS,
                verified_at=datetime.now(UTC),
            )
        )
        db.flush()
        raise RuntimeError("capture failed after ledger flush")

    monkeypatch.setattr(lc, "capture", fail_after_write)
    result = lc.capture_from_field_arrival(
        db_session,
        subscriber_id=str(sub.id),
        lat=6.43,
        lng=3.42,
        accuracy_m=15.0,
        technician_actor_id="tech-1",
        technician_name="Tech One",
    )
    assert result is None
    assert not _ledger_rows(db_session, sub.id, "state")
    sub.display_name = "Outer transaction still usable"
    db_session.commit()
    assert sub.display_name == "Outer transaction still usable"


# ── the prompt: completeness + snooze ────────────────────────────────────────


def test_prompt_only_when_location_needs_revalidation(db_session, monkeypatch):
    monkeypatch.setattr(control_registry, "is_enabled", lambda db, key: True)
    # region resolves -> the state is inferred (present, unconfirmed) -> prompt.
    inferred = _subscriber(db_session, region="Lagos")
    assert lc.should_prompt(db_session, inferred) is True


def test_prompt_suppressed_when_gate_off(db_session, monkeypatch):
    monkeypatch.setattr(control_registry, "is_enabled", lambda db, key: False)
    sub = _subscriber(db_session, region="Lagos")
    assert lc.should_prompt(db_session, sub) is False


def test_prompt_requires_master_and_prompt_controls(db_session, monkeypatch):
    monkeypatch.setattr(
        control_registry,
        "is_enabled",
        lambda db, key: key == "loyalty.capture_prompt",
    )
    sub = _subscriber(db_session, region="Lagos")
    assert lc.should_prompt(db_session, sub) is False


def test_snooze_hides_the_prompt(db_session, monkeypatch):
    monkeypatch.setattr(control_registry, "is_enabled", lambda db, key: True)
    sub = _subscriber(db_session, region="Lagos")
    lc.snooze_prompt(db_session, str(sub.id))
    db_session.commit()
    assert lc.should_prompt(db_session, sub) is False


def test_payment_reprompts_through_the_snooze(db_session, monkeypatch):
    monkeypatch.setattr(control_registry, "is_enabled", lambda db, key: True)
    sub = _subscriber(db_session, region="Lagos")
    lc.snooze_prompt(db_session, str(sub.id))
    db_session.commit()
    # Browsing: suppressed. Payment (ignore_snooze): shown.
    assert lc.should_prompt(db_session, sub) is False
    assert lc.should_prompt(db_session, sub, ignore_snooze=True) is True


def test_portal_payment_context_passes_the_snooze_override(db_session, monkeypatch):
    from app.web.customer import location as portal_location

    monkeypatch.setattr(control_registry, "is_enabled", lambda db, key: True)
    sub = _subscriber(db_session, region="Lagos")
    lc.snooze_prompt(db_session, str(sub.id))
    db_session.commit()
    monkeypatch.setattr(
        portal_location,
        "optional_customer_subscriber_id",
        lambda db, customer: sub.id,
    )
    context = portal_location.location_prompt_context(
        db_session, {"id": str(sub.id)}, ignore_snooze=True
    )
    assert context is not None
    assert context["claimed_state"] == "Lagos"
    assert context["snooze_allowed"] is False


def test_expired_snooze_shows_again(db_session, monkeypatch):
    monkeypatch.setattr(control_registry, "is_enabled", lambda db, key: True)
    sub = _subscriber(db_session, region="Lagos")
    state = LocationCapturePromptState(
        subscriber_id=sub.id,
        snoozed_until=datetime.now(UTC) - timedelta(days=1),
    )
    db_session.add(state)
    db_session.commit()
    assert lc.should_prompt(db_session, sub) is True


def test_prompt_context_is_none_when_not_prompting(db_session, monkeypatch):
    monkeypatch.setattr(control_registry, "is_enabled", lambda db, key: False)
    sub = _subscriber(db_session, region="Lagos")
    assert lc.prompt_context(db_session, sub) is None


def test_location_owners_are_declared_and_out_of_writer_baseline():
    from pathlib import Path

    from app.services import sot_relationships as sr

    names = {
        service.name
        for domain in sr.DOMAIN_SOT_RELATIONSHIPS
        for service in domain.services
    }
    assert {
        "customer.data_completeness",
        "customer.location_verification",
        "customer.location_capture",
    } <= names
    assert sr.dependencies_for("customer.location_capture") == (
        "customer.identity_scope",
        "customer.data_completeness",
        "customer.location_verification",
    )
    baseline = Path("tests/architecture/sot_writer_baseline.txt").read_text().split()
    assert "app.services.geocode_reconciler" not in baseline
    assert "app.services.location_capture" not in baseline


def test_service_location_requirement_is_operator_controlled(db_session, monkeypatch):
    subscriber = _subscriber(db_session)
    monkeypatch.setattr(
        lc.settings_spec,
        "resolve_value",
        lambda _db, _domain, _key: False,
    )
    assert not lc.requires_service_location_update(db_session, str(subscriber.id))

    monkeypatch.setattr(
        lc.settings_spec,
        "resolve_value",
        lambda _db, _domain, _key: True,
    )
    assert lc.requires_service_location_update(db_session, str(subscriber.id))


def test_service_location_requirement_accepts_canonical_coordinates(
    db_session, monkeypatch
):
    subscriber = _subscriber(db_session)
    monkeypatch.setattr(
        lc.settings_spec,
        "resolve_value",
        lambda _db, _domain, _key: True,
    )
    monkeypatch.setattr(
        lc.service_address_service,
        "service_address",
        lambda _db, _subscriber_id: type(
            "AddressStub", (), {"latitude": 6.43, "longitude": 3.42}
        )(),
    )
    assert not lc.requires_service_location_update(db_session, str(subscriber.id))


def test_service_location_requirement_fails_open_on_coordinate_read_error(
    db_session, monkeypatch
):
    subscriber = _subscriber(db_session)
    monkeypatch.setattr(
        lc.settings_spec,
        "resolve_value",
        lambda _db, _domain, _key: True,
    )

    def raise_read_error(_db, _subscriber_id):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(lc.service_address_service, "service_address", raise_read_error)
    assert not lc.requires_service_location_update(db_session, str(subscriber.id))
