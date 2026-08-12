"""Native field attachment metadata and private file access."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.field_attachment import FIELD_ATTACHMENT_KINDS, FieldAttachment
from app.models.stored_file import StoredFile
from app.models.work_order import WorkOrder
from app.services.field.jobs import _profile_from_principal, _scoped_query
from app.services.file_storage import FileValidationError, file_uploads
from app.services.object_storage import ObjectNotFoundError, StreamResult


def _download_path(attachment_id: UUID) -> str:
    return f"/api/v1/field/attachments/{attachment_id}/content"


def serialize_attachment(attachment: FieldAttachment) -> dict:
    return {
        "id": attachment.id,
        "work_order_id": attachment.work_order_mirror.public_id,
        "note_id": attachment.note_id,
        "kind": attachment.kind,
        "file_name": attachment.file_name,
        "mime_type": attachment.mime_type,
        "size_bytes": attachment.size_bytes,
        "latitude": attachment.latitude,
        "longitude": attachment.longitude,
        "captured_at": attachment.captured_at,
        "signer_name": attachment.signer_name,
        "uploaded_by_person_id": attachment.uploaded_by_person_id,
        "uploaded_by_system_user_id": attachment.uploaded_by_system_user_id,
        "client_ref": attachment.client_ref,
        "asset_type": attachment.asset_type,
        "asset_id": attachment.asset_id,
        "created_at": attachment.created_at,
        "download_path": _download_path(attachment.id),
    }


class FieldAttachments:
    @staticmethod
    def create(
        db: Session,
        principal: dict[str, Any],
        *,
        kind: str,
        file_name: str,
        mime_type: str | None,
        content: bytes,
        client_ref: UUID | None = None,
        crm_work_order_id: str | None = None,
        note_id: UUID | None = None,
        latitude: float | None = None,
        longitude: float | None = None,
        captured_at: datetime | None = None,
        signer_name: str | None = None,
        asset_type: str | None = None,
        asset_id: UUID | None = None,
    ) -> dict:
        normalized_kind = kind.strip().lower()
        if normalized_kind not in FIELD_ATTACHMENT_KINDS:
            raise HTTPException(
                status_code=422, detail=f"Unsupported attachment kind: {kind}"
            )
        if not content:
            raise HTTPException(status_code=422, detail="Empty file")
        if not crm_work_order_id and note_id is None:
            raise HTTPException(
                status_code=422, detail="Attachment must reference a job or note"
            )
        if (asset_type and asset_id is None) or (asset_id and not asset_type):
            raise HTTPException(
                status_code=422,
                detail="asset_type and asset_id must be provided together",
            )

        profile = _profile_from_principal(db, principal)
        row = _resolve_work_order(db, principal, crm_work_order_id, note_id)
        if client_ref:
            existing = (
                db.query(FieldAttachment)
                .filter(FieldAttachment.client_ref == client_ref)
                .filter(FieldAttachment.uploaded_by_person_id == profile.person_id)
                .one_or_none()
            )
            if existing is not None:
                return serialize_attachment(existing)

        try:
            stored = file_uploads.upload(
                db=db,
                domain="attachments",
                entity_type="field_attachment",
                entity_id=row.public_id,
                original_filename=file_name or "upload",
                content_type=mime_type,
                data=content,
                uploaded_by=None,
                owner_subscriber_id=None,
            )
        except FileValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        attachment = FieldAttachment(
            work_order_mirror_id=row.id,
            note_id=note_id,
            stored_file_id=stored.id,
            kind=normalized_kind,
            file_name=stored.original_filename,
            mime_type=stored.content_type or mime_type or "application/octet-stream",
            size_bytes=stored.file_size,
            latitude=latitude,
            longitude=longitude,
            captured_at=captured_at,
            signer_name=signer_name,
            uploaded_by_technician_id=profile.id,
            uploaded_by_person_id=profile.person_id,
            uploaded_by_system_user_id=profile.system_user_id,
            client_ref=client_ref,
            asset_type=asset_type,
            asset_id=asset_id,
        )
        db.add(attachment)
        db.commit()
        db.refresh(attachment)
        return serialize_attachment(attachment)

    @staticmethod
    def list(
        db: Session,
        principal: dict[str, Any],
        *,
        crm_work_order_id: str | None = None,
        note_id: UUID | None = None,
        kind: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        row = _resolve_work_order(db, principal, crm_work_order_id, note_id)
        query = (
            db.query(FieldAttachment)
            .filter(FieldAttachment.work_order_mirror_id == row.id)
            .filter(FieldAttachment.is_active.is_(True))
        )
        if note_id is not None:
            query = query.filter(FieldAttachment.note_id == note_id)
        if kind:
            query = query.filter(FieldAttachment.kind == kind.strip().lower())
        rows = (
            query.order_by(FieldAttachment.created_at.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )
        return [serialize_attachment(row) for row in rows]

    @staticmethod
    def get(
        db: Session,
        principal: dict[str, Any],
        attachment_id: str,
    ) -> FieldAttachment:
        attachment = db.get(FieldAttachment, attachment_id)
        if attachment is None or not attachment.is_active:
            raise HTTPException(status_code=404, detail="Attachment not found")
        _resolve_work_order(
            db,
            principal,
            None,
            None,
            work_order_mirror_id=attachment.work_order_mirror_id,
        )
        return attachment

    @staticmethod
    def get_content(
        db: Session,
        principal: dict[str, Any],
        attachment_id: str,
    ) -> tuple[FieldAttachment, StreamResult]:
        attachment = FieldAttachments.get(db, principal, attachment_id)
        stored_file = db.get(StoredFile, attachment.stored_file_id)
        if stored_file is None or stored_file.is_deleted:
            raise HTTPException(status_code=404, detail="Attachment content not found")
        try:
            return attachment, file_uploads.stream_file(stored_file)
        except ObjectNotFoundError as exc:
            raise HTTPException(
                status_code=404, detail="Attachment content not found"
            ) from exc

    @staticmethod
    def delete(db: Session, principal: dict[str, Any], attachment_id: str) -> None:
        attachment = FieldAttachments.get(db, principal, attachment_id)
        stored_file = db.get(StoredFile, attachment.stored_file_id)
        attachment.is_active = False
        if stored_file is not None and not stored_file.is_deleted:
            file_uploads.soft_delete(db=db, file=stored_file, hard_delete_object=True)
        db.commit()


def _resolve_work_order(
    db: Session,
    principal: dict[str, Any],
    crm_work_order_id: str | None,
    note_id: UUID | None,
    work_order_mirror_id: UUID | None = None,
) -> WorkOrder:
    from app.models.field_note import FieldWorkOrderNote

    profile = _profile_from_principal(db, principal)
    query = _scoped_query(db, profile)
    if work_order_mirror_id is not None:
        query = query.filter(WorkOrder.id == work_order_mirror_id)
    elif note_id is not None:
        note = db.get(FieldWorkOrderNote, note_id)
        if note is None:
            raise HTTPException(status_code=404, detail="Note not found")
        query = query.filter(WorkOrder.id == note.work_order_mirror_id)
    elif crm_work_order_id:
        query = query.filter(WorkOrder.public_id == crm_work_order_id)
    else:
        raise HTTPException(status_code=422, detail="crm_work_order_id is required")

    row = query.one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return row


def parse_captured_at(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail="Invalid captured_at timestamp"
        ) from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


field_attachments = FieldAttachments()
