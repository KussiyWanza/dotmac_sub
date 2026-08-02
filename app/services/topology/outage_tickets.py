"""Compatibility re-export: incident ticket links live with their owner.

The link writers are part of ``network.outage_lifecycle`` and live in
``app.services.topology.outage``; this module re-exports them for callers
that address the ticket-link surface by name. It performs no persistence of
its own.
"""

from app.services.topology.outage import (
    COMPLAINT_ROLE,
    INFRASTRUCTURE_ROLE,
    infrastructure_link_for,
    link_complaint_ticket,
    link_infrastructure_ticket,
    links_for_incident,
    mark_reconciliation,
)

__all__ = [
    "COMPLAINT_ROLE",
    "INFRASTRUCTURE_ROLE",
    "infrastructure_link_for",
    "link_complaint_ticket",
    "link_infrastructure_ticket",
    "links_for_incident",
    "mark_reconciliation",
]
