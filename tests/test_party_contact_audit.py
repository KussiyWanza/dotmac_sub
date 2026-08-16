from __future__ import annotations

import json
from pathlib import Path

from app.models.party import (
    PartyContactConsentStatus,
    PartyContactPointType,
    PartyContactVerificationStatus,
    PartyRelationshipType,
    PartyType,
)
from app.models.subscriber import Subscriber, SubscriberContact, SubscriberStatus
from app.models.team_inbox import InboxChannelType, InboxContactLink
from app.services import party as party_service
from app.services import team_inbox_contact_links
from app.services.party_contact_audit import build_party_contact_inbox_audit

_EVIDENCE = {
    "source": "reviewed_contact_projection_worklist",
    "reason": "Protected contact review evidence",
}


def test_contact_inbox_audit_reports_only_aggregate_convergence(db_session):
    private_name = "Private Linked Contact"
    private_email = "private-linked-contact@example.test"
    private_phone = "+2348030000999"
    account_party = party_service.create_party(
        db_session,
        party_type=PartyType.organization,
        display_name="Private Account Party",
    )
    person = party_service.create_party(
        db_session,
        party_type=PartyType.person,
        display_name=private_name,
    )
    subscriber = Subscriber(
        first_name="Private",
        last_name="Account",
        email="private-account@example.test",
        phone=private_phone,
        status=SubscriberStatus.active,
        is_active=True,
    )
    db_session.add(subscriber)
    db_session.flush()
    party_service.bind_subscriber_account(
        db_session,
        subscriber_id=subscriber.id,
        party_id=account_party.id,
        **_EVIDENCE,
    )
    contact = SubscriberContact(
        subscriber_id=subscriber.id,
        full_name=private_name,
        email=private_email,
        is_authorized=True,
        receives_notifications=True,
    )
    db_session.add(contact)
    db_session.flush()
    party_service.bind_subscriber_contact_person(
        db_session,
        subscriber_contact_id=contact.id,
        person_party_id=person.id,
        **_EVIDENCE,
    )
    relationship = party_service.relate_parties(
        db_session,
        subject_party_id=person.id,
        object_party_id=account_party.id,
        relationship_type=PartyRelationshipType.contact_for,
    )
    party_service.bind_subscriber_contact_relationship(
        db_session,
        subscriber_contact_id=contact.id,
        party_relationship_id=relationship.id,
        **_EVIDENCE,
    )
    point = party_service.add_contact_point(
        db_session,
        party_id=person.id,
        channel_type=PartyContactPointType.email,
        normalized_value=private_email,
        verification_status=PartyContactVerificationStatus.verified,
        consent_status=PartyContactConsentStatus.opted_out,
    )
    party_service.bind_subscriber_contact_point(
        db_session,
        subscriber_contact_id=contact.id,
        source_field="email",
        party_contact_point_id=point.id,
        **_EVIDENCE,
    )
    link = InboxContactLink(
        channel_type=InboxChannelType.email.value,
        normalized_contact=private_email,
        subscriber_id=subscriber.id,
        source="manual_inbox_conversation",
        is_active=True,
    )
    db_session.add(link)
    db_session.flush()
    team_inbox_contact_links.bind_contact_link_party_contact_point(
        db_session,
        contact_link_id=link.id,
        party_contact_point_id=point.id,
        **_EVIDENCE,
    )

    audit = build_party_contact_inbox_audit(db_session)
    serialized = json.dumps(audit, sort_keys=True)

    assert audit["status"] == "installed"
    assert audit["subscriber_contact_people"]["aligned"] == 1
    assert audit["relationship_projections"]["aligned"] == 1
    assert (
        audit["relationship_projections"][
            "bound_contacts_without_relationship_projection"
        ]
        == 0
    )
    assert audit["contact_point_projections"]["aligned"] == 1
    assert audit["contact_point_projections"]["projected_fields"]["email"] == 1
    assert audit["canonical_contact_points"]["verification_status"] == {"verified": 1}
    assert audit["canonical_contact_points"]["consent_status"] == {"opted_out": 1}
    assert audit["inbox_contact_point_projections"]["aligned"] == 1
    assert audit["artifact_contract"] == {
        "read_only": True,
        "contains_identity_values": False,
        "automatic_party_binding": False,
        "automatic_relationship_creation": False,
        "automatic_contact_point_creation": False,
        "automatic_inbox_routing": False,
        "changes_verification_or_consent": False,
        "changes_authentication_or_authorization": False,
    }
    assert private_name not in serialized
    assert private_email not in serialized
    assert private_phone not in serialized
    assert str(person.id) not in serialized


def test_contact_inbox_audit_reports_unbound_and_unsupported_debt(db_session):
    subscriber = Subscriber(
        first_name="Unbound",
        last_name="Account",
        email="unbound-account@example.test",
        status=SubscriberStatus.active,
        is_active=True,
    )
    db_session.add(subscriber)
    db_session.flush()
    contact = SubscriberContact(
        subscriber_id=subscriber.id,
        full_name="Unbound Contact",
        email="unbound-contact@example.test",
        other_social="unsupported-social-value",
    )
    link = InboxContactLink(
        channel_type=InboxChannelType.chat_widget.value,
        normalized_contact="opaque-widget-value",
        subscriber_id=subscriber.id,
        source="manual_inbox_conversation",
        is_active=True,
    )
    db_session.add_all((contact, link))
    db_session.flush()

    audit = build_party_contact_inbox_audit(db_session)

    assert audit["subscriber_contact_people"]["unbound"] == 1
    assert audit["contact_point_projections"]["legacy_populated_fields"]["email"] == 1
    assert audit["contact_point_projections"]["other_social_populated"] == 1
    assert audit["inbox_contact_point_projections"]["active_unbound"] == 1
    assert audit["inbox_contact_point_projections"]["unsupported_channel"] == 1
    assert audit["inbox_contact_point_projections"]["missing_target_party_binding"] == 1


def test_audit_party_contact_inbox_uses_the_read_only_snapshot_seam():
    """Behavioural proof lives on PostgreSQL; this pins the wiring."""

    source = Path("scripts/migration/audit_party_contact_inbox.py").read_text(
        encoding="utf-8"
    )

    assert "read_only_snapshot_session" in source
    assert "SET TRANSACTION" not in source
