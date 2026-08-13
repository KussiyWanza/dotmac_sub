"""Participant resolver for quoting against an existing customer account."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models.party import PartyIdentityStatus
from app.models.sales import CustomerQuoteLeadLink, Lead, LeadStatus
from app.models.subscriber import Subscriber
from app.services.domain_errors import DomainError


class CustomerQuoteLinkageError(DomainError):
    """Stable customer recipient resolution failure."""


@dataclass(frozen=True, slots=True)
class CustomerQuoteLeadResolution:
    customer_id: UUID
    lead_id: UUID


def _error(suffix: str, message: str) -> CustomerQuoteLinkageError:
    return CustomerQuoteLinkageError(
        code=f"sales.customer_quote_linkage.{suffix}", message=message, details={}
    )


def resolve_customer_quote_lead(
    db: Session, *, customer_id: UUID
) -> CustomerQuoteLeadResolution:
    """Lock an eligible customer and reuse or stage its dedicated Quote Lead.

    This is a flush-only participant called by ``sales.quote_authoring``. The
    unique customer link is the durable concurrency arbiter; no adapter may
    create or select this system Lead directly.
    """

    customer = db.scalars(
        select(Subscriber)
        .where(Subscriber.id == customer_id)
        .options(selectinload(Subscriber.party))
        .with_for_update()
    ).one_or_none()
    if customer is None:
        raise _error("customer_not_found", "Select a valid customer account.")
    party = customer.party
    if (
        not customer.is_active
        or customer.party_id is None
        or customer.party_bound_at is None
        or not customer.party_binding_source
        or not customer.party_binding_reason
        or party is None
        or party.status != PartyIdentityStatus.active.value
    ):
        raise _error(
            "customer_not_eligible",
            "Select an active customer with a reviewed Party binding.",
        )

    link = db.scalars(
        select(CustomerQuoteLeadLink)
        .where(CustomerQuoteLeadLink.subscriber_id == customer.id)
        .with_for_update()
    ).one_or_none()
    if link is not None:
        return CustomerQuoteLeadResolution(
            customer_id=customer.id, lead_id=link.lead_id
        )

    now = datetime.now(UTC)
    lead = Lead(
        party_id=customer.party_id,
        party_bound_at=now,
        party_binding_source="customer_quote_linkage",
        party_binding_reason="Dedicated reusable Lead for customer-backed Quotes",
        subscriber_id=customer.id,
        subscriber_linked_at=now,
        subscriber_link_source="customer_quote_linkage",
        subscriber_link_reason="Existing customer account selected for Quote authoring",
        title=f"Customer quote — {party.display_name}"[:200],
        status=LeadStatus.qualified.value,
        lead_source="Customer account",
        is_active=True,
    )
    db.add(lead)
    db.flush()
    db.add(CustomerQuoteLeadLink(subscriber_id=customer.id, lead_id=lead.id))
    db.flush()
    return CustomerQuoteLeadResolution(customer_id=customer.id, lead_id=lead.id)
