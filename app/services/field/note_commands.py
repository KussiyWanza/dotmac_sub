"""Typed owner command for durable native field-work-order notes."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import false, func, or_, select
from sqlalchemy.orm import Session, selectinload
from sqlalchemy.sql.elements import ColumnElement

from app.models.dispatch import TechnicianProfile
from app.models.field_attachment import FieldAttachment
from app.models.field_note import FieldWorkOrderNote
from app.models.subscriber import Subscriber
from app.models.system_user import SystemUser
from app.models.work_order import WorkOrder
from app.services.domain_errors import DomainError
from app.services.events.owner_outputs import (
    OwnerOutputEnvelope,
    stage_owner_output,
)
from app.services.events.types import EventType
from app.services.field.jobs import _annotate_vendor_membership, _scoped_query
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

OWNER = "operations.field_notes"


@dataclass(frozen=True, slots=True)
class CreateFieldWorkOrderNote:
    context: CommandContext
    requester_system_user_id: UUID
    work_order_public_id: str
    request_id: UUID
    body: str
    is_internal: bool
    attachment_ids: tuple[UUID, ...] = ()


@dataclass(frozen=True, slots=True)
class FieldNoteAttachmentOutcome:
    id: UUID
    work_order_id: str
    note_id: UUID | None
    kind: str
    file_name: str
    mime_type: str
    size_bytes: int
    uploaded_by_person_id: UUID
    created_at: datetime
    download_path: str
    latitude: float | None = None
    longitude: float | None = None
    captured_at: datetime | None = None
    signer_name: str | None = None
    uploaded_by_system_user_id: UUID | None = None
    client_ref: UUID | None = None
    asset_type: str | None = None
    asset_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class FieldNoteCreationOutcome:
    id: UUID
    client_ref: UUID
    body: str
    is_internal: bool
    author_person_id: UUID
    author_name: str
    created_at: datetime
    attachments: tuple[FieldNoteAttachmentOutcome, ...]
    replayed: bool


@dataclass(frozen=True, slots=True)
class WorkOrderFieldNoteScope:
    work_order_public_id: str


@dataclass(frozen=True, slots=True)
class ProjectTaskFieldNoteScope:
    project_task_id: UUID


@dataclass(frozen=True, slots=True)
class OriginTicketFieldNoteScope:
    origin_ticket_id: UUID


StaffFieldNoteScope = (
    WorkOrderFieldNoteScope | ProjectTaskFieldNoteScope | OriginTicketFieldNoteScope
)


@dataclass(frozen=True, slots=True)
class StaffFieldNoteAccess:
    global_access: bool = False
    reseller_ids: tuple[UUID, ...] = ()
    regions: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ListStaffFieldWorkOrderNotes:
    scope: StaffFieldNoteScope
    access: StaffFieldNoteAccess
    limit: int = 200
    offset: int = 0


@dataclass(frozen=True, slots=True)
class StaffFieldNoteAttachmentView:
    id: UUID
    file_name: str
    mime_type: str
    size_bytes: int
    kind: str
    download_path: str


@dataclass(frozen=True, slots=True)
class StaffFieldWorkOrderNoteView:
    id: UUID
    client_ref: UUID | None
    work_order_id: UUID
    work_order_public_id: str
    work_order_title: str
    project_task_id: UUID | None
    origin_ticket_id: UUID | None
    body: str
    is_internal: bool
    author_person_id: UUID
    author_system_user_id: UUID | None
    author_name: str
    created_at: datetime
    attachments: tuple[StaffFieldNoteAttachmentView, ...]


@dataclass(frozen=True, slots=True)
class StaffFieldNoteListOutcome:
    items: tuple[StaffFieldWorkOrderNoteView, ...]
    total: int
    limit: int
    offset: int


@dataclass(frozen=True, slots=True)
class GetStaffFieldNoteAttachment:
    work_order_public_id: str
    attachment_id: UUID


@dataclass(frozen=True, slots=True)
class StaffFieldNoteAttachmentAccess:
    attachment_id: UUID
    stored_file_id: UUID
    file_name: str
    mime_type: str
    size_bytes: int


class FieldNoteCommandError(DomainError):
    """Stable, transport-neutral rejection from the field-note owner."""


class FieldNoteQueryError(DomainError):
    """Stable rejection from the staff field-note read boundary."""


_CREATE_NOTE = OwnerCommandDefinition(
    owner=OWNER,
    concern="native field work-order note creation",
    name="create_field_work_order_note",
)


def _error(suffix: str, message: str, **details: object) -> FieldNoteCommandError:
    return FieldNoteCommandError(
        code=f"{OWNER}.{suffix}", message=message, details=details
    )


def _query_error(suffix: str, message: str, **details: object) -> FieldNoteQueryError:
    return FieldNoteQueryError(
        code=f"{OWNER}.{suffix}", message=message, details=details
    )


def _fingerprint(command: CreateFieldWorkOrderNote, body: str) -> str:
    payload = {
        "work_order_public_id": command.work_order_public_id,
        "body": body,
        "is_internal": command.is_internal,
        "attachment_ids": sorted(str(item) for item in command.attachment_ids),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _author_name(profile: TechnicianProfile, user: SystemUser) -> str:
    display_name = user.display_name or f"{user.first_name} {user.last_name}".strip()
    if display_name:
        return display_name
    metadata = profile.metadata_ if isinstance(profile.metadata_, dict) else {}
    for key in ("name", "display_name"):
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return profile.workforce_employee_reference or str(profile.person_id)


def _attachment_outcome(
    attachment: FieldAttachment, *, work_order_id: str
) -> FieldNoteAttachmentOutcome:
    return FieldNoteAttachmentOutcome(
        id=attachment.id,
        work_order_id=work_order_id,
        note_id=attachment.note_id,
        kind=attachment.kind,
        file_name=attachment.file_name,
        mime_type=attachment.mime_type,
        size_bytes=attachment.size_bytes,
        latitude=attachment.latitude,
        longitude=attachment.longitude,
        captured_at=attachment.captured_at,
        signer_name=attachment.signer_name,
        uploaded_by_person_id=attachment.uploaded_by_person_id,
        uploaded_by_system_user_id=attachment.uploaded_by_system_user_id,
        client_ref=attachment.client_ref,
        asset_type=attachment.asset_type,
        asset_id=attachment.asset_id,
        created_at=attachment.created_at,
        download_path=f"/api/v1/field/attachments/{attachment.id}/content",
    )


def _outcome(
    note: FieldWorkOrderNote,
    *,
    work_order_id: str,
    replayed: bool,
    attachments: tuple[FieldAttachment, ...] | None = None,
) -> FieldNoteCreationOutcome:
    assert note.client_ref is not None
    resolved_attachments = attachments
    if resolved_attachments is None:
        resolved_attachments = tuple(
            item for item in note.attachments_ if item.is_active
        )
    return FieldNoteCreationOutcome(
        id=note.id,
        client_ref=note.client_ref,
        body=note.body,
        is_internal=note.is_internal,
        author_person_id=note.author_person_id,
        author_name=note.author_name or str(note.author_person_id),
        created_at=note.created_at,
        attachments=tuple(
            _attachment_outcome(item, work_order_id=work_order_id)
            for item in resolved_attachments
        ),
        replayed=replayed,
    )


def _attachments(
    db: Session,
    *,
    profile: TechnicianProfile,
    work_order: WorkOrder,
    attachment_ids: tuple[UUID, ...],
) -> tuple[FieldAttachment, ...]:
    if len(set(attachment_ids)) != len(attachment_ids):
        raise _error("invalid_request", "An attachment can be linked only once.")
    resolved: list[FieldAttachment] = []
    for attachment_id in attachment_ids:
        attachment = db.get(FieldAttachment, attachment_id)
        if attachment is None or not attachment.is_active:
            raise _error("attachment_not_found", "Attachment not found.")
        if attachment.work_order_mirror_id != work_order.id:
            raise _error("invalid_request", "Attachment belongs to a different job.")
        if attachment.uploaded_by_technician_id != profile.id:
            raise _error(
                "attachment_forbidden", "Attachment was uploaded by someone else."
            )
        if attachment.note_id is not None:
            raise _error("invalid_request", "Attachment is already linked to a note.")
        resolved.append(attachment)
    return tuple(resolved)


def create_field_work_order_note(
    db: Session, command: CreateFieldWorkOrderNote
) -> FieldNoteCreationOutcome:
    body = command.body.strip()
    fingerprint = _fingerprint(command, body)

    def operation() -> FieldNoteCreationOutcome:
        if not body:
            raise _error("invalid_request", "Note body is required.")

        # The actor row is the deterministic lock for this actor-scoped
        # idempotency key. Concurrent retries therefore serialize before the
        # check-and-create section, while the unique index remains the database
        # arbiter.
        user = db.execute(
            select(SystemUser)
            .where(
                SystemUser.id == command.requester_system_user_id,
                SystemUser.is_active.is_(True),
            )
            .with_for_update()
        ).scalar_one_or_none()
        if user is None:
            raise _error("requester_not_found", "Technician profile not found.")

        existing = db.execute(
            select(FieldWorkOrderNote).where(
                FieldWorkOrderNote.author_system_user_id == user.id,
                FieldWorkOrderNote.client_ref == command.request_id,
            )
        ).scalar_one_or_none()
        if existing is not None:
            metadata = (
                existing.metadata_ if isinstance(existing.metadata_, dict) else {}
            )
            if (
                existing.work_order_mirror.public_id != command.work_order_public_id
                or metadata.get("command_fingerprint") != fingerprint
            ):
                raise _error(
                    "idempotency_conflict",
                    "This note request identity was already used with different details.",
                )
            return _outcome(
                existing,
                work_order_id=command.work_order_public_id,
                replayed=True,
            )

        profile = db.execute(
            select(TechnicianProfile).where(
                TechnicianProfile.is_active.is_(True),
                or_(
                    TechnicianProfile.system_user_id == user.id,
                    TechnicianProfile.person_id == user.id,
                ),
            )
        ).scalar_one_or_none()
        if profile is None:
            raise _error("requester_not_found", "Technician profile not found.")
        profile = _annotate_vendor_membership(db, profile)
        work_order = (
            _scoped_query(db, profile)
            .filter(WorkOrder.public_id == command.work_order_public_id)
            .with_for_update()
            .one_or_none()
        )
        if work_order is None:
            raise _error("work_order_not_found", "Job not found.")

        attachments = _attachments(
            db,
            profile=profile,
            work_order=work_order,
            attachment_ids=command.attachment_ids,
        )
        note = FieldWorkOrderNote(
            work_order_mirror_id=work_order.id,
            author_technician_id=profile.id,
            author_person_id=profile.person_id,
            author_system_user_id=user.id,
            author_name=_author_name(profile, user),
            client_ref=command.request_id,
            body=body,
            is_internal=command.is_internal,
            attachments=[
                {
                    "id": str(item.id),
                    "kind": item.kind,
                    "file_name": item.file_name,
                    "mime_type": item.mime_type,
                    "size_bytes": item.size_bytes,
                }
                for item in attachments
            ],
            metadata_={
                "source": "native",
                "command_fingerprint": fingerprint,
            },
        )
        db.add(note)
        db.flush()
        for attachment in attachments:
            attachment.note_id = note.id
        db.flush()
        stage_owner_output(
            db,
            OwnerOutputEnvelope(
                event_type=EventType.field_work_order_note_created,
                producer_owner=OWNER,
                source_kind="field_work_order_note",
                source_id=note.id,
                occurred_at=note.created_at,
            ),
            {
                "note_id": str(note.id),
                "work_order_id": work_order.public_id,
                "author_system_user_id": str(user.id),
                "is_internal": note.is_internal,
                "attachment_count": len(attachments),
            },
            context=command.context,
        )
        return _outcome(
            note,
            work_order_id=work_order.public_id,
            replayed=False,
            attachments=attachments,
        )

    return execute_owner_command(
        db,
        definition=_CREATE_NOTE,
        context=command.context,
        operation=operation,
    )


def _staff_note_scope_predicate(scope: StaffFieldNoteScope) -> ColumnElement[bool]:
    if isinstance(scope, WorkOrderFieldNoteScope):
        public_id = scope.work_order_public_id.strip()
        if not public_id:
            raise _query_error("invalid_request", "Work-order identity is required.")
        return WorkOrder.public_id == public_id
    if isinstance(scope, ProjectTaskFieldNoteScope):
        return WorkOrder.project_task_id == scope.project_task_id
    if isinstance(scope, OriginTicketFieldNoteScope):
        return WorkOrder.origin_ticket_id == scope.origin_ticket_id
    raise _query_error("invalid_request", "A supported field-note scope is required.")


def _staff_note_access_predicate(
    access: StaffFieldNoteAccess,
) -> ColumnElement[bool] | None:
    if access.global_access:
        return None
    predicates: list[ColumnElement[bool]] = []
    if access.reseller_ids:
        predicates.append(Subscriber.reseller_id.in_(access.reseller_ids))
    normalized_regions = tuple(
        dict.fromkeys(region.strip() for region in access.regions if region.strip())
    )
    if normalized_regions:
        predicates.append(Subscriber.region.in_(normalized_regions))
    if not predicates:
        return false()
    return or_(*predicates)


def _staff_attachment_view(
    attachment: FieldAttachment,
    *,
    work_order_public_id: str,
) -> StaffFieldNoteAttachmentView:
    return StaffFieldNoteAttachmentView(
        id=attachment.id,
        file_name=attachment.file_name,
        mime_type=attachment.mime_type,
        size_bytes=attachment.size_bytes,
        kind=attachment.kind,
        download_path=(
            f"/admin/dispatch/work-orders/{work_order_public_id}/notes/attachments/"
            f"{attachment.id}"
        ),
    )


def _staff_note_view(
    note: FieldWorkOrderNote, work_order: WorkOrder
) -> StaffFieldWorkOrderNoteView:
    attachments = tuple(
        _staff_attachment_view(attachment, work_order_public_id=work_order.public_id)
        for attachment in sorted(
            (item for item in note.attachments_ if item.is_active),
            key=lambda item: (item.created_at, item.id),
        )
    )
    return StaffFieldWorkOrderNoteView(
        id=note.id,
        client_ref=note.client_ref,
        work_order_id=work_order.id,
        work_order_public_id=work_order.public_id,
        work_order_title=work_order.title,
        project_task_id=work_order.project_task_id,
        origin_ticket_id=work_order.origin_ticket_id,
        body=note.body,
        is_internal=note.is_internal,
        author_person_id=note.author_person_id,
        author_system_user_id=note.author_system_user_id,
        author_name=note.author_name or str(note.author_person_id),
        created_at=note.created_at,
        attachments=attachments,
    )


def list_staff_field_work_order_notes(
    db: Session, query: ListStaffFieldWorkOrderNotes
) -> StaffFieldNoteListOutcome:
    """Read canonical field notes for one authorized staff UI context."""

    if query.limit < 1 or query.limit > 200:
        raise _query_error("invalid_request", "Note limit must be between 1 and 200.")
    if query.offset < 0:
        raise _query_error("invalid_request", "Note offset cannot be negative.")
    predicates = [_staff_note_scope_predicate(query.scope)]
    access_predicate = _staff_note_access_predicate(query.access)
    if access_predicate is not None:
        predicates.append(access_predicate)
    count = db.execute(
        select(func.count(FieldWorkOrderNote.id))
        .select_from(FieldWorkOrderNote)
        .join(WorkOrder, WorkOrder.id == FieldWorkOrderNote.work_order_mirror_id)
        .join(Subscriber, Subscriber.id == WorkOrder.subscriber_id)
        .where(*predicates)
    ).scalar_one()
    rows = db.execute(
        select(FieldWorkOrderNote, WorkOrder)
        .join(WorkOrder, WorkOrder.id == FieldWorkOrderNote.work_order_mirror_id)
        .join(Subscriber, Subscriber.id == WorkOrder.subscriber_id)
        .options(selectinload(FieldWorkOrderNote.attachments_))
        .where(*predicates)
        .order_by(FieldWorkOrderNote.created_at.desc(), FieldWorkOrderNote.id.desc())
        .offset(query.offset)
        .limit(query.limit)
    ).all()
    return StaffFieldNoteListOutcome(
        items=tuple(_staff_note_view(note, work_order) for note, work_order in rows),
        total=int(count),
        limit=query.limit,
        offset=query.offset,
    )


def get_staff_field_note_attachment(
    db: Session, query: GetStaffFieldNoteAttachment
) -> StaffFieldNoteAttachmentAccess:
    """Resolve an active attachment that belongs to a canonical field note."""

    attachment = db.execute(
        select(FieldAttachment)
        .join(FieldWorkOrderNote, FieldWorkOrderNote.id == FieldAttachment.note_id)
        .join(WorkOrder, WorkOrder.id == FieldWorkOrderNote.work_order_mirror_id)
        .where(
            FieldAttachment.id == query.attachment_id,
            FieldAttachment.is_active.is_(True),
            WorkOrder.public_id == query.work_order_public_id,
        )
    ).scalar_one_or_none()
    if attachment is None:
        raise _query_error("attachment_not_found", "Attachment not found.")
    return StaffFieldNoteAttachmentAccess(
        attachment_id=attachment.id,
        stored_file_id=attachment.stored_file_id,
        file_name=attachment.file_name,
        mime_type=attachment.mime_type,
        size_bytes=attachment.size_bytes,
    )


__all__ = [
    "CreateFieldWorkOrderNote",
    "FieldNoteAttachmentOutcome",
    "FieldNoteCommandError",
    "FieldNoteCreationOutcome",
    "FieldNoteQueryError",
    "GetStaffFieldNoteAttachment",
    "ListStaffFieldWorkOrderNotes",
    "OriginTicketFieldNoteScope",
    "ProjectTaskFieldNoteScope",
    "StaffFieldNoteAttachmentAccess",
    "StaffFieldNoteAttachmentView",
    "StaffFieldNoteAccess",
    "StaffFieldNoteListOutcome",
    "StaffFieldWorkOrderNoteView",
    "WorkOrderFieldNoteScope",
    "create_field_work_order_note",
    "get_staff_field_note_attachment",
    "list_staff_field_work_order_notes",
]
