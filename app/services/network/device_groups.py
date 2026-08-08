"""Device group services for batch device operations."""

from __future__ import annotations

import csv
import io
from typing import Any
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models.audit import AuditEvent
from app.models.network import CPEDevice, DeviceGroup, DeviceGroupMember, OntUnit
from app.services.audit_helpers import (
    humanize_action,
    load_audit_actor_subscribers,
    resolve_actor_name,
)

DEVICE_GROUP_MEMBER_TYPES = {"ont", "cpe"}
ONT_GROUP_ACTIONS = {
    "reboot",
    "factory_reset",
    "speed_update",
    "catv_toggle",
    "wifi_update",
    "voip_toggle",
    "provision",
}


class DeviceGroupError(ValueError):
    """Raised when a device group request is invalid."""


def create_device_group(
    db: Session,
    *,
    name: str,
    kind: str = "manual",
    description: str | None = None,
    criteria: dict[str, Any] | None = None,
    created_by: str | None = None,
) -> DeviceGroup:
    """Create a device group."""
    name_value = str(name or "").strip()
    if not name_value:
        raise DeviceGroupError("Device group name is required")
    group = DeviceGroup(
        name=name_value[:120],
        kind=str(kind or "manual").strip()[:40] or "manual",
        description=str(description).strip() if description else None,
        criteria=criteria,
        created_by=str(created_by).strip()[:120] if created_by else None,
        is_active=True,
    )
    db.add(group)
    db.flush()
    return group


def create_device_group_committed(
    db: Session,
    *,
    name: str,
    kind: str = "manual",
    description: str | None = None,
    criteria: dict[str, Any] | None = None,
    created_by: str | None = None,
) -> DeviceGroup:
    """Create a device group and own the transaction boundary."""
    try:
        group = create_device_group(
            db,
            name=name,
            kind=kind,
            description=description,
            criteria=criteria,
            created_by=created_by,
        )
        db.commit()
        db.refresh(group)
        return group
    except Exception:
        db.rollback()
        raise


def update_device_group(
    db: Session,
    *,
    group_id: str | UUID,
    name: str,
    description: str | None = None,
) -> DeviceGroup:
    """Update editable device group fields."""
    group = _get_active_group(db, group_id)
    name_value = str(name or "").strip()
    if not name_value:
        raise DeviceGroupError("Device group name is required")
    group.name = name_value[:120]
    group.description = str(description).strip() if description else None
    db.flush()
    return group


def update_device_group_committed(
    db: Session,
    *,
    group_id: str | UUID,
    name: str,
    description: str | None = None,
) -> DeviceGroup:
    """Update editable device group fields and own the transaction boundary."""
    try:
        group = update_device_group(
            db,
            group_id=group_id,
            name=name,
            description=description,
        )
        db.commit()
        db.refresh(group)
        return group
    except Exception:
        db.rollback()
        raise


def archive_device_group(
    db: Session,
    *,
    group_id: str | UUID,
) -> DeviceGroup:
    """Archive a group without deleting history or memberships."""
    group = _get_active_group(db, group_id)
    group.is_active = False
    db.flush()
    return group


def archive_device_group_committed(
    db: Session,
    *,
    group_id: str | UUID,
) -> DeviceGroup:
    """Archive a group and own the transaction boundary."""
    try:
        group = archive_device_group(db, group_id=group_id)
        db.commit()
        db.refresh(group)
        return group
    except Exception:
        db.rollback()
        raise


def list_device_groups(
    db: Session, *, include_inactive: bool = False
) -> list[dict[str, Any]]:
    """Return device groups with member counts for the admin UI."""
    query = select(DeviceGroup).order_by(DeviceGroup.name.asc())
    if not include_inactive:
        query = query.where(DeviceGroup.is_active.is_(True))
    groups = list(db.scalars(query))
    if not groups:
        return []
    counts = {
        (row.group_id, row.device_type): int(row.count)  # type: ignore[call-overload]
        for row in db.execute(
            select(
                DeviceGroupMember.group_id,
                DeviceGroupMember.device_type,
                func.count(DeviceGroupMember.id).label("count"),
            )
            .where(DeviceGroupMember.group_id.in_([group.id for group in groups]))
            .group_by(DeviceGroupMember.group_id, DeviceGroupMember.device_type)
        ).all()
    }
    return [
        {
            "group": group,
            "ont_count": counts.get((group.id, "ont"), 0),
            "cpe_count": counts.get((group.id, "cpe"), 0),
            "member_count": sum(
                counts.get((group.id, device_type), 0)
                for device_type in DEVICE_GROUP_MEMBER_TYPES
            ),
        }
        for group in groups
    ]


def device_group_detail_context(db: Session, group_id: str | UUID) -> dict[str, Any]:
    """Return a device group with display-ready members."""
    group = _get_active_group(db, group_id)
    members = list(
        db.scalars(
            select(DeviceGroupMember)
            .where(DeviceGroupMember.group_id == group.id)
            .order_by(DeviceGroupMember.added_at.desc())
        )
    )
    ont_ids = [member.device_id for member in members if member.device_type == "ont"]
    cpe_ids = [member.device_id for member in members if member.device_type == "cpe"]
    onts = (
        {
            ont.id: ont
            for ont in db.scalars(select(OntUnit).where(OntUnit.id.in_(ont_ids))).all()
        }
        if ont_ids
        else {}
    )
    cpes = (
        {
            cpe.id: cpe
            for cpe in db.scalars(
                select(CPEDevice).where(CPEDevice.id.in_(cpe_ids))
            ).all()
        }
        if cpe_ids
        else {}
    )
    rows = []
    for member in members:
        device = (
            onts.get(member.device_id)
            if member.device_type == "ont"
            else cpes.get(member.device_id)
        )
        rows.append(
            {
                "member": member,
                "device": device,
                "label": _device_label(member.device_type, device, member.device_id),
            }
        )
    return {
        "group": group,
        "member_rows": rows,
        "ont_count": len(ont_ids),
        "cpe_count": len(cpe_ids),
        "ont_candidates": list_device_group_member_candidates(
            db,
            group_id=group.id,
            device_type="ont",
        ),
        "cpe_candidates": list_device_group_member_candidates(
            db,
            group_id=group.id,
            device_type="cpe",
        ),
        "action_events": list_device_group_action_events(db, group_id=group.id),
        "action_history": list_device_group_action_history(db, group_id=group.id),
    }


def list_device_group_member_candidates(
    db: Session,
    *,
    group_id: str | UUID,
    device_type: str,
    search: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Return selectable devices that are not already in the group."""
    group = _get_active_group(db, group_id)
    normalized_type = _normalize_member_type(device_type)
    existing_ids = set(
        db.scalars(
            select(DeviceGroupMember.device_id)
            .where(DeviceGroupMember.group_id == group.id)
            .where(DeviceGroupMember.device_type == normalized_type)
        )
    )
    query_limit = max(1, min(int(limit or 100), 500))
    search_text = str(search or "").strip()
    if normalized_type == "ont":
        query = (
            select(OntUnit)
            .where(OntUnit.is_active.is_(True))
            .order_by(OntUnit.serial_number.asc())
            .limit(query_limit)
        )
        if existing_ids:
            query = query.where(OntUnit.id.not_in(existing_ids))
        if search_text:
            like = f"%{search_text}%"
            query = query.where(
                or_(
                    OntUnit.serial_number.ilike(like),
                    OntUnit.vendor_serial_number.ilike(like),
                    OntUnit.name.ilike(like),
                    OntUnit.mac_address.ilike(like),
                )
            )
        return [
            {
                "id": str(ont.id),
                "label": _device_label("ont", ont, ont.id),
                "detail": _device_detail("ont", ont),
            }
            for ont in db.scalars(query)
        ]

    query = select(CPEDevice).order_by(CPEDevice.created_at.desc()).limit(query_limit)  # type: ignore[assignment]
    if existing_ids:
        query = query.where(CPEDevice.id.not_in(existing_ids))
    if search_text:
        like = f"%{search_text}%"
        query = query.where(
            or_(
                CPEDevice.serial_number.ilike(like),
                CPEDevice.mac_address.ilike(like),
                CPEDevice.model.ilike(like),
                CPEDevice.vendor.ilike(like),
            )
        )
    return [
        {
            "id": str(cpe.id),
            "label": _device_label("cpe", cpe, cpe.id),
            "detail": _device_detail("cpe", cpe),
        }
        for cpe in db.scalars(query)
    ]


def list_device_group_action_events(
    db: Session,
    *,
    group_id: str | UUID,
    limit: int = 20,
) -> list[AuditEvent]:
    """Return recent device-group audit events."""
    group = _get_active_group(db, group_id)
    return list(
        db.scalars(
            select(AuditEvent)
            .where(AuditEvent.entity_type == "device_group")
            .where(AuditEvent.entity_id == str(group.id))
            .where(AuditEvent.is_active.is_(True))
            .order_by(AuditEvent.occurred_at.desc())
            .limit(max(1, min(int(limit or 20), 100)))
        )
    )


def list_device_group_action_history(
    db: Session,
    *,
    group_id: str | UUID,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Return display-ready device-group audit events with task status."""
    rows: list[dict[str, Any]] = []
    events = list_device_group_action_events(db, group_id=group_id, limit=limit)
    actors = load_audit_actor_subscribers(db, events)
    for event in events:
        metadata = dict(getattr(event, "metadata_", None) or {})
        task_id = str(metadata.get("task_id") or "").strip()
        task_state = _celery_task_state(task_id) if task_id else None
        rows.append(
            {
                "event": event,
                "actor_name": resolve_actor_name(event, actors),
                "action_label": humanize_action(event.action),
                "metadata": metadata,
                "task_id": task_id or None,
                "task_state": task_state,
            }
        )
    return rows


def add_device_group_member(
    db: Session,
    *,
    group_id: str | UUID,
    device_type: str,
    device_id: str | UUID,
    added_by: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> DeviceGroupMember:
    """Add one ONT or CPE to a group, returning an existing row if present."""
    group = _get_active_group(db, group_id)
    normalized_type = _normalize_member_type(device_type)
    normalized_id = _validate_device_exists(db, normalized_type, device_id)

    existing = db.scalars(
        select(DeviceGroupMember)
        .where(DeviceGroupMember.group_id == group.id)
        .where(DeviceGroupMember.device_type == normalized_type)
        .where(DeviceGroupMember.device_id == normalized_id)
        .limit(1)
    ).first()
    if existing is not None:
        return existing

    member = DeviceGroupMember(
        group_id=group.id,
        device_type=normalized_type,
        device_id=normalized_id,
        added_by=str(added_by).strip()[:120] if added_by else None,
        metadata_json=metadata,
    )
    db.add(member)
    db.flush()
    return member


def add_device_group_member_committed(
    db: Session,
    *,
    group_id: str | UUID,
    device_type: str,
    device_id: str | UUID,
    added_by: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> DeviceGroupMember:
    """Add one ONT or CPE to a group and own the transaction boundary."""
    try:
        member = add_device_group_member(
            db,
            group_id=group_id,
            device_type=device_type,
            device_id=device_id,
            added_by=added_by,
            metadata=metadata,
        )
        db.commit()
        db.refresh(member)
        return member
    except Exception:
        db.rollback()
        raise


def add_device_group_members_from_text(
    db: Session,
    *,
    group_id: str | UUID,
    device_type: str,
    identifiers: str,
    added_by: str | None = None,
) -> dict[str, Any]:
    """Bulk-add members from pasted CSV/text identifiers."""
    group = _get_active_group(db, group_id)
    normalized_type = _normalize_member_type(device_type)
    tokens = _parse_identifier_text(identifiers)
    if not tokens:
        raise DeviceGroupError("No device identifiers provided")

    added = 0
    existing = 0
    missing: list[str] = []
    for token in tokens:
        device_id = _resolve_device_identifier(db, normalized_type, token)
        if device_id is None:
            missing.append(token)
            continue
        before = db.scalars(
            select(DeviceGroupMember.id)
            .where(DeviceGroupMember.group_id == group.id)
            .where(DeviceGroupMember.device_type == normalized_type)
            .where(DeviceGroupMember.device_id == device_id)
            .limit(1)
        ).first()
        add_device_group_member(
            db,
            group_id=group.id,
            device_type=normalized_type,
            device_id=device_id,
            added_by=added_by,
            metadata={"source": "bulk_import", "identifier": token},
        )
        if before is None:
            added += 1
        else:
            existing += 1

    db.flush()
    return {
        "group_id": str(group.id),
        "device_type": normalized_type,
        "submitted": len(tokens),
        "added": added,
        "existing": existing,
        "missing": missing,
    }


def add_device_group_members_from_filter(
    db: Session,
    *,
    group_id: str | UUID,
    device_type: str,
    search: str,
    added_by: str | None = None,
    limit: int = 500,
) -> dict[str, Any]:
    """Bulk-add currently matching candidate devices."""
    search_text = str(search or "").strip()
    if not search_text:
        raise DeviceGroupError("Filter text is required")
    normalized_type = _normalize_member_type(device_type)
    candidates = list_device_group_member_candidates(
        db,
        group_id=group_id,
        device_type=normalized_type,
        search=search_text,
        limit=limit,
    )
    added = 0
    for item in candidates:
        add_device_group_member(
            db,
            group_id=group_id,
            device_type=normalized_type,
            device_id=item["id"],
            added_by=added_by,
            metadata={"source": "filter_import", "filter": search_text},
        )
        added += 1

    db.flush()
    return {
        "group_id": str(group_id),
        "device_type": normalized_type,
        "filter": search_text,
        "matched": len(candidates),
        "added": added,
        "limit": max(1, min(int(limit or 500), 500)),
    }


def remove_device_group_member(
    db: Session,
    *,
    group_id: str | UUID,
    member_id: str | UUID,
) -> None:
    """Remove one member from a group."""
    group = _get_active_group(db, group_id)
    try:
        member_uuid = UUID(str(member_id))
    except (TypeError, ValueError) as exc:
        raise DeviceGroupError("Invalid member id") from exc
    member = db.get(DeviceGroupMember, member_uuid)
    if member is None or member.group_id != group.id:
        raise DeviceGroupError("Device group member not found")
    db.delete(member)
    db.flush()


def remove_device_group_member_committed(
    db: Session,
    *,
    group_id: str | UUID,
    member_id: str | UUID,
) -> None:
    """Remove one member from a group and own the transaction boundary."""
    try:
        remove_device_group_member(db, group_id=group_id, member_id=member_id)
        db.commit()
    except Exception:
        db.rollback()
        raise


def list_device_group_ont_ids(db: Session, group_id: str | UUID) -> list[str]:
    """Return active ONT IDs in a group."""
    group = _get_active_group(db, group_id)
    rows = db.scalars(
        select(DeviceGroupMember.device_id)
        .join(OntUnit, OntUnit.id == DeviceGroupMember.device_id)
        .where(DeviceGroupMember.group_id == group.id)
        .where(DeviceGroupMember.device_type == "ont")
        .where(OntUnit.is_active.is_(True))
        .order_by(DeviceGroupMember.added_at.asc())
    ).all()
    return [str(row) for row in rows]


def enqueue_ont_group_action(
    db: Session,
    *,
    group_id: str | UUID,
    action: str,
    params: dict[str, Any] | None = None,
    initiated_by: str | None = None,
) -> dict[str, Any]:
    """Queue a bulk action for all ONTs in a group."""
    action_value = str(action or "").strip()
    if action_value not in ONT_GROUP_ACTIONS:
        raise DeviceGroupError(f"Unsupported ONT group action: {action_value}")
    ont_ids = list_device_group_ont_ids(db, group_id)
    if not ont_ids:
        raise DeviceGroupError("Device group has no active ONT members")

    from app.tasks.ont_bulk import execute_bulk_action

    payload = dict(params or {})
    if initiated_by:
        payload["initiated_by"] = initiated_by
    task = execute_bulk_action.delay(ont_ids, action_value, payload)
    return {
        "group_id": str(group_id),
        "action": action_value,
        "ont_count": len(ont_ids),
        "task_id": getattr(task, "id", None),
    }


def enqueue_ont_group_action_committed(
    db: Session,
    *,
    group_id: str | UUID,
    action: str,
    params: dict[str, Any] | None = None,
    initiated_by: str | None = None,
) -> dict[str, Any]:
    """Queue a bulk action for all ONTs in a group and own the transaction."""
    try:
        result = enqueue_ont_group_action(
            db,
            group_id=group_id,
            action=action,
            params=params,
            initiated_by=initiated_by,
        )
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise


def _get_active_group(db: Session, group_id: str | UUID) -> DeviceGroup:
    try:
        group_uuid = UUID(str(group_id))
    except (TypeError, ValueError) as exc:
        raise DeviceGroupError("Invalid device group id") from exc
    group = db.get(DeviceGroup, group_uuid)
    if group is None or not group.is_active:
        raise DeviceGroupError("Device group not found")
    return group


def _normalize_member_type(device_type: str) -> str:
    value = str(device_type or "").strip().lower()
    if value not in DEVICE_GROUP_MEMBER_TYPES:
        raise DeviceGroupError(f"Unsupported device group member type: {value}")
    return value


def _validate_device_exists(
    db: Session,
    device_type: str,
    device_id: str | UUID,
) -> UUID:
    try:
        device_uuid = UUID(str(device_id))
    except (TypeError, ValueError) as exc:
        raise DeviceGroupError("Invalid device id") from exc
    model = OntUnit if device_type == "ont" else CPEDevice
    if db.get(model, device_uuid) is None:
        raise DeviceGroupError(f"{device_type.upper()} device not found")
    return device_uuid


def _resolve_device_identifier(
    db: Session,
    device_type: str,
    identifier: str,
) -> UUID | None:
    text = str(identifier or "").strip()
    if not text:
        return None
    try:
        candidate_id = UUID(text)
    except (TypeError, ValueError):
        candidate_id = None
    model = OntUnit if device_type == "ont" else CPEDevice
    if candidate_id is not None and db.get(model, candidate_id) is not None:
        return candidate_id

    if device_type == "ont":
        device = db.scalars(
            select(OntUnit)
            .where(
                or_(
                    OntUnit.serial_number == text,
                    OntUnit.vendor_serial_number == text,
                    OntUnit.mac_address == text,
                )
            )
            .limit(1)
        ).first()
        return device.id if device else None

    device = db.scalars(  # type: ignore[assignment]
        select(CPEDevice)
        .where(or_(CPEDevice.serial_number == text, CPEDevice.mac_address == text))
        .limit(1)
    ).first()
    return device.id if device else None


def _device_label(device_type: str, device: Any, fallback_id: UUID) -> str:
    if device_type == "ont":
        return str(getattr(device, "serial_number", None) or fallback_id)
    return str(
        getattr(device, "serial_number", None)
        or getattr(device, "mac_address", None)
        or fallback_id
    )


def _device_detail(device_type: str, device: Any) -> str:
    if device_type == "ont":
        parts = [
            getattr(device, "name", None),
            getattr(device, "model", None),
            getattr(device, "mac_address", None),
        ]
    else:
        parts = [
            getattr(device, "vendor", None),
            getattr(device, "model", None),
            getattr(device, "mac_address", None),
        ]
    return " · ".join(str(part) for part in parts if part)


def _parse_identifier_text(value: str) -> list[str]:
    reader = csv.reader(io.StringIO(str(value or "")))
    tokens: list[str] = []
    seen: set[str] = set()
    for row in reader:
        for cell in row:
            token = str(cell or "").strip()
            if not token or token in seen:
                continue
            seen.add(token)
            tokens.append(token)
    return tokens


def _celery_task_state(task_id: str) -> dict[str, Any] | None:
    try:
        from celery.result import AsyncResult

        result = AsyncResult(task_id)
        payload: dict[str, Any] = {
            "state": str(result.state or "PENDING"),
            "ready": bool(result.ready()),
        }
        if result.ready() and result.successful():
            payload["result"] = result.result
        elif result.failed():
            payload["error"] = str(result.result)
        return payload
    except Exception:
        return None
