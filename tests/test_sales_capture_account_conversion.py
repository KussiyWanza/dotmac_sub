"""Provider-neutral lead capture and exact account-conversion contracts."""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.models.party import PartyRole, PartyRoleStatus, PartyRoleType
from app.models.sales import LeadOriginCaptureImmutableError
from app.models.subscriber import Subscriber
from app.schemas.sales import (
    LeadCapturePartyCreate,
    LeadCaptureRequest,
    LeadOriginCaptureCreate,
)
from app.schemas.subscriber import SubscriberCreate
from app.services.sales import account_conversion, capture


def _request(interaction_id: str, *, title: str = "Abuja fiber enquiry"):
    return LeadCaptureRequest(
        party=LeadCapturePartyCreate(
            display_name="Captured Prospect",
            contacts=[],
        ),
        title=title,
        lead_source="Website",
        origin=LeadOriginCaptureCreate(
            capture_method="landing_page",
            source_platform="website",
            source_interaction_id=interaction_id,
            landing_path="/fiber/abuja",
            capture_source="signed_landing_adapter",
            capture_reason="Signed interaction submitted to canonical capture",
        ),
        region="FCT",
    )


def test_capture_is_party_first_and_exact_replay_is_idempotent(db_session):
    payload = _request("landing-delivery-1")

    first = capture.capture_lead(db_session, payload, actor_id="pytest")
    replay = capture.capture_lead(db_session, payload, actor_id="pytest")

    assert replay.replayed is True
    assert replay.lead.id == first.lead.id
    assert replay.origin.id == first.origin.id
    assert db_session.query(Subscriber).count() == 0
    assert first.origin.source_interaction_id == "landing-delivery-1"
    role = db_session.query(PartyRole).one()
    assert role.role_type == PartyRoleType.prospect.value
    assert role.status == PartyRoleStatus.active.value


def test_capture_rejects_different_content_under_same_source_identity(db_session):
    capture.capture_lead(
        db_session, _request("landing-delivery-collision"), actor_id="pytest"
    )

    with pytest.raises(capture.LeadCaptureError) as exc:
        capture.capture_lead(
            db_session,
            _request("landing-delivery-collision", title="Different enquiry"),
            actor_id="pytest",
        )

    assert exc.value.code == "source_interaction_collision"


def test_capture_origin_is_append_only_in_the_orm(db_session):
    result = capture.capture_lead(
        db_session, _request("landing-delivery-immutable"), actor_id="pytest"
    )
    result.origin.utm_campaign = "later-edit"

    with pytest.raises(LeadOriginCaptureImmutableError, match="append-only"):
        db_session.flush()
    db_session.rollback()


def test_quote_acceptance_participant_creates_customer_and_pending_subscriber_roles(
    db_session,
):
    captured = capture.capture_lead(
        db_session, _request("landing-delivery-convert"), actor_id="pytest"
    )
    account = SubscriberCreate(
        first_name="Captured",
        last_name="Customer",
        email=f"captured-{uuid4().hex}@example.com",
    )

    result = account_conversion.stage_lead_account_conversion(
        db_session,
        lead_id=captured.lead.id,
        party_id=captured.lead.party_id,
        new_account=account,
        actor_id="pytest",
    )
    replay = account_conversion.stage_lead_account_conversion(
        db_session,
        lead_id=captured.lead.id,
        party_id=captured.lead.party_id,
        new_account=account,
        actor_id="pytest",
    )

    assert result.outcome == "created"
    assert replay.outcome == "already_attached"
    assert replay.subscriber_id == result.subscriber_id
    subscriber = db_session.get(Subscriber, result.subscriber_id)
    assert subscriber.party_id == captured.lead.party_id
    assert captured.lead.subscriber_id == subscriber.id
    roles = {
        row.role_type: row.status
        for row in db_session.query(PartyRole)
        .filter(PartyRole.party_id == captured.lead.party_id)
        .all()
    }
    assert roles[PartyRoleType.customer.value] == PartyRoleStatus.active.value
    assert roles[PartyRoleType.subscriber.value] == PartyRoleStatus.pending.value


def test_account_conversion_refuses_a_different_party(db_session):
    captured = capture.capture_lead(
        db_session, _request("landing-delivery-wrong-party"), actor_id="pytest"
    )

    with pytest.raises(account_conversion.LeadAccountConversionError) as exc:
        account_conversion.stage_lead_account_conversion(
            db_session,
            lead_id=captured.lead.id,
            party_id=uuid4(),
            new_account=SubscriberCreate(
                first_name="Wrong",
                last_name="Party",
                email=f"wrong-{uuid4().hex}@example.com",
            ),
            actor_id="pytest",
        )

    assert exc.value.code == "party_mismatch"
