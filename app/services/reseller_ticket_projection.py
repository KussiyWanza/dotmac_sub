"""Typed native Support projections for reseller-owned accounts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy.orm import Session

from app.schemas.status_presentation import StatusPresentation
from app.services import reseller_portal
from app.services import support as support_service
from app.services.status_presentation import ticket_status_presentation


@dataclass(frozen=True, slots=True)
class ResellerTicketCountQuery:
    reseller_id: UUID
    account_ids: tuple[UUID, ...]


@dataclass(frozen=True, slots=True)
class ResellerAccountTicketsQuery:
    reseller_id: UUID
    account_id: UUID
    limit: int = 200


@dataclass(frozen=True, slots=True)
class ResellerOpenTicketCount:
    value: int
    account_count: int


@dataclass(frozen=True, slots=True)
class ResellerTicketSummary:
    id: UUID
    ticket_number: str | None
    title: str
    status: str
    priority: str
    created_at: datetime
    updated_at: datetime
    status_presentation: StatusPresentation

    def to_dict(self) -> dict[str, object]:
        return {
            "id": str(self.id),
            "ticket_number": self.ticket_number,
            "subject": self.title,
            "title": self.title,
            "status": self.status,
            "priority": self.priority,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "status_presentation": self.status_presentation.model_dump(mode="json"),
        }


def native_open_ticket_count(
    db: Session,
    *,
    query: ResellerTicketCountQuery,
) -> ResellerOpenTicketCount:
    """Count non-terminal tickets for an explicitly reseller-owned cohort."""
    owned_account_ids = tuple(
        account_id
        for account_id in query.account_ids
        if reseller_portal.owned_account(
            db,
            str(query.reseller_id),
            str(account_id),
        )
        is not None
    )
    value = sum(
        support_service.tickets.count(
            db,
            subscriber_id=str(account_id),
            status_scope=support_service.TicketStatusScope.not_closed(),
        )
        for account_id in owned_account_ids
    )
    return ResellerOpenTicketCount(
        value=value,
        account_count=len(owned_account_ids),
    )


def native_account_ticket_summaries(
    db: Session,
    *,
    query: ResellerAccountTicketsQuery,
) -> tuple[ResellerTicketSummary, ...]:
    """Project native support tickets while enforcing reseller ownership."""
    if (
        reseller_portal.owned_account(
            db,
            str(query.reseller_id),
            str(query.account_id),
        )
        is None
    ):
        return ()
    tickets = support_service.tickets.list(
        db,
        subscriber_id=str(query.account_id),
        order_by="updated_at",
        order_dir="desc",
        limit=query.limit,
    )
    return tuple(
        ResellerTicketSummary(
            id=ticket.id,
            ticket_number=ticket.number,
            title=ticket.title,
            status=ticket.display_status,
            priority=ticket.priority,
            created_at=ticket.created_at,
            updated_at=ticket.updated_at,
            status_presentation=ticket_status_presentation(ticket.display_status),
        )
        for ticket in tickets
    )
