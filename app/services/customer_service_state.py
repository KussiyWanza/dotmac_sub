"""Shared, outage-aware customer service state (single source of truth).

Billing sweeps, notification policy, support tooling, and the portal/mobile
connection surfaces all need the same answer to "is this customer currently
affected by an infrastructure fault, and how should we treat them?". Each
module inventing its own outage logic drifts, so this service owns the
predicates; consumers (``app.tasks.catalog`` today; dunning/enforcement,
ticket deflection and support context next) call in rather than re-derive.

Two access patterns, deliberately separate:

- ``get_customer_service_state(session, subscription)`` — the full
  per-customer view. Resolves the customer's network path via
  ``topology.connection_status`` (real graph work), so it is right for portal
  pages, support context and ticket deflection — NOT for sweeps.
- The batch helpers (``subscription_ids_under_active_outage``,
  ``subscribers_with_open_infrastructure_down_tickets``) — set-based, one
  query pass over the whole cohort, right for Celery sweeps over thousands of
  subscriptions (expiry reminders, dunning, enforcement retries).
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.services.customer_support_links import (
    ticket_customer_any_link_filter,
    ticket_customer_link_filter,
    ticket_customer_linked_ids,
)
from app.services.status_presentation import connection_health_status_presentation

logger = logging.getLogger(__name__)

OPEN_INFRASTRUCTURE_TICKET_STATUSES = {
    "new",
    "open",
    "pending",
    "waiting_on_customer",
    "lastmile_rerun",
    "site_under_construction",
    "on_hold",
    "pending_confirmation",
}

INFRASTRUCTURE_DOWN_TICKET_MARKERS = (
    "infrastructure down",
    "service down",
    "internet down",
    "no internet",
    "outage",
    "link down",
    "fiber cut",
    "link disconnection",
    "customer link disconnection",
    "multiple customer link disconnection",
    "core link disconnection",
    "multiple core link disconnection",
    "cabinet disconnection",
    "multiple cabinet disconnection",
    "multiple cabinet link disconnection",
    "access point outage",
    "bts outage",
)

# Billing-comms copy when reminders are paused. Kept customer-safe: no node
# names, no incident internals (same discipline as topology.connection_status).
_AREA_OUTAGE_BILLING_MESSAGE = (
    "We're aware of a service issue in your area. Billing reminders are "
    "paused while this outage is active."
)
_TICKET_BILLING_MESSAGE = (
    "We're working on a reported service issue affecting your connection. "
    "Billing reminders are paused while it's open."
)


@dataclass
class CustomerServiceState:
    """Outage-aware treatment decision for one subscription.

    ``active_outage_id`` is set only when an ``OutageIncident`` covers the
    customer's path; an *inferred* area outage (classifier boundary with no
    incident row yet) still sets ``area_outage``/suppression, just without an
    incident to link. ``customer_message`` is the billing-comms softener —
    the connection-status wording itself comes from
    ``topology.connection_status``.
    """

    subscription_id: uuid.UUID
    subscriber_id: uuid.UUID | None
    billing_state: str
    connection_state: str
    area_outage: bool
    active_outage_id: uuid.UUID | None
    open_infrastructure_ticket_id: uuid.UUID | None
    should_suppress_expiry_notice: bool
    should_suppress_suspension_notice: bool
    extension_candidate: bool
    customer_message: str | None
    checked_at: datetime

    def support_context(self) -> dict[str, Any]:
        """Flat dict for support/agent surfaces (subscriber detail page)."""
        return {
            "billing_state": self.billing_state,
            "connection_state": self.connection_state,
            "connection_status_presentation": connection_health_status_presentation(
                self.connection_state
            ).model_dump(mode="json"),
            "area_outage": self.area_outage,
            "active_outage_id": (
                str(self.active_outage_id) if self.active_outage_id else None
            ),
            "open_infrastructure_ticket_id": (
                str(self.open_infrastructure_ticket_id)
                if self.open_infrastructure_ticket_id
                else None
            ),
            "billing_reminders_suppressed": self.should_suppress_expiry_notice,
            "extension_candidate": self.extension_candidate,
            "checked_at": self.checked_at.isoformat(),
        }


def get_customer_service_state(
    session: Session, subscription, *, now: datetime | None = None
) -> CustomerServiceState:
    """Assess one subscription: connection state + fault-aware billing flags."""
    from app.services.topology.connection_status import assess

    a = assess(session, subscription, now=now)

    active_outage_id: uuid.UUID | None = None
    if a.is_area_outage:
        active_outage_id = _incident_id_for_subscription(session, subscription)

    ticket = open_infrastructure_down_ticket(session, subscription.subscriber_id)
    suppress = a.is_area_outage or ticket is not None

    if a.is_area_outage:
        customer_message = _AREA_OUTAGE_BILLING_MESSAGE
    elif ticket is not None:
        customer_message = _TICKET_BILLING_MESSAGE
    else:
        customer_message = None

    status = getattr(subscription, "status", None)
    return CustomerServiceState(
        subscription_id=subscription.id,
        subscriber_id=subscription.subscriber_id,
        billing_state=getattr(status, "value", str(status or "")),
        connection_state=a.state,
        area_outage=a.is_area_outage,
        active_outage_id=active_outage_id,
        open_infrastructure_ticket_id=ticket.id if ticket is not None else None,
        should_suppress_expiry_notice=suppress,
        should_suppress_suspension_notice=suppress,
        extension_candidate=a.is_area_outage,
        customer_message=customer_message,
        checked_at=a.checked_at,
    )


def _incident_id_for_subscription(session: Session, subscription) -> uuid.UUID | None:
    """The live OutageIncident covering this customer's path, if any."""
    from app.services.topology.customer_path import resolve_customer_path
    from app.services.topology.outage import open_incident_for_path

    try:
        path = resolve_customer_path(session, subscription)
        incident = open_incident_for_path(session, path)
    except Exception:  # advisory link only; the area_outage flag already stands
        logger.exception(
            "Failed to resolve outage incident for subscription %s", subscription.id
        )
        return None
    return incident.id if incident is not None else None


# ---------------------------------------------------------------------------
# Batch helpers (Celery sweeps) — set-based, no per-customer path resolution.
# ---------------------------------------------------------------------------


def open_infrastructure_down_ticket(session: Session, subscriber_id):
    """The subscriber's open infrastructure-down ticket, or None."""
    if not subscriber_id:
        return None
    from app.models.support import Ticket

    tickets = (
        session.query(Ticket)
        .filter(Ticket.is_active.is_(True))
        .filter(Ticket.status.in_(OPEN_INFRASTRUCTURE_TICKET_STATUSES))
        .filter(ticket_customer_link_filter(Ticket, subscriber_id))
        .all()
    )
    for ticket in tickets:
        if is_infrastructure_down_ticket(ticket):
            return ticket
    return None


def subscribers_with_open_infrastructure_down_tickets(
    session: Session,
    subscriber_ids: set[object],
    *,
    max_age_hours: int | None = None,
) -> set[object]:
    """Subset of ``subscriber_ids`` holding an open infrastructure-down ticket.

    ``max_age_hours`` bounds how long an open ticket keeps counting — a
    customer-impact shield must expire rather than pin its account
    indefinitely. ``None`` preserves the unbounded behavior for callers that
    are not shields.
    """
    from app.models.support import Ticket

    subscriber_ids = {
        subscriber_id for subscriber_id in subscriber_ids if subscriber_id
    }
    if not subscriber_ids:
        return set()

    query = (
        session.query(Ticket)
        .filter(Ticket.is_active.is_(True))
        .filter(Ticket.status.in_(OPEN_INFRASTRUCTURE_TICKET_STATUSES))
        .filter(ticket_customer_any_link_filter(Ticket, subscriber_ids))
    )
    if max_age_hours is not None:
        cutoff = datetime.now(UTC) - timedelta(hours=max(1, int(max_age_hours)))
        query = query.filter(Ticket.created_at >= cutoff)
    tickets = query.all()
    suppressed: set[object] = set()
    for ticket in tickets:
        if not is_infrastructure_down_ticket(ticket):
            continue
        for ticket_subscriber_id in ticket_customer_linked_ids(ticket):
            if ticket_subscriber_id in subscriber_ids:
                suppressed.add(ticket_subscriber_id)
    return suppressed


def subscription_ids_under_active_outage(
    session: Session, subscriptions
) -> set[object]:
    """Subset of ``subscriptions`` inside a live outage incident's blast radius."""
    subscription_ids = {sub.id for sub in subscriptions if sub.id}
    if not subscription_ids:
        return set()
    return active_outage_subscription_ids(session) & subscription_ids


def active_outage_subscription_ids(
    session: Session,
    *,
    manual_open_max_hours: int | None = None,
) -> set[object]:
    """Every subscription id inside the blast radius of a live outage incident.

    Which incidents count (customer-impact policy):

    - manual operator ``open``: until resolved — a human declared it and a
      human owns closing it. ``manual_open_max_hours`` optionally bounds how
      long such an incident keeps counting for shield-type callers, so a
      forgotten incident cannot pin billing suppression forever;
    - classifier ``confirmed``/``clearing``: always — that lifecycle debounces
      and auto-resolves itself;
    - auto-detect operator ``open`` (``declared_by = system:outage-autodetect``):
      only while FRESH. The legacy auto-detect path opens incidents that
      nothing auto-resolves, and a few days of accumulated zombies once put
      97% of the fleet "under outage" and suppressed nearly every billing
      notice. Freshness window: ``autodetect_incident_impact_ttl_hours``
      (default 6).

    The blast radius per incident scope (root node / basestation / FDH
    cabinet) comes from ``topology.affected``.
    """
    from app.models.domain_settings import SettingDomain
    from app.models.network_monitoring import NetworkDevice, OutageIncident, PopSite
    from app.services.settings_spec import resolve_value
    from app.services.topology.outage import (
        AUTO_DETECT_ACTOR,
        CLASSIFIER_CUSTOMER_VISIBLE_STATUSES,
    )

    live_statuses = ("open", *CLASSIFIER_CUSTOMER_VISIBLE_STATUSES)
    incidents = (
        session.query(OutageIncident)
        .filter(OutageIncident.status.in_(live_statuses))
        .all()
    )
    if not incidents:
        return set()

    try:
        ttl_hours = int(
            resolve_value(
                session,
                SettingDomain.network_monitoring,
                "autodetect_incident_impact_ttl_hours",
            )
            or 6
        )
    except (TypeError, ValueError):
        ttl_hours = 6
    autodetect_cutoff = datetime.now(UTC) - timedelta(hours=max(1, ttl_hours))

    manual_cutoff = (
        datetime.now(UTC) - timedelta(hours=max(1, int(manual_open_max_hours)))
        if manual_open_max_hours is not None
        else None
    )

    def _counts_for_impact(incident) -> bool:
        if incident.status != "open":
            return True
        started = incident.started_at
        if started is not None and started.tzinfo is None:
            started = started.replace(tzinfo=UTC)
        if incident.declared_by == AUTO_DETECT_ACTOR:
            return started is not None and started >= autodetect_cutoff
        if manual_cutoff is not None:
            return started is not None and started >= manual_cutoff
        return True

    incidents = [incident for incident in incidents if _counts_for_impact(incident)]
    if not incidents:
        return set()

    from app.models.network import FdhCabinet
    from app.services.topology.affected import affected_customers

    affected: set[object] = set()
    for incident in incidents:
        node = (
            session.get(NetworkDevice, incident.root_node_id)
            if incident.root_node_id
            else None
        )
        basestation = (
            session.get(PopSite, incident.basestation_id)
            if incident.basestation_id
            else None
        )
        fdh = (
            session.get(FdhCabinet, incident.fdh_cabinet_id)
            if incident.fdh_cabinet_id
            else None
        )
        if node is None and basestation is None and fdh is None:
            continue
        try:
            impact = affected_customers(
                session, node=node, basestation=basestation, fdh=fdh
            )
        except Exception:
            # One unresolvable incident must not block the whole sweep; the
            # ticket-based suppression still covers reported faults.
            logger.exception(
                "Failed to resolve outage blast radius for incident %s", incident.id
            )
            continue
        affected.update(
            s.id for s in impact.get("subscriptions", []) if getattr(s, "id", None)
        )
    return affected


def is_infrastructure_down_ticket(ticket: Any) -> bool:
    """Free-text match: does this ticket describe an infrastructure fault?

    ``ticket_type`` is a free-form String (no enum), so markers are matched
    over type/title/description/tags/metadata — same contract the expiry
    suppression has used since it shipped.
    """
    parts: list[str] = [
        str(getattr(ticket, "ticket_type", "") or ""),
        str(getattr(ticket, "title", "") or ""),
        str(getattr(ticket, "description", "") or ""),
    ]
    tags = getattr(ticket, "tags", None)
    if isinstance(tags, list):
        parts.extend(str(tag or "") for tag in tags)
    metadata = getattr(ticket, "metadata_", None)
    if isinstance(metadata, dict):
        for key in ("ticket_type", "category", "issue", "reason", "source"):
            parts.append(str(metadata.get(key) or ""))
    text = " ".join(parts).strip().lower()
    text = " ".join(text.split())
    return any(marker in text for marker in INFRASTRUCTURE_DOWN_TICKET_MARKERS)
