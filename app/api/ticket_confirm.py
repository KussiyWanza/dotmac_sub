from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.support import canonical_ticket_status_value
from app.services import support as support_service
from app.services.db_session_adapter import db_session_adapter

router = APIRouter(prefix="/ticket-confirm", tags=["ticket-confirm"])


class TicketDisputeRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    reason: str | None = None


def _load_token_or_error(db: Session, token: str):
    token_row = support_service.ticket_access_tokens.get_by_token(db, token)
    state = support_service.ticket_access_tokens.token_state(token_row)
    if state == "not_found":
        raise HTTPException(status_code=404, detail="Confirmation link not found.")
    if state == "expired":
        raise HTTPException(status_code=410, detail="Confirmation link has expired.")
    if state == "closed":
        raise HTTPException(
            status_code=409,
            detail="This confirmation link has already been used or closed.",
        )
    assert token_row is not None
    return token_row


@router.get("/{token}")
def get_confirmation_state(token: str, db: Session = Depends(get_db)) -> dict:
    token_row = _load_token_or_error(db, token)
    db_session_adapter.release_read_transaction(db)
    support_service.ticket_access_tokens.mark_accessed(db, token_row)
    ticket = token_row.ticket
    return {
        "available": True,
        "ticket_id": str(ticket.id),
        "ticket_ref": ticket.number or str(ticket.id),
        "subject": ticket.title,
        "status": canonical_ticket_status_value(ticket.status),
        "resolved_at": ticket.resolved_at.isoformat() if ticket.resolved_at else None,
    }


@router.post("/{token}/confirm")
def confirm_resolution(token: str, db: Session = Depends(get_db)) -> dict:
    token_row = _load_token_or_error(db, token)
    db_session_adapter.release_read_transaction(db)
    ticket = support_service.tickets.confirm_resolution(db, token_row)
    return {
        "ok": True,
        "ticket_id": str(ticket.id),
        "ticket_ref": ticket.number or str(ticket.id),
        "status": canonical_ticket_status_value(ticket.status),
    }


@router.post("/{token}/dispute")
def dispute_resolution(
    token: str,
    payload: TicketDisputeRequest,
    db: Session = Depends(get_db),
) -> dict:
    token_row = _load_token_or_error(db, token)
    db_session_adapter.release_read_transaction(db)
    ticket = support_service.tickets.dispute_resolution(
        db, token_row, reason=payload.reason
    )
    return {
        "ok": True,
        "ticket_id": str(ticket.id),
        "ticket_ref": ticket.number or str(ticket.id),
        "status": canonical_ticket_status_value(ticket.status),
    }
