"""Preview-first command owner for admin support-ticket bulk updates."""

from __future__ import annotations

import hmac
import json
from collections.abc import Mapping
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.support import Ticket, TicketStatus
from app.schemas.support import TicketBulkUpdateItem, TicketBulkUpdateRequest
from app.services import support as support_service
from app.services import support_ticket_settings
from app.services.bulk_actions import membership_scope_token, parse_bulk_selection
from app.services.db_session_adapter import db_session_adapter
from app.services.domain_errors import DomainError


class SupportTicketBulkError(DomainError):
    """Stable bulk membership, validation, or drift rejection."""


@dataclass(frozen=True, slots=True)
class SupportTicketBulkChanges:
    """One normalized change set applied to every eligible selected ticket."""

    status: str | None = None
    priority: str | None = None
    assigned_to_person_id: str | None = None

    def as_dict(self) -> dict[str, str]:
        return {
            key: value
            for key, value in (
                ("status", self.status),
                ("priority", self.priority),
                ("assigned_to_person_id", self.assigned_to_person_id),
            )
            if value is not None
        }

    @property
    def fingerprint(self) -> str:
        return json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class SupportTicketBulkPreview:
    """Exact membership, eligibility, and proposed-change snapshot."""

    selected_ids: tuple[str, ...]
    resolved_ids: tuple[str, ...]
    eligible_ids: tuple[str, ...]
    skipped: tuple[dict[str, str], ...]
    changes: SupportTicketBulkChanges

    @property
    def scope_token(self) -> str:
        eligible = set(self.eligible_ids)
        skipped_reasons = {item["id"]: item["reason"] for item in self.skipped}
        outcomes = [f"changes:{self.changes.fingerprint}"]
        outcomes.extend(
            (
                f"{ticket_id}:eligible"
                if ticket_id in eligible
                else (
                    f"{ticket_id}:skipped:{skipped_reasons.get(ticket_id, 'unknown')}"
                )
            )
            for ticket_id in self.selected_ids
        )
        return membership_scope_token("support-tickets:update", outcomes)

    def as_response(self) -> dict[str, object]:
        return {
            "preview": True,
            "selected_count": len(self.selected_ids),
            "matched_count": len(self.resolved_ids),
            "eligible_count": len(self.eligible_ids),
            "skipped_count": len(self.skipped),
            "eligible_ids": list(self.eligible_ids),
            "skipped": list(self.skipped),
            "changes": self.changes.as_dict(),
            "scope_token": self.scope_token,
        }


def support_ticket_bulk_static_ineligibility(ticket: Ticket) -> str | None:
    """Return row eligibility that is independent of the proposed changes."""

    if (
        ticket.merged_into_ticket_id is not None
        or support_ticket_settings.status_is_merged(ticket.status)
    ):
        return "Merged source tickets cannot be updated"
    return None


def _normalize_changes(db: Session, raw_updates: object) -> SupportTicketBulkChanges:
    if not isinstance(raw_updates, Mapping):
        raise ValueError("updates must be an object")

    status: str | None = None
    raw_status = raw_updates.get("status")
    if raw_status not in (None, ""):
        if not isinstance(raw_status, (TicketStatus, str)):
            raise ValueError("Select a configured ticket status")
        status = support_ticket_settings.normalize_ticket_status(raw_status)
        configured_statuses = set(support_ticket_settings.list_status_options(db))
        if not status or status not in configured_statuses:
            raise ValueError("Select a configured ticket status")
        if support_ticket_settings.status_is_merged(status):
            raise ValueError("Use the ticket merge workflow to set merged status")

    priority: str | None = None
    if raw_updates.get("priority") not in (None, ""):
        priority = support_ticket_settings.normalize_system_value(
            str(raw_updates.get("priority"))
        )
        if priority not in set(support_ticket_settings.list_priority_options(db)):
            raise ValueError("Select a configured ticket priority")

    assigned_to_person_id: str | None = None
    if raw_updates.get("assigned_to_person_id") not in (None, ""):
        try:
            assigned_to_person_id = str(UUID(str(raw_updates["assigned_to_person_id"])))
        except ValueError as exc:
            raise ValueError("Select a valid ticket assignee") from exc
        if not support_service.assignment_person_option(db, assigned_to_person_id):
            raise ValueError("Selected ticket assignee was not found")

    changes = SupportTicketBulkChanges(
        status=status,
        priority=priority,
        assigned_to_person_id=assigned_to_person_id,
    )
    if not changes.as_dict():
        raise ValueError("Choose at least one ticket field to update")
    return changes


def _ticket_for_preview(db: Session, raw_id: str) -> Ticket | None:
    try:
        ticket_id = UUID(raw_id)
    except ValueError:
        return None
    ticket = db.get(Ticket, ticket_id)
    return ticket if ticket and ticket.is_active else None


def _already_matches(ticket: Ticket, changes: SupportTicketBulkChanges) -> bool:
    proposed = changes.as_dict()
    return all(
        str(getattr(ticket, key) or "") == value for key, value in proposed.items()
    )


def preview_support_ticket_bulk_update(
    db: Session, payload: Mapping[str, object]
) -> SupportTicketBulkPreview:
    """Resolve selected membership and update eligibility without side effects."""

    try:
        selection = parse_bulk_selection(
            payload,
            allowed_filter_keys=(),
            filtered_selection_supported=False,
        )
        changes = _normalize_changes(db, payload.get("updates"))
    except ValueError as exc:
        raise SupportTicketBulkError(
            code="support_ticket_bulk_invalid",
            message=str(exc),
        ) from exc
    resolved_ids: list[str] = []
    eligible_ids: list[str] = []
    skipped: list[dict[str, str]] = []
    for selected_id in selection.ids:
        ticket = _ticket_for_preview(db, selected_id)
        if ticket is None:
            skipped.append({"id": selected_id, "reason": "Ticket not found"})
            continue
        ticket_id = str(ticket.id)
        resolved_ids.append(ticket_id)
        reason = support_ticket_bulk_static_ineligibility(ticket)
        if reason is None and _already_matches(ticket, changes):
            reason = "Ticket already matches the requested values"
        if reason:
            skipped.append({"id": ticket_id, "reason": reason})
        else:
            eligible_ids.append(ticket_id)
    return SupportTicketBulkPreview(
        selected_ids=selection.ids,
        resolved_ids=tuple(resolved_ids),
        eligible_ids=tuple(eligible_ids),
        skipped=tuple(skipped),
        changes=changes,
    )


def require_support_ticket_bulk_confirmation(
    db: Session, payload: Mapping[str, object]
) -> SupportTicketBulkPreview:
    """Reject execution unless membership, eligibility, and changes match preview."""

    try:
        selection = parse_bulk_selection(
            payload,
            allowed_filter_keys=(),
            filtered_selection_supported=False,
        )
    except ValueError as exc:
        raise SupportTicketBulkError(
            code="support_ticket_bulk_invalid",
            message=str(exc),
        ) from exc
    if selection.expected_count is None or not selection.expected_scope_token:
        raise SupportTicketBulkError(
            code="support_ticket_bulk_preview_required",
            message="Preview the ticket update before confirming",
        )
    preview = preview_support_ticket_bulk_update(db, payload)
    changed = selection.expected_count != len(
        preview.resolved_ids
    ) or not hmac.compare_digest(selection.expected_scope_token, preview.scope_token)
    if changed:
        raise SupportTicketBulkError(
            code="support_ticket_bulk_scope_drift",
            message=(
                "The selected ticket scope or eligibility changed after preview. "
                "Review the updated impact before confirming again."
            ),
        )
    return preview


def execute_support_ticket_bulk_update(
    db: Session,
    payload: Mapping[str, object],
    *,
    actor_id: str | None,
    request=None,
) -> dict[str, object]:
    """Execute one confirmed update through the canonical ticket mutation owner."""

    if payload.get("confirmed") is not True:
        raise SupportTicketBulkError(
            code="support_ticket_bulk_confirmation_required",
            message="Ticket update confirmation required",
        )
    preview = require_support_ticket_bulk_confirmation(db, payload)
    items = [
        TicketBulkUpdateItem(
            ticket_id=UUID(ticket_id),
            status=(
                TicketStatus(preview.changes.status) if preview.changes.status else None
            ),
            priority=preview.changes.priority,
            assigned_to_person_id=(
                UUID(preview.changes.assigned_to_person_id)
                if preview.changes.assigned_to_person_id
                else None
            ),
        )
        for ticket_id in preview.eligible_ids
    ]
    updated = (
        _execute_bulk_owner(
            db,
            items=items,
            actor_id=actor_id,
            request=request,
        )
        if items
        else []
    )
    processed_ids = [str(ticket.id) for ticket in updated]
    message = (
        f"Updated {len(processed_ids)} of {len(preview.selected_ids)} selected tickets"
    )
    if preview.skipped:
        message += f"; {len(preview.skipped)} skipped"
    return {
        "preview": False,
        "message": message,
        "selected_count": len(preview.selected_ids),
        "matched_count": len(preview.resolved_ids),
        "processed_count": len(processed_ids),
        "skipped_count": len(preview.skipped),
        "processed_ids": processed_ids,
        "skipped": list(preview.skipped),
    }


def _execute_bulk_owner(
    db: Session,
    *,
    items: list[TicketBulkUpdateItem],
    actor_id: str | None,
    request,
):
    db_session_adapter.release_read_transaction(db)
    return support_service.tickets.bulk_update(
        db,
        TicketBulkUpdateRequest(items=items),
        actor_id=actor_id,
        request=request,
    )
