"""Atomic, transport-neutral Lead/Party to Subscriber account conversion."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.party import PartyRoleStatus, PartyRoleType
from app.models.sales import Lead
from app.models.subscriber import Subscriber
from app.schemas.subscriber import SubscriberCreate
from app.services import party as party_service
from app.services import subscriber as subscriber_service
from app.services.domain_errors import DomainError
from app.services.events import EventType, emit_event
from app.services.sales import lifecycle

AccountConversionOutcome = Literal["created", "attached", "already_attached"]


class LeadAccountConversionError(DomainError):
    def __init__(self, code: str, message: str, *, kind: str = "conflict") -> None:
        super().__init__(code=code, message=message)
        self.kind = kind


@dataclass(frozen=True)
class LeadAccountConversionResult:
    lead_id: UUID
    party_id: UUID
    subscriber_id: UUID
    outcome: AccountConversionOutcome


def _lock_lead(db: Session, lead_id: UUID, party_id: UUID) -> Lead:
    lead = db.scalars(
        select(Lead).where(Lead.id == lead_id).with_for_update()
    ).one_or_none()
    if lead is None:
        raise LeadAccountConversionError(
            "lead_not_found", "Lead not found", kind="not_found"
        )
    if lead.party_id != party_id:
        raise LeadAccountConversionError(
            "party_mismatch",
            "Lead does not belong to the supplied Party",
            kind="invalid",
        )
    return lead


def _establish_roles(db: Session, party_id: UUID) -> None:
    party_service.ensure_role(
        db,
        party_id=party_id,
        role_type=PartyRoleType.customer,
        status=PartyRoleStatus.active,
        source="sales.account_conversion",
    )
    party_service.ensure_role(
        db,
        party_id=party_id,
        role_type=PartyRoleType.subscriber,
        status=PartyRoleStatus.pending,
        source="sales.account_conversion",
    )


def stage_lead_account_conversion(
    db: Session,
    *,
    lead_id: UUID,
    party_id: UUID,
    actor_id: str,
    subscriber_id: UUID | None = None,
    new_account: SubscriberCreate | None = None,
) -> LeadAccountConversionResult:
    """Stage one exact conversion in the caller's transaction without commit."""

    actor = str(actor_id or "").strip()
    if not actor:
        raise LeadAccountConversionError(
            "actor_required", "Conversion actor is required", kind="invalid"
        )
    if (subscriber_id is None) == (new_account is None):
        raise LeadAccountConversionError(
            "account_target_required",
            "Supply exactly one existing Subscriber or new account payload",
            kind="invalid",
        )
    lead = _lock_lead(db, lead_id, party_id)
    if lead.subscriber_id is not None:
        subscriber = db.get(Subscriber, lead.subscriber_id)
        if subscriber is None or subscriber.party_id != party_id:
            raise LeadAccountConversionError(
                "existing_account_mismatch",
                "Lead account link does not match its canonical Party",
            )
        outcome: AccountConversionOutcome = "already_attached"
    elif subscriber_id is not None:
        subscriber = db.scalars(
            select(Subscriber).where(Subscriber.id == subscriber_id).with_for_update()
        ).one_or_none()
        if subscriber is None:
            raise LeadAccountConversionError(
                "subscriber_not_found", "Subscriber not found", kind="not_found"
            )
        party_service.bind_subscriber_account(
            db,
            subscriber_id=subscriber.id,
            party_id=party_id,
            source="sales.account_conversion",
            reason="Reviewed Lead account attachment",
        )
        outcome = "attached"
    else:
        assert new_account is not None
        if new_account.person_id is not None:
            raise LeadAccountConversionError(
                "existing_target_not_allowed",
                "Use subscriber_id to attach an existing account",
                kind="invalid",
            )
        subscriber = subscriber_service.subscribers.prepare_new_account(db, new_account)
        party_service.bind_subscriber_account(
            db,
            subscriber_id=subscriber.id,
            party_id=party_id,
            source="sales.account_conversion",
            reason="Account created from the exact reviewed Lead and Party context",
        )
        emit_event(
            db,
            EventType.subscriber_created,
            {
                "subscriber_id": str(subscriber.id),
                "subscriber_number": subscriber.subscriber_number,
                "lead_id": str(lead.id),
            },
            actor=actor,
            subscriber_id=subscriber.id,
        )
        outcome = "created"

    lifecycle.attach_lead_subscriber(
        db,
        lead_id=lead.id,
        subscriber_id=subscriber.id,
        source="sales.account_conversion",
        reason="Exact Lead Party converted to customer account",
    )
    _establish_roles(db, party_id)
    if outcome != "already_attached":
        emit_event(
            db,
            EventType.lead_account_converted,
            {
                "lead_id": str(lead.id),
                "party_id": str(party_id),
                "subscriber_id": str(subscriber.id),
                "outcome": outcome,
            },
            actor=actor,
            subscriber_id=subscriber.id,
        )
    db.flush()
    return LeadAccountConversionResult(
        lead_id=lead.id,
        party_id=party_id,
        subscriber_id=subscriber.id,
        outcome=outcome,
    )
