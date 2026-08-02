"""Incident-to-ticket link composition for ``network.outage_lifecycle``.

One canonical infrastructure ticket per incident, any number of linked
complaint tickets — typed rows superseding the ``crm_ticket_id``
placeholder. The lifecycle owner records relationships and reconciliation
state only: ticket transitions stay with the Support owner, and network
recovery never closes a Ticket or WorkOrder from here (the boundary the
lifecycle chain tests already pin).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.models.network_monitoring import OutageIncident, OutageIncidentTicketLink

logger = logging.getLogger(__name__)

INFRASTRUCTURE_ROLE = "infrastructure"
COMPLAINT_ROLE = "complaint"
_RECONCILIATION_STATES = ("native", "pending", "synced", "drift")


def _current_revision_sequence(
    session: Session, incident: OutageIncident
) -> int | None:
    from app.services.topology.outage import latest_scope_revision

    revision = latest_scope_revision(session, incident.id)
    return revision.sequence if revision is not None else None


def links_for_incident(session: Session, incident_id) -> list[OutageIncidentTicketLink]:
    return (
        session.query(OutageIncidentTicketLink)
        .filter(OutageIncidentTicketLink.incident_id == incident_id)
        .order_by(OutageIncidentTicketLink.linked_at)
        .all()
    )


def infrastructure_link_for(
    session: Session, incident_id
) -> OutageIncidentTicketLink | None:
    return (
        session.query(OutageIncidentTicketLink)
        .filter(
            OutageIncidentTicketLink.incident_id == incident_id,
            OutageIncidentTicketLink.role == INFRASTRUCTURE_ROLE,
        )
        .first()
    )


def link_infrastructure_ticket(
    session: Session,
    incident: OutageIncident,
    ticket_id,
    *,
    linked_by: str | None,
    source: str = "operator",
    external_ref: str | None = None,
) -> OutageIncidentTicketLink:
    """Bind the ONE canonical infrastructure ticket.

    Idempotent for the same ticket; a different ticket while one is already
    bound raises — replacing the canonical ticket is an explicit reviewed
    operation, never a silent overwrite.
    """

    existing = infrastructure_link_for(session, incident.id)
    if existing is not None:
        if existing.ticket_id == ticket_id:
            return existing
        raise ValueError(
            f"incident {incident.id} already has canonical infrastructure "
            f"ticket {existing.ticket_id}; unlink it explicitly before "
            "binding another"
        )
    link = OutageIncidentTicketLink(
        incident_id=incident.id,
        ticket_id=ticket_id,
        role=INFRASTRUCTURE_ROLE,
        linked_at=datetime.now(UTC),
        linked_by=linked_by,
        source=source,
        external_ref=external_ref,
        scope_revision_sequence=_current_revision_sequence(session, incident),
    )
    session.add(link)
    session.flush()
    return link


def link_complaint_ticket(
    session: Session,
    incident: OutageIncident,
    ticket_id,
    *,
    linked_by: str | None,
    source: str = "operator",
) -> OutageIncidentTicketLink:
    """Attach one customer complaint ticket; the pair is deduplicated."""

    existing = (
        session.query(OutageIncidentTicketLink)
        .filter(
            OutageIncidentTicketLink.incident_id == incident.id,
            OutageIncidentTicketLink.ticket_id == ticket_id,
        )
        .first()
    )
    if existing is not None:
        return existing
    link = OutageIncidentTicketLink(
        incident_id=incident.id,
        ticket_id=ticket_id,
        role=COMPLAINT_ROLE,
        linked_at=datetime.now(UTC),
        linked_by=linked_by,
        source=source,
        scope_revision_sequence=_current_revision_sequence(session, incident),
    )
    session.add(link)
    session.flush()
    return link


def mark_reconciliation(
    session: Session,
    link: OutageIncidentTicketLink,
    *,
    state: str,
    external_ref: str | None = None,
) -> None:
    """Record the external CRM reconciliation state for one link."""

    if state not in _RECONCILIATION_STATES:
        raise ValueError(
            f"unknown reconciliation state {state!r}; expected one of "
            f"{_RECONCILIATION_STATES}"
        )
    link.reconciliation_state = state
    if external_ref is not None:
        link.external_ref = external_ref
    session.flush()
