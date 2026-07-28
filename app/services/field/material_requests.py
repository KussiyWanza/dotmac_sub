"""Native field material requests."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException
from sqlalchemy.orm import Session, selectinload

from app.models.field_material import (
    FIELD_MATERIAL_REQUEST_PRIORITIES,
    FIELD_MATERIAL_REQUEST_STATUSES,
    FieldInventoryItem,
    FieldMaterialRequest,
    FieldMaterialRequestItem,
    FieldWorkOrderMaterial,
)
from app.models.work_order import WorkOrder
from app.services.common import apply_pagination, coerce_uuid
from app.services.field.jobs import _profile_from_principal, _scoped_query
from app.services.field.source import (
    mark_sub_authoritative as _mark_source_authoritative,
)

logger = logging.getLogger(__name__)

_BACKOFFICE_ISSUED_STATUSES = frozenset(
    {"issued", "fulfilled", "complete", "completed"}
)
_BACKOFFICE_REFUSED_STATUSES = frozenset(
    {"cancelled", "canceled", "rejected", "declined", "denied"}
)


def serialize_material_request(request: FieldMaterialRequest) -> dict:
    return {
        "id": request.id,
        "crm_work_order_id": request.work_order_mirror.public_id,
        "crm_material_request_id": request.crm_material_request_id,
        "requested_by_person_id": request.requested_by_person_id,
        "requested_by_system_user_id": request.requested_by_system_user_id,
        "status": request.status,
        "priority": request.priority,
        "notes": request.notes,
        "source_warehouse_code": request.source_warehouse_code,
        "support_system": request.support_system,
        "support_reference": request.support_reference,
        "support_status": request.support_status,
        "client_ref": request.client_ref,
        "submitted_at": request.submitted_at,
        "approved_at": request.approved_at,
        "rejected_at": request.rejected_at,
        "fulfilled_at": request.fulfilled_at,
        "created_at": request.created_at,
        "updated_at": request.updated_at,
        "items": [
            {
                "id": item.id,
                "item_id": item.item_id,
                "sku": item.item.sku if item.item else None,
                "name": item.item.name if item.item else None,
                "unit": item.item.unit if item.item else None,
                "quantity": item.quantity,
                "notes": item.notes,
                "serial_numbers": item.serial_numbers or [],
            }
            for item in request.items
        ],
    }


class FieldMaterialRequests:
    @staticmethod
    def list_mine(
        db: Session,
        principal: dict[str, Any],
        *,
        crm_work_order_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        profile = _profile_from_principal(db, principal)
        scoped = _scoped_query(db, profile)
        if crm_work_order_id:
            scoped = scoped.filter(WorkOrder.public_id == crm_work_order_id)
        scoped_ids = scoped.with_entities(WorkOrder.id)
        query = (
            db.query(FieldMaterialRequest)
            .options(
                selectinload(FieldMaterialRequest.items).selectinload(
                    FieldMaterialRequestItem.item
                )
            )
            .filter(FieldMaterialRequest.work_order_mirror_id.in_(scoped_ids))
            .filter(FieldMaterialRequest.is_active.is_(True))
            .order_by(FieldMaterialRequest.created_at.desc())
        )
        if status:
            query = query.filter(FieldMaterialRequest.status == _status(status))
        return [
            serialize_material_request(request)
            for request in apply_pagination(query, limit, offset).all()
        ]

    @staticmethod
    def get(
        db: Session,
        principal: dict[str, Any],
        material_request_id: str,
    ) -> dict:
        request = _get_scoped_request(db, principal, material_request_id)
        return serialize_material_request(request)

    @staticmethod
    def create(
        db: Session,
        principal: dict[str, Any],
        *,
        crm_work_order_id: str,
        priority: str,
        notes: str | None,
        items: list[dict[str, Any]],
        source_warehouse_code: str | None = None,
    ) -> dict:
        if not items:
            raise HTTPException(status_code=422, detail="At least one item is required")
        profile = _profile_from_principal(db, principal)
        row = (
            _scoped_query(db, profile)
            .filter(WorkOrder.public_id == crm_work_order_id)
            .one_or_none()
        )
        if row is None:
            raise HTTPException(status_code=404, detail="Job not found")
        planned_items = _validate_items(db, items)
        request = FieldMaterialRequest(
            work_order_mirror_id=row.id,
            requested_by_technician_id=profile.id,
            requested_by_person_id=profile.person_id,
            requested_by_system_user_id=profile.system_user_id,
            status="draft",
            priority=_priority(priority),
            notes=(notes or "").strip() or None,
            source_warehouse_code=(source_warehouse_code or "").strip() or None,
        )
        db.add(request)
        db.flush()
        for item, quantity, notes, serial_numbers in planned_items:
            request.items.append(
                FieldMaterialRequestItem(
                    item_id=item.id,
                    quantity=quantity,
                    notes=notes,
                    serial_numbers=serial_numbers,
                )
            )
        _mark_sub_authoritative(row)
        db.commit()
        db.refresh(request)
        return serialize_material_request(request)

    @staticmethod
    def submit(
        db: Session,
        principal: dict[str, Any],
        material_request_id: str,
    ) -> dict:
        request = _get_scoped_request(db, principal, material_request_id)
        profile = _profile_from_principal(db, principal)
        if request.requested_by_technician_id != profile.id:
            raise HTTPException(status_code=404, detail="Material request not found")
        if request.status != "draft":
            raise HTTPException(status_code=409, detail="Only draft requests submit")
        request.status = "submitted"
        request.submitted_at = datetime.now(UTC)
        _mark_sub_authoritative(request.work_order_mirror)
        db.commit()
        db.refresh(request)
        return serialize_material_request(request)

    @staticmethod
    def list_all(
        db: Session,
        *,
        status: str | None = None,
        crm_work_order_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        """Manager view: material requests across all technicians."""
        query = (
            db.query(FieldMaterialRequest)
            .options(
                selectinload(FieldMaterialRequest.items).selectinload(
                    FieldMaterialRequestItem.item
                )
            )
            .filter(FieldMaterialRequest.is_active.is_(True))
            .order_by(FieldMaterialRequest.created_at.desc())
        )
        if status:
            query = query.filter(FieldMaterialRequest.status == _status(status))
        if crm_work_order_id:
            work_order_ids = db.query(WorkOrder.id).filter(
                WorkOrder.public_id == crm_work_order_id
            )
            query = query.filter(
                FieldMaterialRequest.work_order_mirror_id.in_(work_order_ids)
            )
        return [
            serialize_material_request(request)
            for request in apply_pagination(query, limit, offset).all()
        ]

    @staticmethod
    def approve(db: Session, material_request_id: str) -> dict:
        """Approve a submitted request and stage configured back-office support.

        The Sub decision remains valid if the replaceable back-office provider
        is unavailable. When an adapter is available, its outbox intent is
        committed atomically with this source transition.
        """
        request = _get_request(db, material_request_id)
        if request.status != "submitted":
            raise HTTPException(
                status_code=409, detail="Only submitted requests approve"
            )
        request.status = "approved"
        request.approved_at = datetime.now(UTC)
        _note_request_event(request, "approved")
        _mark_sub_authoritative(request.work_order_mirror)
        # The approval's committed output drives the receipted ERP-issue
        # request through the materials lifecycle projection handler; a
        # failed enqueue stays a failed retryable delivery instead of a
        # metadata breadcrumb.
        from app.services.events import EventType, emit_event

        emit_event(
            db,
            EventType.field_material_request_approved,
            {
                "material_request_id": str(request.id),
                "work_order_mirror_id": str(request.work_order_mirror_id),
                "client_ref": request.client_ref,
                "source_warehouse_code": request.source_warehouse_code,
                "approved_at": request.approved_at.isoformat(),
            },
            actor="operations.material_dependencies",
        )
        db.commit()
        db.refresh(request)
        return serialize_material_request(request)

    @staticmethod
    def reject(db: Session, material_request_id: str, reason: str) -> dict:
        request = _get_request(db, material_request_id)
        if request.status != "submitted":
            raise HTTPException(
                status_code=409, detail="Only submitted requests reject"
            )
        cleaned = (reason or "").strip()
        if not cleaned:
            raise HTTPException(status_code=422, detail="reason is required")
        request.status = "rejected"
        request.rejected_at = datetime.now(UTC)
        _note_request_event(request, "rejected", reason=cleaned[:500])
        _mark_sub_authoritative(request.work_order_mirror)
        db.commit()
        db.refresh(request)
        return serialize_material_request(request)

    @staticmethod
    def issue(db: Session, material_request_id: str) -> dict:
        _require_legacy_material_fulfilment(db)
        request = _get_request(db, material_request_id)
        if request.status != "approved":
            raise HTTPException(status_code=409, detail="Only approved requests issue")
        _sync_work_order_materials(db, request, status="reserved")
        request.status = "issued"
        _note_request_event(request, "issued")
        _mark_sub_authoritative(request.work_order_mirror)
        db.commit()
        db.refresh(request)
        return serialize_material_request(request)

    @staticmethod
    def fulfill(db: Session, material_request_id: str) -> dict:
        _require_legacy_material_fulfilment(db)
        request = _get_request(db, material_request_id)
        if request.status not in {"approved", "issued"}:
            raise HTTPException(
                status_code=409, detail="Only approved or issued requests fulfill"
            )
        _sync_work_order_materials(db, request, status="reserved")
        request.status = "fulfilled"
        request.fulfilled_at = datetime.now(UTC)
        _note_request_event(request, "fulfilled")
        _mark_sub_authoritative(request.work_order_mirror)
        db.commit()
        db.refresh(request)
        return serialize_material_request(request)

    @staticmethod
    def apply_backoffice_outcome(
        db: Session,
        request: FieldMaterialRequest,
        *,
        support_system: str,
        support_reference: str | None,
        support_status: str | None,
    ) -> bool:
        """Project one back-office material outcome into the service workflow.

        The configured provider decides stock availability, serial allocation,
        issue, and cancellation. This resolver is Sub's only writer for the
        resulting material-dependency state.
        """
        changed = False
        normalized_status = _normalize_backoffice_status(support_status)
        normalized_system = str(support_system or "").strip().lower()[:40]
        if not normalized_system:
            raise ValueError("Back-office support outcome requires a source system")

        if request.support_system not in {None, normalized_system}:
            raise ValueError(
                "Back-office support system changed for "
                f"{request.id}: {request.support_system} -> {normalized_system}"
            )
        if request.support_system != normalized_system:
            request.support_system = normalized_system
            changed = True

        if support_reference:
            normalized_id = str(support_reference)[:120]
            if request.support_reference not in {None, normalized_id}:
                raise ValueError(
                    "Back-office material request identity changed for "
                    f"{request.id}: {request.support_reference} -> {normalized_id}"
                )
            if request.support_reference != normalized_id:
                request.support_reference = normalized_id
                changed = True

        if normalized_status and request.support_status != normalized_status:
            request.support_status = normalized_status
            changed = True

        if normalized_status in _BACKOFFICE_ISSUED_STATUSES:
            _sync_work_order_materials(db, request, status="reserved")
            if request.status in {"approved", "issued"}:
                request.status = "fulfilled"
                request.fulfilled_at = request.fulfilled_at or datetime.now(UTC)
                _note_request_event(request, "backoffice_material_issued")
                from app.services.events import EventType, emit_event

                emit_event(
                    db,
                    EventType.field_material_request_fulfilled,
                    {
                        "material_request_id": str(request.id),
                        "work_order_mirror_id": str(request.work_order_mirror_id),
                        "support_system": request.support_system,
                        "support_reference": request.support_reference,
                        "support_status": request.support_status,
                    },
                    actor="operations.material_dependencies",
                )
                changed = True
        elif normalized_status in _BACKOFFICE_REFUSED_STATUSES:
            if request.status in {"approved", "issued"}:
                request.status = "canceled"
                _note_request_event(
                    request,
                    "backoffice_material_refused",
                    reason=f"Back-office outcome: {normalized_status}",
                )
                changed = True

        if changed:
            _mark_sub_authoritative(request.work_order_mirror)
        return changed


def _get_scoped_request(
    db: Session,
    principal: dict[str, Any],
    material_request_id: str,
) -> FieldMaterialRequest:
    profile = _profile_from_principal(db, principal)
    scoped_ids = _scoped_query(db, profile).with_entities(WorkOrder.id)
    request = (
        db.query(FieldMaterialRequest)
        .options(
            selectinload(FieldMaterialRequest.items).selectinload(
                FieldMaterialRequestItem.item
            )
        )
        .filter(FieldMaterialRequest.id == coerce_uuid(material_request_id))
        .filter(FieldMaterialRequest.work_order_mirror_id.in_(scoped_ids))
        .filter(FieldMaterialRequest.is_active.is_(True))
        .one_or_none()
    )
    if request is None:
        raise HTTPException(status_code=404, detail="Material request not found")
    return request


def _get_request(db: Session, material_request_id: str) -> FieldMaterialRequest:
    request = (
        db.query(FieldMaterialRequest)
        .options(
            selectinload(FieldMaterialRequest.items).selectinload(
                FieldMaterialRequestItem.item
            )
        )
        .filter(FieldMaterialRequest.id == coerce_uuid(material_request_id))
        .filter(FieldMaterialRequest.is_active.is_(True))
        .one_or_none()
    )
    if request is None:
        raise HTTPException(status_code=404, detail="Material request not found")
    return request


def _note_request_event(
    request: FieldMaterialRequest, event: str, *, reason: str | None = None
) -> None:
    metadata = dict(request.metadata_ or {})
    events = list(metadata.get("manager_events") or [])
    event_payload: dict[str, Any] = {
        "event": event,
        "occurred_at": datetime.now(UTC).isoformat(),
    }
    if reason:
        event_payload["reason"] = reason
    events.append(event_payload)
    metadata["manager_events"] = events[-20:]
    if reason:
        metadata["rejection_reason"] = reason
    request.metadata_ = metadata


def _sync_work_order_materials(
    db: Session, request: FieldMaterialRequest, *, status: str
) -> None:
    existing = {
        row.item_id: row
        for row in db.query(FieldWorkOrderMaterial)
        .filter(
            FieldWorkOrderMaterial.work_order_mirror_id == request.work_order_mirror_id
        )
        .filter(FieldWorkOrderMaterial.is_active.is_(True))
        .all()
    }
    for requested_item in request.items:
        row = existing.get(requested_item.item_id)
        if row is None:
            row = FieldWorkOrderMaterial(
                work_order_mirror_id=request.work_order_mirror_id,
                item_id=requested_item.item_id,
                allocated_quantity=requested_item.quantity,
                consumed_quantity=0,
                status=status,
                notes=requested_item.notes,
                metadata_={"material_request_id": str(request.id)},
            )
            db.add(row)
            continue
        row.allocated_quantity = max(row.allocated_quantity, requested_item.quantity)
        row.status = (
            "used" if row.consumed_quantity >= row.allocated_quantity else status
        )
        if requested_item.notes:
            row.notes = requested_item.notes
        metadata = dict(row.metadata_ or {})
        metadata["material_request_id"] = str(request.id)
        row.metadata_ = metadata


def _item(db: Session, item_id) -> FieldInventoryItem:
    item = db.get(FieldInventoryItem, item_id)
    if item is None or not item.is_active:
        raise HTTPException(status_code=404, detail="Material item not found")
    return item


def _validate_items(
    db: Session, items: list[dict[str, Any]]
) -> list[tuple[FieldInventoryItem, int, str | None, list[str]]]:
    planned: list[tuple[FieldInventoryItem, int, str | None, list[str]]] = []
    seen: set[str] = set()
    for entry in items:
        item = _item(db, entry.get("item_id"))
        item_key = str(item.id)
        if item_key in seen:
            raise HTTPException(status_code=422, detail="Duplicate item_id in request")
        seen.add(item_key)
        serial_numbers = [
            str(value).strip()
            for value in (entry.get("serial_numbers") or [])
            if str(value).strip()
        ]
        if len(serial_numbers) != len(set(serial_numbers)):
            raise HTTPException(status_code=422, detail="Duplicate serial number")
        planned.append(
            (
                item,
                _quantity(entry.get("quantity")),
                (entry.get("notes") or "").strip() or None,
                serial_numbers,
            )
        )
    return planned


def _quantity(value) -> int:
    try:
        quantity = int(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=422, detail="quantity must be an integer"
        ) from exc
    if quantity <= 0:
        raise HTTPException(
            status_code=422, detail="quantity must be greater than zero"
        )
    return quantity


def _priority(value: str) -> str:
    priority = (value or "medium").strip().lower()
    if priority not in FIELD_MATERIAL_REQUEST_PRIORITIES:
        raise HTTPException(status_code=422, detail=f"Unsupported priority: {value}")
    return priority


def _status(value: str) -> str:
    status = (value or "").strip().lower()
    if status not in FIELD_MATERIAL_REQUEST_STATUSES:
        raise HTTPException(status_code=422, detail=f"Unsupported status: {value}")
    return status


def _normalize_backoffice_status(value: str | None) -> str | None:
    if not value:
        return None
    normalized = str(value).strip().lower().replace("-", "_").replace(" ", "_")
    return normalized[:40] or None


def _require_legacy_material_fulfilment(db: Session) -> None:
    """Keep the old local transition available only before explicit cutover."""
    from app.services.backoffice import external_material_fulfilment_active

    if external_material_fulfilment_active(db):
        raise HTTPException(
            status_code=409,
            detail=(
                "The configured back-office system owns material issue and "
                "fulfilment after cutover; reconcile its outcome instead"
            ),
        )


def _mark_sub_authoritative(row: WorkOrder) -> None:
    _mark_source_authoritative(row, "material_requests")


# --- receipted lifecycle-output consumption --------------------------------


def consume_material_request_approved(
    db: Session,
    *,
    material_request_id: str,
    event_id,
    context,
) -> str | None:
    """Receipt one committed approval into the ERP-issue request.

    The outbox intent and its unique ``(consumer, event_id)`` receipt commit
    atomically; a redelivery is an exact no-op, and a failed enqueue stays a
    failed retryable delivery. Sub never infers issuance — the ERP outcome
    returns through the durable outbox observation projection.
    """
    from app.services.events.owner_outputs import consume_owner_output
    from app.services.owner_commands import (
        OwnerCommandDefinition,
        execute_owner_command,
    )

    definition = OwnerCommandDefinition(
        owner="operations.material_dependencies",
        concern="committed material output consumption",
        name="consume_material_request_approved",
    )

    def _effect() -> str:
        from app.services.backoffice import enqueue_material_request_outbox

        request = db.get(FieldMaterialRequest, coerce_uuid(material_request_id))
        if request is None:
            return "skipped_missing"
        if request.status != "approved":
            # Stale replay after refusal, fulfilment, or cancellation.
            return "skipped_state"
        event = enqueue_material_request_outbox(db, request)
        return "enqueued" if event is not None else "skipped_not_owned"

    return execute_owner_command(
        db,
        definition=definition,
        context=context,
        operation=lambda: consume_owner_output(
            db,
            consumer="operations.material_dependencies",
            event_id=event_id,
            event_type="field_material_request.approved",
            producer_owner="operations.material_dependencies",
            context=context,
            operation=_effect,
        )[0],
    )


field_material_requests = FieldMaterialRequests()
