"""Phase 3 PR 8 — §2.5 golden-payload tests for the read-flipped surfaces.

The mirror rows are recorded CRM responses (``QuoteMirror.payload`` literally
is one), so they double as fixtures: for every repointed surface the native
path's response must be shape-identical to the mirror path's — same keys,
compatible value types — and the untouched mobile schemas
(``app/schemas/portal.py``) must deserialize both. This turns "shapes
preserved" from a claim into a gate (§2.5).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from app.models.domain_settings import DomainSetting, SettingDomain, SettingValueType
from app.models.party import Party
from app.models.project import ProjectTemplate
from app.models.quote_mirror import QuoteMirror, QuoteSyncState
from app.models.referral import ReferralMirror, ReferralProgramCache
from app.models.subscriber import Reseller, Subscriber
from app.schemas.portal import (
    MyQuotesResponse,
    MyReferralsResponse,
    QuoteItem,
    ReferralItem,
)
from app.services import (
    quotes_mirror,
    referrals_mirror,
    reseller_crm_views,
)
from app.services import referrals as referrals_service
from app.services.sales import selfserve as selfserve_service

# ── shape comparison ──────────────────────────────────────────────────────────


def _assert_same_shape(native, mirror, path="$"):
    """Same keys, compatible value types, recursively.

    ``None`` is a wildcard on either side — the §2.5 schemas make those keys
    optional — but when both sides carry a value the types must match exactly
    (so a money *string* can never silently become a float, and an int never
    a bool)."""
    if native is None or mirror is None:
        return
    assert type(native) is type(mirror), (
        f"{path}: {type(native).__name__} != {type(mirror).__name__}"
    )
    if isinstance(native, dict):
        assert set(native) == set(mirror), (
            f"{path}: key mismatch {sorted(set(native) ^ set(mirror))}"
        )
        for key in native:
            _assert_same_shape(native[key], mirror[key], f"{path}.{key}")
    elif isinstance(native, list):
        if not native or not mirror:
            return
        for i, item in enumerate(native):
            _assert_same_shape(item, mirror[0], f"{path}[{i}]")
        for i, item in enumerate(mirror):
            _assert_same_shape(native[0], item, f"{path}<-mirror[{i}]")


# ── fixtures ──────────────────────────────────────────────────────────────────


def _subscriber(db, reseller_id=None) -> Subscriber:
    party = Party(
        display_name="Cust Omer",
        party_type="person",
        status="active",
    )
    db.add(party)
    db.flush()
    sub = Subscriber(
        first_name="Cust",
        last_name="Omer",
        email=f"c-{uuid.uuid4().hex[:8]}@example.com",
        reseller_id=reseller_id,
        party_id=party.id,
        party_bound_at=datetime.now(UTC),
        party_binding_source="pytest",
        party_binding_reason="Golden payload fixture Party binding",
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
                name=f"Golden quote {uuid.uuid4().hex[:8]}",
                project_type="fiber_optics_installation",
                is_active=True,
            )
        )
    db.commit()
    db.refresh(sub)
    return sub


def _synced_quotes(db, sub):
    db.add(QuoteSyncState(subscriber_id=sub.id, synced_at=datetime.now(UTC)))
    db.commit()


# A recorded CRM portal-quote response (the exact shape QuoteMirror.payload
# cached — see dotmac_crm portal_quotes serializer / §2.5): money and
# quantities as strings, deposit_percent int, floats for the pin.
def _crm_quote_payload(quote_id: str, subscriber_id: str) -> dict:
    return {
        "id": quote_id,
        "status": "accepted",
        "currency": "NGN",
        "subtotal": "75000.00",
        "tax_total": "0.00",
        "total": "75000.00",
        "project_type": "fiber_optics_installation",
        "subscriber_id": subscriber_id,
        "subscriber_external_id": None,
        "latitude": 9.0765,
        "longitude": 7.3986,
        "address": "12 Mississippi St, Maitama",
        "region": "Abuja",
        "feasibility": {
            "coverage": "covered",
            "feasible": True,
            "distance_meters": 1300.0,
            "nearest_fap_name": "NAP-041",
        },
        "estimate_provisional": False,
        "deposit_percent": 50,
        "deposit_amount": "37500.00",
        "deposit_paid": True,
        "deposit_reference": "ref_dep_1",
        "line_items": [
            {
                "description": "Fiber installation (base)",
                "quantity": "1.000",
                "unit_price": "50000.00",
                "amount": "50000.00",
            },
            {
                "description": "Distance surcharge (1.0 km beyond 300 m)",
                "quantity": "1.000",
                "unit_price": "25000.00",
                "amount": "25000.00",
            },
        ],
        "sales_order_id": str(uuid.uuid4()),
        "project_id": str(uuid.uuid4()),
        "already_accepted": False,
        "created_at": "2026-06-29T10:00:00+00:00",
        "expires_at": None,
    }


def _mirror_quote(db, sub) -> QuoteMirror:
    payload = _crm_quote_payload(f"q-{uuid.uuid4().hex[:8]}", str(uuid.uuid4()))
    row = QuoteMirror(
        crm_quote_id=payload["id"],
        subscriber_id=sub.id,
        status=payload["status"],
        currency=payload["currency"],
        total=payload["total"],
        deposit_amount=payload["deposit_amount"],
        deposit_percent=payload["deposit_percent"],
        deposit_paid=payload["deposit_paid"],
        payload=payload,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _native_quote(db, sub, *, accept=True):
    """A fully-populated native quote: self-serve request (map pin +
    feasibility + estimate lines) then the deposit accept (sales order) and
    its structurally linked acceptance-owned Project."""
    fap = SimpleNamespace(id=uuid.uuid4(), name="NAP-041")
    with patch(
        "app.services.sales.selfserve._nearest_fiber_access_point",
        return_value=(fap, 1300.0),
    ):
        quote = selfserve_service.selfserve_quotes.request_quote(
            db,
            str(sub.id),
            latitude=9.0765,
            longitude=7.3986,
            address="12 Mississippi St, Maitama",
            region="Abuja",
        )
    if accept:
        selfserve_service.selfserve_quotes.accept_with_deposit(
            db,
            str(sub.id),
            str(quote.id),
            deposit_reference="ref_1",
            deposit_amount="37500.00",
            provider="paystack",
        )
        db.refresh(quote)
    return quote


# ── quotes: GET /me/quotes ────────────────────────────────────────────────────


def test_me_quotes_native_matches_mirror_golden_payload(db_session):
    mirror_sub = _subscriber(db_session)
    _mirror_quote(db_session, mirror_sub)
    _synced_quotes(db_session, mirror_sub)
    mirror_out = quotes_mirror.read_for_subscriber(db_session, str(mirror_sub.id))

    native_sub = _subscriber(db_session)
    _native_quote(db_session, native_sub)
    native_out = selfserve_service.selfserve_quotes.read_for_subscriber(
        db_session, str(native_sub.id)
    )

    # Shell + item shapes are identical (§2.5).
    _assert_same_shape(native_out, mirror_out)
    assert native_out["total"] == 1 and mirror_out["total"] == 1

    # Both deserialize with the untouched mobile schema.
    for out in (native_out, mirror_out):
        parsed = MyQuotesResponse.model_validate(out)
        assert len(parsed.quotes) == 1
        QuoteItem.model_validate(out["quotes"][0])

    # §2.5 hard invariants on the native item: money/quantities are strings,
    # deposit_percent is an int, id is the quote UUID.
    item = native_out["quotes"][0]
    assert isinstance(item["total"], str)
    assert isinstance(item["deposit_amount"], str)
    assert isinstance(item["deposit_percent"], int)
    for line in item["line_items"]:
        assert isinstance(line["quantity"], str)
        assert isinstance(line["unit_price"], str)
        assert isinstance(line["amount"], str)
    uuid.UUID(item["id"])
    # The accept populated the pipeline links.
    assert item["deposit_paid"] is True
    assert item["deposit_reference"] == "ref_1"
    uuid.UUID(item["sales_order_id"])
    assert item["project_id"] is not None
    uuid.UUID(item["project_id"])


def test_me_quotes_native_open_count_matches_mirror_semantics(db_session):
    sub = _subscriber(db_session)
    _native_quote(db_session, sub, accept=False)  # draft → open
    accepted = _native_quote(db_session, sub)  # accepted → closed
    out = selfserve_service.selfserve_quotes.read_for_subscriber(
        db_session, str(sub.id)
    )
    assert out["total"] == 2
    assert out["open"] == 1
    statuses = {q["id"]: q["status"] for q in out["quotes"]}
    assert statuses[str(accepted.id)] == "accepted"


# ── referrals: GET /me/referrals ──────────────────────────────────────────────


def _program_settings(db, *, amount="2500.00"):
    for key, (text, value_type) in {
        "referral_program_enabled": ("true", SettingValueType.boolean),
        "referral_reward_amount": (amount, SettingValueType.string),
    }.items():
        db.add(
            DomainSetting(
                domain=SettingDomain.subscriber,
                key=key,
                value_type=value_type,
                value_text=text,
                is_active=True,
            )
        )
    db.commit()


def _mirror_referrals(db, sub):
    db.add(
        ReferralProgramCache(
            subscriber_id=sub.id,
            code="REF23456",
            share_url="https://app.dotmac.io/r/REF23456",
            program_enabled=True,
            reward_amount=Decimal("2500.00"),
            reward_currency="NGN",
            synced_at=datetime.now(UTC),
        )
    )
    db.add(
        ReferralMirror(
            crm_referral_id=str(uuid.uuid4()),
            subscriber_id=sub.id,
            referred_name="Jane Friend",
            status="rewarded",
            reward_amount=Decimal("2500.00"),
            reward_currency="NGN",
            reward_status="issued",
            referral_created_at=datetime(2026, 6, 20, 10, 0, tzinfo=UTC),
            qualified_at=datetime(2026, 6, 25, 10, 0, tzinfo=UTC),
        )
    )
    db.commit()


def _native_referral(db, referrer):
    from tests.referral_program_testkit import capture, ensure_code

    code = ensure_code(db, referrer.id)
    referral = capture(
        db,
        code=code.code,
        name="Jane Friend",
        email=f"jane-{uuid.uuid4().hex[:8]}@example.com",
        source="portal",
    )
    referral.status = "rewarded"
    referral.reward_status = "issued"
    referral.reward_amount = Decimal("2500.00")
    referral.qualified_at = datetime(2026, 6, 25, 10, 0, tzinfo=UTC)
    db.commit()
    return referral


def test_me_referrals_native_matches_mirror_golden_payload(db_session):
    _program_settings(db_session)

    mirror_sub = _subscriber(db_session)
    _mirror_referrals(db_session, mirror_sub)
    mirror_out = referrals_mirror.read_for_subscriber(db_session, str(mirror_sub.id))

    native_sub = _subscriber(db_session)
    native_referral = _native_referral(db_session, native_sub)
    native_out = referrals_service.referrals.read_for_subscriber(
        db_session, str(native_sub.id)
    )

    _assert_same_shape(native_out, mirror_out)

    for out in (native_out, mirror_out):
        parsed = MyReferralsResponse.model_validate(out)
        assert parsed.program.enabled is True
        assert len(parsed.referrals) == 1
        ReferralItem.model_validate(out["referrals"][0])

    # §2.5: id is the referral UUID; native reward vocabulary (`issued`) is
    # already tolerated by mobile; amounts stay decimal strings.
    item = native_out["referrals"][0]
    assert item["id"] == str(native_referral.id)
    assert item["reward_status"] == "issued"
    assert isinstance(item["reward_amount"], str)
    assert native_out["totals"] == {
        "total": 1,
        "pending": 0,
        "qualified": 0,
        "rewarded": 1,
        "total_earned": "2500.00",
    }


# ── reseller aggregations ─────────────────────────────────────────────────────


def _reseller_with_customer(db):
    reseller = Reseller(name=f"Reseller {uuid.uuid4().hex[:6]}", is_active=True)
    db.add(reseller)
    db.commit()
    db.refresh(reseller)
    return reseller, _subscriber(db, reseller_id=reseller.id)


def test_reseller_quotes_native_matches_mirror_golden_payload(db_session):
    mirror_reseller, mirror_sub = _reseller_with_customer(db_session)
    _mirror_quote(db_session, mirror_sub)
    with patch.object(selfserve_service, "native_read_enabled", return_value=False):
        mirror_out = reseller_crm_views.quotes_for_reseller(
            db_session, str(mirror_reseller.id)
        )

    native_reseller, native_sub = _reseller_with_customer(db_session)
    _native_quote(db_session, native_sub)
    with patch.object(selfserve_service, "native_read_enabled", return_value=True):
        native_out = reseller_crm_views.quotes_for_reseller(
            db_session, str(native_reseller.id)
        )

    _assert_same_shape(native_out, mirror_out)
    assert native_out["total"] == 1 and mirror_out["total"] == 1
    item = native_out["quotes"][0]
    assert item["account_id"] == str(native_sub.id)
    assert isinstance(item["account_name"], str)
