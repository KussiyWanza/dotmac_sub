"""Native field notes for imported work-order mirrors."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.field_note import FieldWorkOrderNote
from app.models.work_order import WorkOrder
from app.services.field.jobs import (
    _profile_from_principal,
    _scoped_query,
)


def _serialize(note: FieldWorkOrderNote) -> dict:
    attachments = [
        _serialize_attachment(attachment)
        for attachment in getattr(note, "attachments_", []) or []
        if attachment.is_active
    ]
    return {
        "id": note.id,
        "client_ref": note.client_ref,
        "body": note.body,
        "is_internal": note.is_internal,
        "author_person_id": note.author_person_id,
        "author_name": note.author_name,
        "created_at": note.created_at,
        "attachments": attachments,
    }


class FieldNotes:
    @staticmethod
    def list_for_job(
        db: Session,
        principal: dict[str, Any],
        crm_work_order_id: str,
    ) -> list[dict]:
        row = _scoped_work_order(db, principal, crm_work_order_id)
        notes = (
            db.query(FieldWorkOrderNote)
            .filter(FieldWorkOrderNote.work_order_mirror_id == row.id)
            .order_by(FieldWorkOrderNote.created_at.asc())
            .all()
        )
        return [_serialize(note) for note in notes]


def _scoped_work_order(
    db: Session,
    principal: dict[str, Any],
    crm_work_order_id: str,
) -> WorkOrder:
    profile = _profile_from_principal(db, principal)
    row = (
        _scoped_query(db, profile)
        .filter(WorkOrder.public_id == crm_work_order_id)
        .one_or_none()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return row


field_notes = FieldNotes()


def _serialize_attachment(attachment) -> dict:
    from app.services.field.attachments import serialize_attachment

    return serialize_attachment(attachment)
