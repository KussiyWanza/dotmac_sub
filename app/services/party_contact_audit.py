"""Read-only convergence audit for linked contacts and Team Inbox routing.

Only schema state and aggregate counts leave this module. Names, addresses,
phone numbers, social identifiers, UUIDs, notes, evidence text, verification
details, and consent details are never emitted or changed.
"""

from __future__ import annotations

from collections import Counter
from typing import Any
from uuid import UUID

from sqlalchemy import inspect
from sqlalchemy.orm import Session

from app.models.party import (
    Party,
    PartyContactPoint,
    PartyContactPointType,
    PartyIdentityStatus,
    PartyRelationship,
    PartyRelationshipStatus,
    PartyRelationshipType,
    PartyType,
    SubscriberContactPointProjection,
    SubscriberContactRelationshipProjection,
)
from app.models.subscriber import Reseller, Subscriber, SubscriberContact
from app.models.team_inbox import InboxContactLink
from app.services.customer_identity_normalization import (
    normalize_email_identifier,
    normalize_phone_identifier,
)
from app.services.team_inbox_contact_links import normalize_contact_address

_EVIDENCE_COLUMNS = {
    "party_bound_at",
    "party_binding_source",
    "party_binding_reason",
}
_INBOX_EVIDENCE_COLUMNS = {
    "party_contact_point_bound_at",
    "party_contact_point_binding_source",
    "party_contact_point_binding_reason",
}
_REQUIRED_COLUMNS = {
    "subscriber_contacts": {"person_party_id"} | _EVIDENCE_COLUMNS,
    "subscriber_contact_relationship_projections": {
        "subscriber_contact_id",
        "party_relationship_id",
        "bound_at",
        "binding_source",
        "binding_reason",
    },
    "subscriber_contact_point_projections": {
        "subscriber_contact_id",
        "source_field",
        "party_contact_point_id",
        "bound_at",
        "binding_source",
        "binding_reason",
    },
    "inbox_contact_links": {"party_contact_point_id"} | _INBOX_EVIDENCE_COLUMNS,
}
_CONTACT_SOURCE_CHANNELS = {
    "email": PartyContactPointType.email.value,
    "phone": PartyContactPointType.phone.value,
    "whatsapp": PartyContactPointType.whatsapp.value,
    "facebook": PartyContactPointType.facebook_messenger.value,
    "instagram": PartyContactPointType.instagram_dm.value,
    "x_handle": PartyContactPointType.x.value,
    "telegram": PartyContactPointType.telegram.value,
    "linkedin": PartyContactPointType.linkedin.value,
}
_SOCIAL_CHANNELS = {
    PartyContactPointType.facebook_messenger.value,
    PartyContactPointType.instagram_dm.value,
    PartyContactPointType.telegram.value,
    PartyContactPointType.linkedin.value,
    PartyContactPointType.x.value,
}
_INBOX_CHANNELS = {
    "email": PartyContactPointType.email.value,
    "whatsapp": PartyContactPointType.whatsapp.value,
    "facebook_messenger": PartyContactPointType.facebook_messenger.value,
    "instagram_dm": PartyContactPointType.instagram_dm.value,
}
_CONTACT_RELATIONSHIPS = {
    PartyRelationshipType.contact_for.value,
    PartyRelationshipType.billing_contact_for.value,
    PartyRelationshipType.technical_contact_for.value,
    PartyRelationshipType.emergency_contact_for.value,
}
_CURRENT_RELATIONSHIP_STATUSES = {
    PartyRelationshipStatus.pending.value,
    PartyRelationshipStatus.active.value,
}
_ROUTABLE_PARTY_STATUSES = {
    PartyIdentityStatus.active.value,
    PartyIdentityStatus.quarantined.value,
}


def _complete(values: tuple[Any, ...]) -> bool:
    timestamp, source, reason = values
    return bool(
        timestamp is not None
        and str(source or "").strip()
        and str(reason or "").strip()
    )


def _any(values: tuple[Any, ...]) -> bool:
    return any(value is not None for value in values)


def _binding_evidence(row: Any) -> tuple[Any, ...]:
    return (
        row.party_bound_at,
        row.party_binding_source,
        row.party_binding_reason,
    )


def _inbox_evidence(row: Any) -> tuple[Any, ...]:
    return (
        row.party_contact_point_bound_at,
        row.party_contact_point_binding_source,
        row.party_contact_point_binding_reason,
    )


def _projection_evidence(row: Any) -> tuple[Any, ...]:
    return row.bound_at, row.binding_source, row.binding_reason


def _normalized_legacy_value(source_field: str, value: str | None) -> str | None:
    if source_field == "email":
        return normalize_email_identifier(value)
    if source_field in {"phone", "whatsapp"}:
        return normalize_phone_identifier(value)
    normalized = str(value or "").strip().casefold()
    return normalized or None


def _social_scope_complete(point: PartyContactPoint) -> bool:
    return bool(
        str(point.provider or "").strip()
        and str(point.provider_account_id or "").strip()
        and str(point.external_subject_id or "").strip()
    )


def _subscriber_contact_counts(
    contacts: list[SubscriberContact],
    *,
    parties: dict[UUID, tuple[str, str]],
    subscribers: dict[UUID, UUID | None],
) -> dict[str, int]:
    counts = {
        "total": len(contacts),
        "bound": 0,
        "unbound": 0,
        "incomplete_evidence": 0,
        "missing_or_non_person_party": 0,
        "nonroutable_person_party": 0,
        "missing_subscriber_account_party": 0,
        "nonroutable_subscriber_account_party": 0,
        "aligned": 0,
    }
    for contact in contacts:
        evidence = _binding_evidence(contact)
        if contact.person_party_id is None:
            counts["unbound"] += 1
            if _any(evidence):
                counts["incomplete_evidence"] += 1
            continue
        counts["bound"] += 1
        aligned = True
        if not _complete(evidence):
            counts["incomplete_evidence"] += 1
            aligned = False
        party = parties.get(contact.person_party_id)
        if party is None or party[0] != PartyType.person.value:
            counts["missing_or_non_person_party"] += 1
            aligned = False
        elif party[1] not in _ROUTABLE_PARTY_STATUSES:
            counts["nonroutable_person_party"] += 1
            aligned = False
        account_party_id = subscribers.get(contact.subscriber_id)
        if account_party_id is None or account_party_id not in parties:
            counts["missing_subscriber_account_party"] += 1
            aligned = False
        elif parties[account_party_id][1] not in _ROUTABLE_PARTY_STATUSES:
            counts["nonroutable_subscriber_account_party"] += 1
            aligned = False
        if aligned:
            counts["aligned"] += 1
    return counts


def _relationship_projection_counts(
    rows: list[SubscriberContactRelationshipProjection],
    *,
    contacts: dict[UUID, SubscriberContact],
    subscribers: dict[UUID, UUID | None],
    relationships: dict[UUID, PartyRelationship],
) -> dict[str, int]:
    counts = {
        "total": len(rows),
        "incomplete_evidence": 0,
        "missing_or_unbound_contact": 0,
        "missing_relationship": 0,
        "wrong_person_endpoint": 0,
        "wrong_account_endpoint": 0,
        "unsupported_relationship_type": 0,
        "non_current_relationship": 0,
        "aligned": 0,
    }
    for row in rows:
        issues: set[str] = set()
        if not _complete(_projection_evidence(row)):
            issues.add("incomplete_evidence")
        contact = contacts.get(row.subscriber_contact_id)
        if contact is None or contact.person_party_id is None:
            issues.add("missing_or_unbound_contact")
        relationship = relationships.get(row.party_relationship_id)
        if relationship is None:
            issues.add("missing_relationship")
        if contact is not None and relationship is not None:
            if relationship.subject_party_id != contact.person_party_id:
                issues.add("wrong_person_endpoint")
            account_party_id = subscribers.get(contact.subscriber_id)
            if (
                account_party_id is None
                or relationship.object_party_id != account_party_id
            ):
                issues.add("wrong_account_endpoint")
            if relationship.relationship_type not in _CONTACT_RELATIONSHIPS:
                issues.add("unsupported_relationship_type")
            if relationship.status not in _CURRENT_RELATIONSHIP_STATUSES:
                issues.add("non_current_relationship")
        for issue in issues:
            counts[issue] += 1
        if not issues:
            counts["aligned"] += 1
    projected_contact_ids = {row.subscriber_contact_id for row in rows}
    counts["bound_contacts_without_relationship_projection"] = sum(
        1
        for contact in contacts.values()
        if contact.person_party_id is not None
        and contact.id not in projected_contact_ids
    )
    return counts


def _contact_point_projection_counts(
    rows: list[SubscriberContactPointProjection],
    *,
    contacts: dict[UUID, SubscriberContact],
    parties: dict[UUID, tuple[str, str]],
    points: dict[UUID, PartyContactPoint],
) -> dict[str, Any]:
    counts: dict[str, Any] = {
        "total": len(rows),
        "incomplete_evidence": 0,
        "missing_or_unbound_contact": 0,
        "missing_contact_point": 0,
        "unsupported_source_field": 0,
        "source_value_missing": 0,
        "wrong_person_party": 0,
        "nonroutable_person_party": 0,
        "channel_mismatch": 0,
        "normalized_value_mismatch": 0,
        "inactive_contact_point": 0,
        "social_scope_missing": 0,
        "aligned": 0,
    }
    projected_by_field: Counter[str] = Counter()
    for row in rows:
        issues: set[str] = set()
        projected_by_field[row.source_field] += 1
        if not _complete(_projection_evidence(row)):
            issues.add("incomplete_evidence")
        contact = contacts.get(row.subscriber_contact_id)
        if contact is None or contact.person_party_id is None:
            issues.add("missing_or_unbound_contact")
        point = points.get(row.party_contact_point_id)
        if point is None:
            issues.add("missing_contact_point")
        expected_channel = _CONTACT_SOURCE_CHANNELS.get(row.source_field)
        if expected_channel is None:
            issues.add("unsupported_source_field")
        if contact is not None and point is not None and expected_channel is not None:
            legacy_value = _normalized_legacy_value(
                row.source_field, getattr(contact, row.source_field, None)
            )
            if legacy_value is None:
                issues.add("source_value_missing")
            if point.party_id != contact.person_party_id:
                issues.add("wrong_person_party")
            point_party = parties.get(point.party_id)
            if point_party is None or point_party[1] not in _ROUTABLE_PARTY_STATUSES:
                issues.add("nonroutable_person_party")
            if point.channel_type != expected_channel:
                issues.add("channel_mismatch")
            point_values = {
                value
                for value in (
                    _normalized_legacy_value(row.source_field, point.normalized_value),
                    _normalized_legacy_value(row.source_field, point.display_value),
                )
                if value
            }
            if legacy_value is not None and legacy_value not in point_values:
                issues.add("normalized_value_mismatch")
            if not point.is_active:
                issues.add("inactive_contact_point")
            if point.channel_type in _SOCIAL_CHANNELS and not _social_scope_complete(
                point
            ):
                issues.add("social_scope_missing")
        for issue in issues:
            counts[issue] += 1
        if not issues:
            counts["aligned"] += 1

    populated_by_field = {
        source_field: sum(
            1
            for contact in contacts.values()
            if str(getattr(contact, source_field, None) or "").strip()
        )
        for source_field in _CONTACT_SOURCE_CHANNELS
    }
    counts["legacy_populated_fields"] = populated_by_field
    counts["projected_fields"] = {
        source_field: projected_by_field[source_field]
        for source_field in _CONTACT_SOURCE_CHANNELS
    }
    counts["other_social_populated"] = sum(
        1 for contact in contacts.values() if str(contact.other_social or "").strip()
    )
    return counts


def _canonical_contact_point_counts(
    points: dict[UUID, PartyContactPoint],
) -> dict[str, Any]:
    verification = Counter(point.verification_status for point in points.values())
    consent = Counter(point.consent_status for point in points.values())
    return {
        "total": len(points),
        "active": sum(1 for point in points.values() if point.is_active),
        "inactive": sum(1 for point in points.values() if not point.is_active),
        "verification_status": dict(sorted(verification.items())),
        "consent_status": dict(sorted(consent.items())),
        "social_points_missing_scope": sum(
            1
            for point in points.values()
            if point.channel_type in _SOCIAL_CHANNELS
            and not _social_scope_complete(point)
        ),
    }


def _inbox_projection_counts(
    db: Session,
    links: list[InboxContactLink],
    *,
    parties: dict[UUID, tuple[str, str]],
    points: dict[UUID, PartyContactPoint],
    subscribers: dict[UUID, UUID | None],
    resellers: dict[UUID, UUID | None],
    relationships: list[PartyRelationship],
) -> dict[str, int]:
    counts = {
        "total": len(links),
        "active": sum(1 for link in links if link.is_active),
        "bound": 0,
        "unbound": 0,
        "active_unbound": 0,
        "incomplete_evidence": 0,
        "unsupported_channel": 0,
        "missing_target_party_binding": 0,
        "missing_or_unroutable_target_party": 0,
        "missing_or_inactive_contact_point": 0,
        "missing_or_unroutable_contact_party": 0,
        "channel_mismatch": 0,
        "normalized_contact_mismatch": 0,
        "social_scope_missing": 0,
        "no_active_contact_relationship": 0,
        "aligned": 0,
    }
    active_relationships = {
        (row.subject_party_id, row.object_party_id)
        for row in relationships
        if row.relationship_type in _CONTACT_RELATIONSHIPS
        and row.status == PartyRelationshipStatus.active.value
    }
    for link in links:
        issues: set[str] = set()
        evidence = _inbox_evidence(link)
        if link.party_contact_point_id is None:
            counts["unbound"] += 1
            if link.is_active:
                counts["active_unbound"] += 1
            if _any(evidence):
                issues.add("incomplete_evidence")
        else:
            counts["bound"] += 1
            if not _complete(evidence):
                issues.add("incomplete_evidence")
        expected_channel = _INBOX_CHANNELS.get(link.channel_type)
        if expected_channel is None:
            issues.add("unsupported_channel")
        target_party_id = None
        if link.subscriber_id is not None:
            target_party_id = subscribers.get(link.subscriber_id)
        elif link.reseller_id is not None:
            target_party_id = resellers.get(link.reseller_id)
        if target_party_id is None:
            issues.add("missing_target_party_binding")
        else:
            target_party = parties.get(target_party_id)
            if target_party is None or target_party[1] not in _ROUTABLE_PARTY_STATUSES:
                issues.add("missing_or_unroutable_target_party")
        point = (
            points.get(link.party_contact_point_id)
            if link.party_contact_point_id
            else None
        )
        if link.party_contact_point_id is not None and (
            point is None or not point.is_active
        ):
            issues.add("missing_or_inactive_contact_point")
        if point is not None:
            point_party = parties.get(point.party_id)
            if point_party is None or point_party[1] not in _ROUTABLE_PARTY_STATUSES:
                issues.add("missing_or_unroutable_contact_party")
            if expected_channel is not None and point.channel_type != expected_channel:
                issues.add("channel_mismatch")
            normalized_values = {
                value
                for value in (
                    normalize_contact_address(
                        db, link.channel_type, point.normalized_value
                    ),
                    normalize_contact_address(
                        db, link.channel_type, point.external_subject_id
                    ),
                )
                if value
            }
            if link.normalized_contact not in normalized_values:
                issues.add("normalized_contact_mismatch")
            if point.channel_type in _SOCIAL_CHANNELS and not _social_scope_complete(
                point
            ):
                issues.add("social_scope_missing")
            if (
                target_party_id is not None
                and point.party_id != target_party_id
                and (point.party_id, target_party_id) not in active_relationships
            ):
                issues.add("no_active_contact_relationship")
        for issue in issues:
            counts[issue] += 1
        if link.party_contact_point_id is not None and not issues:
            counts["aligned"] += 1
    return counts


def build_party_contact_inbox_audit(db: Session) -> dict[str, Any]:
    """Return PII-free contact identity and Inbox projection convergence."""

    inspector = inspect(db.get_bind())
    installed_tables = set(inspector.get_table_names())
    missing_tables = sorted(set(_REQUIRED_COLUMNS) - installed_tables)
    if missing_tables:
        return _not_installed(missing_tables=missing_tables)
    missing_columns = {
        table_name: sorted(
            required - {column["name"] for column in inspector.get_columns(table_name)}
        )
        for table_name, required in _REQUIRED_COLUMNS.items()
    }
    missing_columns = {
        table_name: columns
        for table_name, columns in missing_columns.items()
        if columns
    }
    if missing_columns:
        return _not_installed(missing_columns=missing_columns)

    parties = {
        row.id: (row.party_type, row.status)
        for row in db.query(Party.id, Party.party_type, Party.status).all()
    }
    subscribers = {
        row.id: row.party_id
        for row in db.query(Subscriber.id, Subscriber.party_id).all()
    }
    resellers = {
        row.id: row.party_id for row in db.query(Reseller.id, Reseller.party_id).all()
    }
    contact_rows = db.query(SubscriberContact).all()
    contacts = {row.id: row for row in contact_rows}
    relationship_rows = db.query(PartyRelationship).all()
    relationships = {row.id: row for row in relationship_rows}
    points = {row.id: row for row in db.query(PartyContactPoint).all()}
    relationship_projections = db.query(SubscriberContactRelationshipProjection).all()
    point_projections = db.query(SubscriberContactPointProjection).all()
    inbox_links = db.query(InboxContactLink).all()

    return {
        "status": "installed",
        "subscriber_contact_people": _subscriber_contact_counts(
            contact_rows,
            parties=parties,
            subscribers=subscribers,
        ),
        "relationship_projections": _relationship_projection_counts(
            relationship_projections,
            contacts=contacts,
            subscribers=subscribers,
            relationships=relationships,
        ),
        "contact_point_projections": _contact_point_projection_counts(
            point_projections,
            contacts=contacts,
            parties=parties,
            points=points,
        ),
        "canonical_contact_points": _canonical_contact_point_counts(points),
        "inbox_contact_point_projections": _inbox_projection_counts(
            db,
            inbox_links,
            parties=parties,
            points=points,
            subscribers=subscribers,
            resellers=resellers,
            relationships=relationship_rows,
        ),
        "artifact_contract": _artifact_contract(),
    }


def _artifact_contract() -> dict[str, bool]:
    return {
        "read_only": True,
        "contains_identity_values": False,
        "automatic_party_binding": False,
        "automatic_relationship_creation": False,
        "automatic_contact_point_creation": False,
        "automatic_inbox_routing": False,
        "changes_verification_or_consent": False,
        "changes_authentication_or_authorization": False,
    }


def _not_installed(**details: Any) -> dict[str, Any]:
    return {
        "status": "not_installed",
        **details,
        "artifact_contract": _artifact_contract(),
    }
