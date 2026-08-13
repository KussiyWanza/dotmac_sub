"""Customer/reseller portal support service.

Tickets are served by the internal (local) ticket module
(``app.services.support``) so the portal works standalone. Work orders (and the
reseller ticket counts) still read from the external CRM via ``crm_client``;
those can be pointed at dotmac_crm when configured.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from fastapi import Request
from sqlalchemy.orm import Session

from app.models.subscriber import Subscriber
from app.models.support import TicketCommentAuthorType, canonical_ticket_status_value
from app.services.common import coerce_uuid
from app.services.crm_client import CRMClientError
from app.services.db_session_adapter import db_session_adapter
from app.services.domain_errors import DomainError
from app.services.integrations.crm_capability import capability_client
from app.services.session_store import get_session_redis
from app.services.status_presentation import ticket_status_presentation
from app.services.support_ticket_settings import SupportTeamRoutingResolution

logger = logging.getLogger(__name__)

# ── Cache TTLs ───────────────────────────────────────────────────────────

_CACHE_SUBSCRIBER_MAP = 3600  # 1hr — subscriber ID mapping
_CACHE_LIST = 60  # 60s — list pages
_CACHE_DETAIL = 30  # 30s — detail pages

TICKET_PRIORITY_DISPLAY: dict[str, str] = {
    "lower": "Lower",
    "low": "Low",
    "medium": "Medium",
    "normal": "Normal",
    "high": "High",
    "urgent": "Urgent",
}

TICKET_PRIORITY_COLORS: dict[str, str] = {
    "lower": "bg-slate-100 text-slate-700 dark:bg-slate-700 dark:text-slate-300",
    "low": "bg-slate-100 text-slate-700 dark:bg-slate-700 dark:text-slate-300",
    "medium": "bg-blue-100 text-blue-700 dark:bg-blue-900 dark:text-blue-300",
    "normal": "bg-blue-100 text-blue-700 dark:bg-blue-900 dark:text-blue-300",
    "high": "bg-amber-100 text-amber-700 dark:bg-amber-900 dark:text-amber-300",
    "urgent": "bg-rose-100 text-rose-700 dark:bg-rose-900 dark:text-rose-300",
}

# ── Subscriber ID Resolution ────────────────────────────────────────────


def _cache_get(key: str) -> str | None:
    """Get a value from Redis cache, or None."""
    r = get_session_redis()
    if not r:
        return None
    try:
        val = r.get(key)
        return str(val) if val is not None else None
    except Exception as exc:
        logger.warning("Redis cache get failed for key %s: %s", key, exc)
        return None


def _cache_set(key: str, value: str, ttl: int) -> None:
    """Set a value in Redis cache with TTL."""
    r = get_session_redis()
    if not r:
        return
    try:
        r.setex(key, ttl, value)
    except Exception as exc:
        logger.warning("Redis cache set failed for key %s: %s", key, exc)


def resolve_crm_subscriber_id(db: Session, subscriber_id: str) -> str | None:
    """Resolve a DotMac Sub subscriber UUID to a CRM subscriber UUID.

    Prefers the locally stored crm_subscriber_id; falls back to the legacy
    imported customer id -> CRM external_id chain and persists the result so each
    subscriber only pays for the chain once. Cached in Redis for 1hr.
    """
    cache_key = f"crm:sub_map:{subscriber_id}"
    cached = _cache_get(cache_key)
    if cached:
        return cached if cached != "__none__" else None

    subscriber = db.get(Subscriber, coerce_uuid(subscriber_id))
    if not subscriber:
        _cache_set(cache_key, "__none__", _CACHE_SUBSCRIBER_MAP)
        return None

    if subscriber.crm_subscriber_id:
        crm_id = str(subscriber.crm_subscriber_id)
        _cache_set(cache_key, crm_id, _CACHE_SUBSCRIBER_MAP)
        return crm_id

    if not subscriber.splynx_customer_id:
        _cache_set(cache_key, "__none__", _CACHE_SUBSCRIBER_MAP)
        return None

    client = capability_client(db)
    crm_id = client.resolve_subscriber_id(subscriber.splynx_customer_id)
    if crm_id:
        try:
            subscriber.crm_subscriber_id = coerce_uuid(crm_id)
            db.commit()
        except ValueError:
            pass
        _cache_set(cache_key, crm_id, _CACHE_SUBSCRIBER_MAP)
    else:
        _cache_set(cache_key, "__none__", 300)  # shorter TTL for misses
    return crm_id


def resolve_crm_subscriber_ids(db: Session, subscriber_ids: Iterable[str]) -> list[str]:
    """Resolve multiple DotMac subscriber UUIDs to CRM subscriber UUIDs."""
    resolved: list[str] = []
    seen: set[str] = set()
    for subscriber_id in subscriber_ids:
        candidate = str(subscriber_id or "").strip()
        if not candidate:
            continue
        crm_id = resolve_crm_subscriber_id(db, candidate)
        if crm_id and crm_id not in seen:
            seen.add(crm_id)
            resolved.append(crm_id)
    return resolved


def _sort_by_recent(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort CRM items by updated_at/created_at descending."""
    return sorted(
        items,
        key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""),
        reverse=True,
    )


def _error_context(
    message: str = "Unable to reach support system. Please try again later.",
) -> dict[str, Any]:
    """Return CRM error flags for template context."""
    return {"crm_error": True, "crm_error_message": message}


def _ok_context() -> dict[str, Any]:
    """Return clean CRM status flags."""
    return {"crm_error": False, "crm_error_message": ""}


# ── Customer Portal: Tickets ────────────────────────────────────────────


def _ticket_to_dict(ticket: Any) -> dict[str, Any]:
    """Map a local support Ticket to the dict shape the portal templates expect."""
    from app.services.customer_experience_lifecycle import ticket_actions

    meta = ticket.metadata_ if isinstance(ticket.metadata_, dict) else {}
    csat = meta.get("csat") if isinstance(meta.get("csat"), dict) else {}
    due_at = getattr(ticket, "due_at", None)
    resolved_at = getattr(ticket, "resolved_at", None)
    closed_at = getattr(ticket, "closed_at", None)
    description_is_internal = bool(getattr(ticket, "description_is_internal", True))
    return {
        "id": str(ticket.id),
        "ticket_number": ticket.number,
        "title": ticket.title,
        "description": "" if description_is_internal else (ticket.description or ""),
        "description_available_to_customer": not description_is_internal,
        "attachments": (
            [] if description_is_internal else list(ticket.attachments or [])
        ),
        "status": canonical_ticket_status_value(ticket.status),
        "status_presentation": ticket_status_presentation(ticket.status).model_dump(
            mode="json"
        ),
        "priority": ticket.priority,
        "subscriber_id": str(ticket.subscriber_id) if ticket.subscriber_id else None,
        "due_at": due_at.isoformat() if due_at else None,
        "resolved_at": resolved_at.isoformat() if resolved_at else None,
        "closed_at": closed_at.isoformat() if closed_at else None,
        "created_at": ticket.created_at.isoformat() if ticket.created_at else None,
        "updated_at": ticket.updated_at.isoformat() if ticket.updated_at else None,
        "csat_rating": csat.get("rating"),
        "resolution_actions": [
            action.model_dump(mode="json") for action in ticket_actions(ticket)
        ],
    }


def handle_ticket_rating(
    db: Session,
    subscriber_ids: list[str],
    ticket_id: str,
    rating: int,
    comment: str | None = None,
) -> dict[str, Any]:
    """Record a customer CSAT rating on a local support ticket the caller owns.

    Returns a dict with a 'success' bool (mirrors handle_ticket_comment)."""
    from app.services import support as support_service

    allowed = {str(s or "").strip() for s in subscriber_ids if str(s or "").strip()}
    try:
        ticket = support_service.Tickets.get(db, ticket_id)
    except Exception:  # noqa: BLE001 - not found / invalid id
        return {"success": False, "error": "Ticket not found."}
    if not ticket.subscriber_id or str(ticket.subscriber_id) not in allowed:
        return {"success": False, "error": "Ticket not found."}
    try:
        db_session_adapter.release_read_transaction(db)
        support_service.Tickets.set_satisfaction(
            db,
            ticket,
            rating=max(1, min(5, int(rating))),
            comment=(comment or "")[:2000] or None,
        )
    except DomainError:
        return {"success": False, "error": "You can rate support once closed."}
    return {"success": True}


def handle_ticket_resolution_response(
    db: Session,
    subscriber_ids: list[str],
    ticket_id: str,
    *,
    confirm: bool,
    reason: str | None = None,
) -> dict[str, Any]:
    """Apply a signed-in customer's resolution response to a native ticket."""
    from app.services import support as support_service

    allowed = {str(value) for value in subscriber_ids if value}
    try:
        ticket = support_service.Tickets.get(db, ticket_id)
    except Exception:  # noqa: BLE001 - ownership-safe not-found response
        return {"success": False, "error": "Ticket not found."}
    if not ticket.subscriber_id or str(ticket.subscriber_id) not in allowed:
        return {"success": False, "error": "Ticket not found."}
    try:
        db_session_adapter.release_read_transaction(db)
        support_service.Tickets.respond_to_resolution_for_customer(
            db,
            ticket,
            confirm=confirm,
            reason=reason,
        )
    except DomainError as exc:
        return {"success": False, "error": exc.message}
    return {"success": True}


def _comment_to_dict(comment: Any, customer_subscriber_ids: set[str]) -> dict[str, Any]:
    """Map a local TicketComment to the portal comment dict.

    New rows carry explicit author identity. Legacy rows with a subscriber
    author are treated as customer-authored only when the ID is in the current
    customer's allowed subscriber set; NULL author rows render as support.
    """
    author_type = str(getattr(comment, "author_type", "") or "")
    author_person_id = getattr(comment, "author_person_id", None)
    is_customer = (
        author_type == TicketCommentAuthorType.customer.value
        and str(author_person_id or "") in customer_subscriber_ids
    )
    if not author_type and author_person_id:
        is_customer = str(author_person_id) in customer_subscriber_ids
    return {
        "body": comment.body,
        "attachments": list(comment.attachments or []),
        "author_name": "You" if is_customer else "Support Team",
        "is_internal": comment.is_internal,
        "created_at": comment.created_at.isoformat() if comment.created_at else None,
    }


def _ticket_list_base(request: Request, customer: dict) -> dict[str, Any]:
    from app.services import web_support_tickets

    return {
        "request": request,
        "customer": customer,
        "active_page": "support",
        "priority_display": TICKET_PRIORITY_DISPLAY,
        "priority_colors": TICKET_PRIORITY_COLORS,
        "max_attachment_bytes": web_support_tickets.MAX_ATTACHMENT_BYTES,
        "max_attachment_mb": max(
            1, web_support_tickets.MAX_ATTACHMENT_BYTES // 1048576
        ),
    }


def tickets_list_context(
    request: Request,
    db: Session,
    customer: dict,
    subscriber_ids: list[str],
) -> dict[str, Any]:
    """Customer ticket list, sourced from the internal (local) ticket module."""
    from app.services import support as support_service

    try:
        merged: dict[str, dict[str, Any]] = {}
        for sid in subscriber_ids:
            sid_str = str(sid or "").strip()
            if not sid_str:
                continue
            for ticket in support_service.Tickets.list(
                db, subscriber_id=sid_str, limit=100
            ):
                merged[str(ticket.id)] = _ticket_to_dict(ticket)
    except Exception as e:  # noqa: BLE001
        logger.error("Failed to load portal tickets: %s", e)
        return {
            **_ticket_list_base(request, customer),
            "tickets": [],
            **_error_context(),
        }
    tickets = _sort_by_recent(list(merged.values()))
    return {**_ticket_list_base(request, customer), "tickets": tickets, **_ok_context()}


def ticket_detail_context(
    request: Request,
    db: Session,
    customer: dict,
    subscriber_ids: list[str],
    ticket_id: str,
) -> dict[str, Any]:
    """Customer ticket detail, sourced from the internal (local) ticket module."""
    from app.services import support as support_service

    allowed = {str(s or "").strip() for s in subscriber_ids if str(s or "").strip()}
    not_found = {
        "request": request,
        "customer": customer,
        "ticket": None,
        "comments": [],
        "active_page": "support",
        **_error_context("Ticket not found."),
    }
    try:
        ticket = support_service.Tickets.get(db, ticket_id)
    except Exception:  # noqa: BLE001 - not found / invalid id
        return not_found
    # Verify the ticket belongs to one of this customer's subscriber accounts.
    if not ticket.subscriber_id or str(ticket.subscriber_id) not in allowed:
        return not_found

    comments = [
        _comment_to_dict(c, allowed)
        for c in support_service.TicketComments.list(db, str(ticket.id))
        if not c.is_internal
    ]
    return {
        **_ticket_list_base(request, customer),
        "ticket": _ticket_to_dict(ticket),
        "comments": comments,
        **_ok_context(),
    }


def ticket_create_context(
    request: Request,
    db: Session,
    customer: dict,
) -> dict[str, Any]:
    """Build template context for ticket creation form."""
    from app.services import support as support_service

    return {
        "request": request,
        "customer": customer,
        "active_page": "support",
        "priorities": list(TICKET_PRIORITY_DISPLAY.keys()),
        "priority_display": TICKET_PRIORITY_DISPLAY,
        "region_options": support_service.regions(db),
        **_ok_context(),
    }


def _record_ticket_created_portal_notification(db: Session, ticket: Any) -> None:
    """Add the immediate in-app acknowledgement for a customer-created ticket.

    This is deliberately a portal event, not an email/SMS delivery: customers
    should see the acknowledgement in their notification inbox even when no
    external notification channel is configured.
    """
    subscriber_id = getattr(ticket, "subscriber_id", None)
    if not subscriber_id:
        return

    try:
        from app.models.comms import (
            CustomerNotificationEvent,
            CustomerNotificationStatus,
        )

        ticket_number = getattr(ticket, "number", None) or ""
        reference = f" {ticket_number}" if ticket_number else ""
        db.add(
            CustomerNotificationEvent(
                entity_type="support_ticket",
                entity_id=ticket.id,
                subscriber_id=subscriber_id,
                channel="portal",
                recipient=f"portal:{subscriber_id}",
                message=(
                    f"Your support ticket{reference} has been created. "
                    "We will update you here."
                ),
                status=CustomerNotificationStatus.sent,
                sent_at=datetime.now(UTC),
            )
        )
        db.commit()
    except Exception:  # noqa: BLE001 - acknowledgement must not hide the ticket
        db.rollback()
        logger.exception(
            "Failed to record portal acknowledgement for support ticket %s",
            getattr(ticket, "id", None),
        )


def handle_ticket_create(
    db: Session,
    customer: dict,
    subscriber_id: str,
    title: str,
    description: str,
    priority: str,
    region: str,
    team_routing: SupportTeamRoutingResolution,
    attachments: list | None = None,
) -> dict[str, Any]:
    """Create a ticket in the internal (local) ticket module.

    ``attachments`` (UploadFile list) are stored locally on the ticket via
    ``upload_ticket_attachments``.

    Returns:
        Dict with 'success' bool and 'ticket' or 'error' key.
    """
    from app.models.support import TicketChannel
    from app.schemas.support import TicketCreate
    from app.services import support as support_service
    from app.services import web_support_tickets

    try:
        sid = coerce_uuid(str(subscriber_id or "").strip() or None)
    except (ValueError, TypeError):
        sid = None
    if not sid:
        return {
            "success": False,
            "error": "Unable to link your account to the support system.",
        }
    files = [a for a in (attachments or []) if getattr(a, "filename", "")]
    try:
        db_session_adapter.release_read_transaction(db)
        ticket = support_service.Tickets.create(
            db,
            TicketCreate(
                subscriber_id=sid,
                title=title,
                description=description or "",
                description_is_internal=False,
                priority=priority if priority in TICKET_PRIORITY_DISPLAY else "normal",
                region=region,
                service_team_id=team_routing.service_team_id,
                channel=TicketChannel.web,
            ),
            actor_id=None,
            # The ticket is already durable before event delivery. Do not make
            # the customer wait for a broker or third-party integration; the
            # scheduled event dispatcher drains this outbox record.
            dispatch_event_after_commit=False,
            routing_mode=support_service.TicketCreationRoutingMode.preserve_requested_team,
        )
        if files:
            uploaded = web_support_tickets.upload_ticket_attachments(
                db,
                ticket_id=str(ticket.id),
                attachments=files,
                entity_type="support_ticket_attachment",
                actor_id=str(sid),
            )
            support_service.Tickets.add_attachments(db, str(ticket.id), uploaded)
        _record_ticket_created_portal_notification(db, ticket)
        return {"success": True, "ticket": _ticket_to_dict(ticket)}
    except ValueError as e:
        # Attachment validation (type/size) — surface the specific reason.
        logger.info("Rejected portal ticket attachment: %s", e)
        return {"success": False, "error": str(e)}
    except Exception as e:  # noqa: BLE001
        logger.error("Failed to create portal ticket: %s", e)
        return {
            "success": False,
            "error": "Unable to create ticket. Please try again later.",
        }


def handle_ticket_comment(
    db: Session,
    customer: dict,
    subscriber_ids: list[str],
    ticket_id: str,
    body: str,
    attachments: list | None = None,
) -> dict[str, Any]:
    """Add a customer comment to a local support ticket.

    ``attachments`` (UploadFile list) are stored locally on the comment via
    ``upload_ticket_attachments``.

    Returns:
        Dict with 'success' bool.
    """
    from app.schemas.support import AttachmentMeta, TicketCommentCreate
    from app.services import support as support_service
    from app.services import web_support_tickets

    allowed = {str(s or "").strip() for s in subscriber_ids if str(s or "").strip()}
    try:
        ticket = support_service.Tickets.get(db, ticket_id)
    except Exception:  # noqa: BLE001 - not found / invalid id
        return {"success": False, "error": "Ticket not found."}
    if not ticket.subscriber_id or str(ticket.subscriber_id) not in allowed:
        return {"success": False, "error": "Ticket not found."}
    files = [a for a in (attachments or []) if getattr(a, "filename", "")]
    try:
        try:
            author_person_id = coerce_uuid(ticket.subscriber_id)
        except (TypeError, ValueError):
            author_person_id = None
        uploaded: list[dict] = []
        if files:
            db_session_adapter.release_read_transaction(db)
            uploaded = web_support_tickets.upload_ticket_attachments(
                db,
                ticket_id=str(ticket.id),
                attachments=files,
                entity_type="support_ticket_comment_attachment",
                actor_id=str(ticket.subscriber_id),
            )
        db_session_adapter.release_read_transaction(db)
        support_service.Tickets.create_comment(
            db,
            str(ticket.id),
            TicketCommentCreate(
                body=body,
                is_internal=False,
                author_type=TicketCommentAuthorType.customer,
                author_person_id=author_person_id,
                attachments=[AttachmentMeta(**item) for item in uploaded],
            ),
            actor_id=None,
        )
        return {"success": True}
    except ValueError as e:
        # Attachment validation (type/size) — surface the specific reason.
        logger.info("Rejected portal ticket comment attachment: %s", e)
        return {"success": False, "error": str(e)}
    except Exception as e:  # noqa: BLE001
        logger.error("Failed to add portal ticket comment: %s", e)
        return {
            "success": False,
            "error": "Unable to add comment. Please try again later.",
        }


# ── Reseller Portal ─────────────────────────────────────────────────────


def reseller_account_tickets_context(
    request: Request,
    db: Session,
    account_id: str,
    current_user: dict,
    reseller: Any,
) -> dict[str, Any]:
    """Build template context for reseller viewing a customer's tickets."""
    try:
        crm_sub_id = resolve_crm_subscriber_id(db, account_id)
        if not crm_sub_id:
            return {
                "request": request,
                "current_user": current_user,
                "reseller": reseller,
                "tickets": [],
                "account_id": account_id,
                "active_page": "accounts",
                **_ok_context(),
            }
        client = capability_client(db)
        tickets = client.list_tickets(subscriber_id=crm_sub_id)
    except CRMClientError:
        return {
            "request": request,
            "current_user": current_user,
            "reseller": reseller,
            "tickets": [],
            "account_id": account_id,
            "active_page": "accounts",
            **_error_context(),
        }

    return {
        "request": request,
        "current_user": current_user,
        "reseller": reseller,
        "tickets": [
            {
                **ticket,
                "status": canonical_ticket_status_value(
                    str(ticket.get("status") or "")
                ),
                "status_presentation": ticket_status_presentation(
                    ticket.get("status")
                ).model_dump(mode="json"),
            }
            for ticket in tickets
        ],
        "account_id": account_id,
        "active_page": "accounts",
        **_ok_context(),
    }


def reseller_open_tickets_count(
    db: Session,
    reseller_id: str,
    account_ids: list[str],
) -> int | None:
    """Count open tickets across all reseller accounts.

    Returns None if CRM is unreachable so callers do not show a false zero.
    """
    total = 0
    client = capability_client(db)
    for account_id in account_ids:
        try:
            crm_sub_id = resolve_crm_subscriber_id(db, account_id)
            if not crm_sub_id:
                continue
            tickets = client.list_tickets(subscriber_id=crm_sub_id)
            total += sum(
                1
                for t in tickets
                if t.get("status") in ("open", "in_progress", "waiting_on_agent")
            )
        except CRMClientError:
            logger.debug("CRM unreachable for open ticket count, skipping")
            return None
    return total
