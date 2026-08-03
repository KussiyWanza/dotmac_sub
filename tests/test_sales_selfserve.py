"""Self-serve quote extraction tests (Phase 3 §2.2).

Covers: feasibility classification against the native FAP proximity result,
estimate pricing (derived / bundle / offer-priced), the map-pin capture
contract on ``request_quote`` (install{lat,lng,address,region} stamped on
lead + quote metadata — reused downstream for estimate/survey/billing), the
portal payload shape (§2.5: money as strings), and accept-with-deposit
idempotency + the risk-#2 no-second-payment invariant.

``_nearest_fiber_access_point`` is monkeypatched — the PostGIS query itself
needs a spatial database (the pricing/classification logic is what's under
test, same isolation the CRM source used).
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from app.models.billing import Payment
from app.models.catalog import (
    AccessType,
    CatalogOffer,
    OfferPrice,
    PriceBasis,
    PriceType,
    ServiceType,
)
from app.models.party import Party
from app.models.project import ProjectTemplate
from app.models.sales import Lead, SalesOrder
from app.models.subscriber import Subscriber
from app.services.sales import selfserve

_FAP = SimpleNamespace(id=uuid.uuid4(), name="NAP-041")


def _cfg(**overrides) -> dict:
    """Resolved settings dict mirroring the spec defaults."""
    cfg = {
        "enabled": True,
        "base_fee": Decimal("50000.00"),
        "free_radius_m": 300,
        "fee_per_km": Decimal("25000.00"),
        "deposit_percent": 50,
        "feasibility_radius_m": 2000,
        "bundle_offer_id": None,
        "base_offer_id": None,
        "distance_offer_id": None,
    }
    cfg.update(overrides)
    return cfg


def _subscriber(db) -> Subscriber:
    party = Party(
        display_name="Ada Obi",
        party_type="person",
        status="active",
    )
    db.add(party)
    db.flush()
    sub = Subscriber(
        first_name="Ada",
        last_name="Obi",
        email=f"ada-{uuid.uuid4().hex[:10]}@example.com",
        party_id=party.id,
        party_bound_at=datetime.now(UTC),
        party_binding_source="pytest",
        party_binding_reason="Self-serve sales fixture Party binding",
    )
    db.add(sub)
    if (
        db.query(ProjectTemplate)
        .filter_by(project_type="fiber_optics_installation", is_active=True)
        .first()
        is None
    ):
        db.add(
            ProjectTemplate(
                name=f"Self-serve quote {uuid.uuid4().hex[:8]}",
                project_type="fiber_optics_installation",
                is_active=True,
            )
        )
    db.commit()
    db.refresh(sub)
    return sub


def _offer(db, name="Install bundle", price="120000.00", price_type=PriceType.one_time):
    offer = CatalogOffer(
        name=name,
        service_type=ServiceType.residential,
        access_type=AccessType.fiber,
        price_basis=PriceBasis.flat,
    )
    db.add(offer)
    db.flush()
    db.add(OfferPrice(offer_id=offer.id, price_type=price_type, amount=Decimal(price)))
    db.commit()
    db.refresh(offer)
    return offer


def _patch_fap(result):
    return patch(
        "app.services.sales.selfserve._nearest_fiber_access_point",
        return_value=result,
    )


def _request(db, sub, *, distance=1300.0, address="12 Mississippi St, Maitama", **kw):
    with _patch_fap((_FAP, distance)):
        return selfserve.selfserve_quotes.request_quote(
            db,
            str(sub.id),
            latitude=kw.pop("latitude", 9.0765),
            longitude=kw.pop("longitude", 7.3986),
            address=address,
            **kw,
        )


# ---------------------------------------------------------------------------
# Feasibility (native FAP)
# ---------------------------------------------------------------------------


def test_feasibility_out_of_area_without_fiber_plant(db_session):
    with _patch_fap((None, None)):
        out = selfserve.compute_feasibility(db_session, 9.0, 7.4)
    assert out == {
        "feasible": False,
        "coverage": "out_of_area",
        "nearest_fap_id": None,
        "nearest_fap_name": None,
        "distance_meters": None,
    }


def test_feasibility_covered_within_radius(db_session):
    with _patch_fap((_FAP, 1999.9)):
        out = selfserve.compute_feasibility(db_session, 9.0, 7.4)
    assert out["feasible"] is True
    assert out["coverage"] == "covered"
    assert out["nearest_fap_id"] == str(_FAP.id)
    assert out["nearest_fap_name"] == "NAP-041"
    assert out["distance_meters"] == 1999.9


def test_feasibility_survey_required_beyond_radius(db_session):
    with _patch_fap((_FAP, 2000.1)):
        out = selfserve.compute_feasibility(db_session, 9.0, 7.4)
    assert out["coverage"] == "survey_required"
    assert out["feasible"] is True


# ---------------------------------------------------------------------------
# Estimate pricing
# ---------------------------------------------------------------------------


def test_estimate_derived_with_distance_surcharge(db_session):
    # 1300 m − 300 m free radius = 1 km billable → 50,000 + 25,000.
    feasibility = {"coverage": "covered", "distance_meters": 1300.0}
    out = selfserve.compute_estimate(db_session, feasibility, "NGN")
    assert out["pricing_mode"] == "derived"
    assert out["base_fee"] == Decimal("50000.00")
    assert out["distance_fee"] == Decimal("25000.00")
    assert out["subtotal"] == Decimal("75000.00")
    assert out["deposit_percent"] == 50
    assert out["deposit_amount"] == Decimal("37500.00")
    assert out["provisional"] is False
    assert [li["description"] for li in out["line_items"]] == [
        "Fiber installation (base)",
        "Distance surcharge (1.0 km beyond free radius)",
    ]


def test_estimate_within_free_radius_has_no_surcharge(db_session):
    feasibility = {"coverage": "covered", "distance_meters": 250.0}
    out = selfserve.compute_estimate(db_session, feasibility, "NGN")
    assert out["distance_fee"] == Decimal("0.00")
    assert out["subtotal"] == Decimal("50000.00")
    assert len(out["line_items"]) == 1


def test_estimate_survey_required_is_provisional_base_only(db_session):
    # Distance is not billed until a survey confirms the run.
    feasibility = {"coverage": "survey_required", "distance_meters": 4200.0}
    out = selfserve.compute_estimate(db_session, feasibility, "NGN")
    assert out["provisional"] is True
    assert out["distance_fee"] == Decimal("0.00")
    assert out["subtotal"] == Decimal("50000.00")


def test_estimate_bundle_offer_flat_price(db_session):
    offer = _offer(db_session, name="Fiber install bundle", price="120000.00")
    with patch(
        "app.services.sales.selfserve._settings",
        return_value=_cfg(bundle_offer_id=str(offer.id)),
    ):
        out = selfserve.compute_estimate(
            db_session, {"coverage": "covered", "distance_meters": 5000.0}, "NGN"
        )
    assert out["pricing_mode"] == "bundle"
    assert out["subtotal"] == Decimal("120000.00")
    assert out["deposit_amount"] == Decimal("60000.00")
    assert out["provisional"] is False
    (line,) = out["line_items"]
    assert line["description"] == "Fiber install bundle"
    assert line["sub_offer_id"] == str(offer.id)


def test_estimate_base_offer_price_overrides_setting(db_session):
    offer = _offer(db_session, name="Standard fiber install", price="65000.00")
    with patch(
        "app.services.sales.selfserve._settings",
        return_value=_cfg(base_offer_id=str(offer.id)),
    ):
        out = selfserve.compute_estimate(
            db_session, {"coverage": "covered", "distance_meters": 100.0}, "NGN"
        )
    assert out["base_fee"] == Decimal("65000.00")
    assert out["line_items"][0]["description"] == "Standard fiber install"
    assert out["line_items"][0]["sub_offer_id"] == str(offer.id)


def test_estimate_missing_or_inactive_offer_falls_back_to_settings(db_session):
    with patch(
        "app.services.sales.selfserve._settings",
        return_value=_cfg(base_offer_id=str(uuid.uuid4())),
    ):
        out = selfserve.compute_estimate(
            db_session, {"coverage": "covered", "distance_meters": 100.0}, "NGN"
        )
    assert out["base_fee"] == Decimal("50000.00")
    assert out["line_items"][0]["sub_offer_id"] is None


# ---------------------------------------------------------------------------
# Request — the map-pin capture contract
# ---------------------------------------------------------------------------


def test_request_quote_captures_map_pin_on_lead_and_quote(db_session):
    sub = _subscriber(db_session)
    quote = _request(
        db_session,
        sub,
        distance=1300.0,
        region="Abuja",
        note="Front gate faces the street",
    )

    install = {
        "latitude": 9.0765,
        "longitude": 7.3986,
        "address": "12 Mississippi St, Maitama",
        "region": "Abuja",
    }
    # Quote carries the pin + feasibility + deposit contract (§1.4 metadata).
    meta = quote.metadata_
    assert meta["install"] == install
    assert meta["source"] == "portal_self_serve"
    assert quote.project_type == "fiber_optics_installation"
    assert "project_type" not in meta
    assert meta["feasibility"]["coverage"] == "covered"
    assert meta["feasibility"]["nearest_fap_name"] == "NAP-041"
    assert meta["deposit_percent"] == 50
    assert meta["estimate_provisional"] is False
    assert meta["pricing_mode"] == "derived"
    # §1.4: never write the legacy subscriber_external_id key for new quotes.
    assert "subscriber_external_id" not in meta

    # Lead carries the same pin (survey/install crews read it from the lead).
    lead = db_session.get(Lead, quote.lead_id)
    assert lead is not None
    assert lead.metadata_["install"] == install
    assert lead.metadata_["source"] == "portal_self_serve"
    assert lead.lead_source == "Portal"
    assert lead.title == "Self-serve installation request"
    assert lead.address == "12 Mississippi St, Maitama"
    assert lead.notes == "Front gate faces the street"

    # Estimate lines + totals landed on the draft quote.
    assert quote.status == "draft"
    assert quote.total == Decimal("75000.00")
    assert len(quote.line_items) == 2


def test_request_quote_payload_serializes_pin_and_money_strings(db_session):
    sub = _subscriber(db_session)
    quote = _request(db_session, sub, distance=1300.0)
    payload = selfserve.build_portal_quote_payload(db_session, quote)

    assert payload["id"] == str(quote.id)
    assert payload["latitude"] == 9.0765
    assert payload["longitude"] == 7.3986
    assert payload["address"] == "12 Mississippi St, Maitama"
    # §2.5 mobile contract: money and quantities are strings.
    assert payload["total"] == "75000.00"
    assert payload["deposit_amount"] == "37500.00"
    assert payload["deposit_percent"] == 50
    assert payload["deposit_paid"] is False
    for line in payload["line_items"]:
        assert isinstance(line["quantity"], str)
        assert isinstance(line["unit_price"], str)
        assert isinstance(line["amount"], str)
    assert payload["subscriber_id"] == str(sub.id)
    assert payload["sales_order_id"] is None
    assert payload["project_id"] is None  # PR 6 seam


def test_request_quote_403_when_disabled(db_session):
    sub = _subscriber(db_session)
    with patch(
        "app.services.sales.selfserve._settings",
        return_value=_cfg(enabled=False),
    ):
        with pytest.raises(HTTPException) as exc:
            selfserve.selfserve_quotes.request_quote(
                db_session, str(sub.id), latitude=9.0, longitude=7.4
            )
    assert exc.value.status_code == 403


def test_request_quote_404_for_unknown_subscriber(db_session):
    with _patch_fap((_FAP, 100.0)):
        with pytest.raises(HTTPException) as exc:
            selfserve.selfserve_quotes.request_quote(
                db_session, str(uuid.uuid4()), latitude=9.0, longitude=7.4
            )
    assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# Accept with deposit
# ---------------------------------------------------------------------------


def test_accept_with_deposit_accepts_and_marks_sales_order(db_session):
    sub = _subscriber(db_session)
    quote = _request(db_session, sub, distance=1300.0)

    payload = selfserve.selfserve_quotes.accept_with_deposit(
        db_session,
        str(sub.id),
        str(quote.id),
        deposit_reference="ref_1",
        deposit_amount="37500.00",
        provider="paystack",
    )

    assert payload["status"] == "accepted"
    assert payload["deposit_paid"] is True
    assert payload["deposit_reference"] == "ref_1"
    assert payload["already_accepted"] is False

    order = db_session.query(SalesOrder).filter(SalesOrder.quote_id == quote.id).one()
    assert payload["sales_order_id"] == str(order.id)
    assert order.status == "confirmed"
    assert order.deposit_required is True
    assert order.deposit_paid is True
    assert order.amount_paid == Decimal("37500.00")
    assert order.balance_due == Decimal("37500.00")
    assert order.payment_status == "partial"

    # Risk #2: the accept is SO bookkeeping only — never a payment row (the
    # sole ledger event is verify_and_record_payment on the deposit invoice).
    assert db_session.query(Payment).count() == 0


def test_accept_with_deposit_is_idempotent(db_session):
    sub = _subscriber(db_session)
    quote = _request(db_session, sub, distance=1300.0)

    first = selfserve.selfserve_quotes.accept_with_deposit(
        db_session,
        str(sub.id),
        str(quote.id),
        deposit_reference="ref_1",
        deposit_amount="37500.00",
    )
    second = selfserve.selfserve_quotes.accept_with_deposit(
        db_session,
        str(sub.id),
        str(quote.id),
        deposit_reference="ref_1",
        deposit_amount="37500.00",
    )

    assert first["already_accepted"] is False
    assert second["already_accepted"] is True
    # The retry returns the same sales order; only one exists.
    assert second["sales_order_id"] == first["sales_order_id"]
    orders = db_session.query(SalesOrder).filter(SalesOrder.quote_id == quote.id).all()
    assert len(orders) == 1
    # Only the exact evidence is idempotent; the original stamp is unchanged.
    assert second["deposit_reference"] == "ref_1"


def test_accept_full_deposit_marks_order_paid(db_session):
    sub = _subscriber(db_session)
    quote = _request(db_session, sub, distance=1300.0)
    selfserve.selfserve_quotes.accept_with_deposit(
        db_session,
        str(sub.id),
        str(quote.id),
        deposit_reference="ref_full",
        deposit_amount="75000.00",
    )
    order = db_session.query(SalesOrder).filter(SalesOrder.quote_id == quote.id).one()
    assert order.payment_status == "paid"
    assert order.status == "paid"
    assert order.balance_due == Decimal("0.00")


def test_accept_is_subscriber_scoped(db_session):
    sub = _subscriber(db_session)
    other = _subscriber(db_session)
    quote = _request(db_session, sub, distance=1300.0)
    with pytest.raises(HTTPException) as exc:
        selfserve.selfserve_quotes.accept_with_deposit(
            db_session,
            str(other.id),
            str(quote.id),
            deposit_reference="ref_x",
            deposit_amount="37500.00",
        )
    assert exc.value.status_code == 404
    db_session.refresh(quote)
    assert quote.status == "draft"
