"""Ops queue for unmatched/ambiguous customer radios (internal tickets).

Truly-unmatched radios must become work items in a queue someone already
works — the internal support-ticket table — not a dashboard list. Creation is
delegated to the canonical Ticket lifecycle participant so every item receives
a human-readable number plus audit/event evidence. The typed silent-internal
mode suppresses assignment, SLA, automation, and notification consequences.

Semantics:
- Dedupe: at most ONE open item per radio MAC (``metadata.radio_mac``,
  bare lowercase hex). Re-detections bump ``metadata.occurrences`` instead of
  spawning new tickets.
- Auto-close: the hourly review (``evaluate``) resolves an item as soon as
  the radio becomes matched (a UISP-confirmed ``cpe_devices`` row exists for
  the MAC) or, for conflict items, once the MAC no longer maps to more than
  one subscriber.
- Sources:
  1. validation-time conflicts from radio registration
     (app/services/radio_registration.py),
  2. radios registered at install that the UISP sync has NOT confirmed after
     a grace period (the radio never appeared in UISP, or the recorded MAC is
     wrong), and
  3. the uisp_sync per-station hook (``_enqueue_station_review``): stations
     UISP reports that resolve to no subscriber (``REASON_UISP_UNMATCHED``),
     to more than one candidate (``REASON_CONFLICT``), or whose serving AP
     matches no topology node (``REASON_AP_UNRESOLVED``).

``evaluate`` also retires manual placeholder rows superseded by a
UISP-confirmed row for the same MAC + subscriber, so the transient duplicate
created before the uisp_sync adoption hook lands cleans itself up.
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.network import CPEDevice, DeviceStatus, DeviceType
from app.models.service_team import ServiceTeam
from app.models.support import Ticket, TicketChannel, TicketStatus
from app.schemas.support import TicketCreate
from app.services import support as support_service

logger = logging.getLogger(__name__)

TICKET_TYPE = "unmatched_radio"
TAG = "unmatched-radio"
NETWORK_SUPPORT_TEAM_NAME = "Network Support"
NETWORK_SUPPORT_TEAM_NAME_ALIASES = (
    NETWORK_SUPPORT_TEAM_NAME,
    "Network Support Ticket",
)

REASON_CONFLICT = "mac_conflict"
REASON_NOT_ADOPTED = "not_adopted_by_uisp"
# Sync-side detections (uisp_sync._enqueue_station_review):
REASON_UISP_UNMATCHED = "uisp_station_unmatched"
REASON_AP_UNRESOLVED = "ap_unresolved"

# A registered radio normally appears in UISP within one 15-minute sync run;
# give installs a working shift before raising a review item.
DEFAULT_GRACE_HOURS = 6

_CLOSED_STATUSES = {
    TicketStatus.closed.value,
    TicketStatus.canceled.value,
    TicketStatus.merged.value,
}


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _compact(value: str | None) -> str | None:
    from app.services.radio_registration import compact_mac

    return compact_mac(value)


def open_items(db: Session) -> list[Ticket]:
    return (
        db.query(Ticket)
        .filter(
            Ticket.ticket_type == TICKET_TYPE,
            Ticket.is_active.is_(True),
            Ticket.status.notin_(_CLOSED_STATUSES),
        )
        .all()
    )


def find_open_item(db: Session, mac_compact: str) -> Ticket | None:
    for ticket in open_items(db):
        meta = ticket.metadata_ if isinstance(ticket.metadata_, dict) else {}
        if meta.get("radio_mac") == mac_compact:
            return ticket
    return None


def _network_support_team_id(db: Session) -> UUID | None:
    for team_name in NETWORK_SUPPORT_TEAM_NAME_ALIASES:
        team = (
            db.query(ServiceTeam.id)
            .filter(
                ServiceTeam.is_active.is_(True),
                func.lower(ServiceTeam.name) == team_name.lower(),
            )
            .one_or_none()
        )
        if team is not None:
            return team[0]
    return None


def open_item(
    db: Session,
    *,
    mac_compact: str,
    reason: str,
    title: str,
    description: str,
    subscriber_id=None,
    details: dict | None = None,
) -> tuple[Ticket, bool]:
    """Open (or bump) the single review item for a radio MAC. Flushes only."""
    from app.services.radio_registration import acquire_mac_lock

    # Same per-MAC advisory lock as radio registration (same key derivation):
    # the find-then-create below is not atomic on its own, and concurrent
    # detections of the same MAC must not spawn duplicate open items.
    # Reentrant when the caller (register_radio_mac) already holds the lock.
    acquire_mac_lock(db, mac_compact)

    existing = find_open_item(db, mac_compact)
    if existing is not None:
        observed = support_service.tickets.stage_internal_observation_participant(
            db,
            ticket_id=existing.id,
            source=(
                support_service.InternalOperationalTicketSource.unmatched_radio_queue
            ),
            observed_at=_now(),
        )
        return observed, False

    internal_source = (
        support_service.InternalOperationalTicketSource.unmatched_radio_queue
    )
    metadata = {
        "radio_mac": mac_compact,
        "reason": reason,
        "occurrences": 1,
        "opened_by": internal_source.value,
        **(details or {}),
    }
    ticket_payload = TicketCreate.model_validate(
        {
            "title": title,
            "description": description,
            "status": TicketStatus.open.value,
            "channel": TicketChannel.api,
            "ticket_type": TICKET_TYPE,
            "tags": [TAG],
            "subscriber_id": subscriber_id,
            "service_team_id": _network_support_team_id(db),
            "metadata": metadata,
        }
    )
    ticket = support_service.tickets.stage_internal_creation_participant(
        db,
        ticket_payload,
        source=internal_source,
    )
    logger.info(
        "unmatched_radio_item_opened mac=%s reason=%s ticket=%s",
        mac_compact,
        reason,
        ticket.id,
    )
    return ticket, True


def close_item(db: Session, ticket: Ticket, resolution: str) -> None:
    now = _now()
    support_service.transition_ticket_status(
        ticket,
        TicketStatus.closed,
        source="unmatched_radio_queue",
    )
    ticket.resolved_at = now
    ticket.closed_at = now
    meta = dict(ticket.metadata_ or {})
    meta["resolution"] = resolution
    meta["resolved_by"] = "unmatched_radio_queue"
    ticket.metadata_ = meta
    db.flush()
    logger.info("unmatched_radio_item_closed ticket=%s: %s", ticket.id, resolution)


def close_open_items_for_mac(
    db: Session,
    *,
    mac_compact: str,
    resolution: str,
    reasons: set[str] | None = None,
) -> int:
    closed = 0
    for ticket in open_items(db):
        meta = ticket.metadata_ if isinstance(ticket.metadata_, dict) else {}
        if meta.get("radio_mac") != mac_compact:
            continue
        if reasons is not None and meta.get("reason") not in reasons:
            continue
        close_item(db, ticket, resolution)
        closed += 1
    return closed


def _confirmed_rows_by_mac(db: Session) -> dict[str, CPEDevice]:
    """Normalized MAC -> a UISP-confirmed cpe row (uisp_device_id set)."""
    rows = (
        db.query(CPEDevice)
        .filter(
            CPEDevice.uisp_device_id.isnot(None),
            CPEDevice.mac_address.isnot(None),
        )
        .all()
    )
    index: dict[str, CPEDevice] = {}
    for row in rows:
        mac = _compact(row.mac_address)
        if mac:
            index.setdefault(mac, row)
    return index


def _mac_owner_count(db: Session, mac_compact: str) -> int:
    """Distinct subscribers a MAC maps to (active subscriptions + cpe rows)."""
    from app.models.catalog import Subscription, SubscriptionStatus
    from app.services.radio_registration import _compact_mac_sql

    owners: set = set()
    rows = (
        db.query(Subscription.subscriber_id)
        .filter(
            Subscription.status == SubscriptionStatus.active,
            Subscription.mac_address.isnot(None),
            _compact_mac_sql(Subscription.mac_address) == mac_compact,
        )
        .all()
    )
    owners.update(row[0] for row in rows)
    cpe_rows = (
        db.query(CPEDevice.subscriber_id)
        .filter(
            CPEDevice.mac_address.isnot(None),
            CPEDevice.status != DeviceStatus.retired,
            _compact_mac_sql(CPEDevice.mac_address) == mac_compact,
        )
        .all()
    )
    owners.update(row[0] for row in cpe_rows)
    owners.discard(None)
    return len(owners)


def evaluate(
    db: Session,
    *,
    now: datetime | None = None,
    grace_hours: int = DEFAULT_GRACE_HOURS,
) -> dict:
    """Hourly review: auto-close resolved items, retire superseded
    placeholders, and open items for registered radios the UISP sync has not
    confirmed within the grace period. Flushes; the caller commits.
    """
    now = now or _now()
    stats: Counter = Counter()
    confirmed = _confirmed_rows_by_mac(db)

    # 1. Auto-close items whose condition no longer holds.
    for ticket in open_items(db):
        meta = ticket.metadata_ if isinstance(ticket.metadata_, dict) else {}
        mac = meta.get("radio_mac")
        if not mac:
            continue
        if meta.get("reason") == REASON_CONFLICT:
            if _mac_owner_count(db, mac) <= 1:
                close_item(db, ticket, "MAC no longer maps to multiple subscribers.")
                stats["closed_conflict_cleared"] += 1
        elif meta.get("reason") == REASON_AP_UNRESOLVED:
            # Matched all along — the gap was the AP edge. Closed only once
            # the confirmed row is parented to a topology node.
            row = confirmed.get(mac)
            if row is not None and row.parent_network_device_id is not None:
                close_item(
                    db,
                    ticket,
                    f"Radio parented: device {row.id} now has a serving AP node.",
                )
                stats["closed_ap_resolved"] += 1
        elif mac in confirmed:
            close_item(
                db,
                ticket,
                f"Radio matched: UISP-confirmed device {confirmed[mac].id}.",
            )
            stats["closed_matched"] += 1

    # 2. Manual placeholders (registered at install, uisp_device_id NULL).
    placeholders = (
        db.query(CPEDevice)
        .filter(
            CPEDevice.device_type == DeviceType.wireless_radio,
            CPEDevice.uisp_device_id.is_(None),
            CPEDevice.status == DeviceStatus.active,
            CPEDevice.mac_address.isnot(None),
        )
        .all()
    )
    grace = timedelta(hours=grace_hours)
    for row in placeholders:
        mac = _compact(row.mac_address)
        if not mac:
            continue
        confirmed_row = confirmed.get(mac)
        if confirmed_row is not None:
            if confirmed_row.subscriber_id == row.subscriber_id:
                # The sync created its own row for this radio (adoption hook
                # not yet in place); retire the manual placeholder so the
                # device is not listed twice.
                row.status = DeviceStatus.retired
                note = (
                    f"Superseded by UISP-synced device {confirmed_row.id} "
                    f"({now.date().isoformat()})."
                )
                row.notes = f"{row.notes}\n{note}" if row.notes else note
                stats["placeholders_retired"] += 1
            continue
        created_at = _aware(row.created_at)
        if created_at is None or now - created_at < grace:
            continue
        _, created = open_item(
            db,
            mac_compact=mac,
            reason=REASON_NOT_ADOPTED,
            title=f"Registered radio not seen by UISP: {row.mac_address}",
            description=(
                f"Radio MAC {row.mac_address} was registered at install for "
                f"subscriber {row.subscriber_id} (cpe_devices {row.id}) but the "
                f"UISP topology sync has not confirmed the device after "
                f"{grace_hours}h. Verify the radio is online in UISP and that "
                "the recorded MAC is correct."
            ),
            subscriber_id=row.subscriber_id,
            details={"cpe_device_id": str(row.id)},
        )
        stats["opened_not_adopted" if created else "bumped_not_adopted"] += 1

    db.flush()
    return dict(stats)
