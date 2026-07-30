"""Global persisted OLT autofind inventory helpers."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from urllib.parse import quote_plus

from sqlalchemy import ColumnElement, func, or_, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import SQLColumnExpression
from starlette.requests import Request

from app.models.network import AuthorizationPreset, OLTDevice, OntAssignment, OntUnit
from app.models.ont_autofind import OltAutofindCandidate
from app.services.network._common import normalize_mac_address
from app.services.network.olt_web_audit import log_olt_audit_event
from app.services.network.serial_utils import normalize as normalize_serial
from app.services.network.serial_utils import (
    search_candidates as serial_search_candidates,
)

logger = logging.getLogger(__name__)


def _normalize_serial(value: str | None) -> str:
    return normalize_serial(value)


def _candidate_serial_values(candidate: OltAutofindCandidate) -> list[str]:
    values: list[str] = []
    for value in (candidate.serial_number, candidate.serial_hex):
        for serial in serial_search_candidates(value):
            normalized = normalize_serial(serial)
            if normalized and normalized not in values:
                values.append(normalized)
    return values


def _normalized_serial_expr(column: SQLColumnExpression[str]) -> ColumnElement[str]:
    """Build a SQL expression that normalizes a serial number column for comparison."""
    expr = func.upper(column)
    for token in ("-", " ", ":", ".", "_", "/"):
        expr = func.replace(expr, token, "")
    return expr


def _find_ont_by_serial(db: Session, serial_number: str | None) -> OntUnit | None:
    candidates = [
        normalize_serial(candidate)
        for candidate in serial_search_candidates(serial_number)
    ]
    candidates = [candidate for candidate in dict.fromkeys(candidates) if candidate]
    if not candidates:
        return None
    stmt = (
        select(OntUnit)
        .where(_normalized_serial_expr(OntUnit.serial_number).in_(candidates))
        .where(OntUnit.is_active.is_(True))
        .order_by(OntUnit.updated_at.desc(), OntUnit.created_at.desc())
    )
    return db.scalars(stmt).first()


def ensure_returned_inventory_candidate(
    db: Session,
    *,
    olt_id: str | None,
    fsp: str | None,
    serial_number: str | None,
    ont_unit_id: object | None = None,
) -> tuple[bool, str]:
    """Make a returned ONT immediately selectable for authorization.

    Huawei autofind may not report a deauthorized ONT until the device physically
    re-registers. Return-to-inventory already knows the previous OLT, FSP, and
    serial, so preserve that as an active inventory candidate for reauthorization.
    """
    clean_olt_id = str(olt_id or "").strip()
    clean_fsp = str(fsp or "").strip()
    clean_serial = str(serial_number or "").strip()
    serial_variants = [
        candidate
        for candidate in dict.fromkeys(serial_search_candidates(clean_serial))
        if candidate
    ]
    normalized_serials = {
        normalize_serial(candidate) for candidate in serial_variants if candidate
    }
    if not clean_olt_id or not clean_fsp or not normalized_serials:
        return False, "Missing OLT, port, or serial for returned inventory candidate"

    now = datetime.now(UTC)
    candidates = list(
        db.scalars(
            select(OltAutofindCandidate).where(
                OltAutofindCandidate.olt_id == clean_olt_id,
                OltAutofindCandidate.fsp == clean_fsp,
            )
        ).all()
    )
    candidate = next(
        (
            item
            for item in candidates
            if normalized_serials.intersection(_candidate_serial_values(item))
        ),
        None,
    )

    display_serial = next(
        (
            variant
            for variant in serial_variants
            if len(normalize_serial(variant)) == 12
            and normalize_serial(variant)[:4].isalpha()
        ),
        clean_serial,
    )
    serial_hex = next(
        (
            normalize_serial(variant)
            for variant in serial_variants
            if len(normalize_serial(variant)) == 16
            and normalize_serial(variant).startswith("48575443")
        ),
        None,
    )

    if candidate is None:
        candidate = OltAutofindCandidate(
            olt_id=clean_olt_id,
            ont_unit_id=ont_unit_id,
            fsp=clean_fsp,
            serial_number=display_serial,
            serial_hex=serial_hex,
            is_active=True,
            first_seen_at=now,
            last_seen_at=now,
            notes="Restored from return-to-inventory for immediate reauthorization.",
        )
        db.add(candidate)
        action = "created"
    else:
        candidate.ont_unit_id = ont_unit_id or candidate.ont_unit_id
        candidate.is_active = True
        candidate.resolution_reason = None
        candidate.resolved_at = None
        candidate.last_seen_at = now
        if not candidate.serial_hex:
            candidate.serial_hex = serial_hex
        candidate.notes = (
            "Restored from return-to-inventory for immediate reauthorization."
        )
        action = "restored"

    db.flush()
    return True, f"Returned inventory candidate {action}"


def restore_candidate_by_serial(
    db: Session,
    *,
    serial_number: str | None,
    ont_unit_id: object | None = None,
) -> tuple[bool, str]:
    """Restore an autofind candidate by serial number when OLT/FSP is unknown.

    Used by return-to-inventory when the ONT's OLT binding was already cleared
    before the return. Finds the most recent candidate matching the serial and
    restores it to active state for reauthorization.
    """
    clean_serial = str(serial_number or "").strip()
    serial_variants = [
        candidate
        for candidate in dict.fromkeys(serial_search_candidates(clean_serial))
        if candidate
    ]
    normalized_serials = [
        normalized
        for normalized in dict.fromkeys(
            normalize_serial(candidate) for candidate in serial_variants if candidate
        )
        if normalized
    ]
    if not normalized_serials:
        return False, "No serial number provided"

    candidate = db.scalars(
        select(OltAutofindCandidate)
        .where(
            or_(
                _normalized_serial_expr(OltAutofindCandidate.serial_number).in_(
                    normalized_serials
                ),
                _normalized_serial_expr(OltAutofindCandidate.serial_hex).in_(
                    normalized_serials
                ),
            )
        )
        .order_by(
            OltAutofindCandidate.last_seen_at.desc().nulls_last(),
            OltAutofindCandidate.created_at.desc(),
        )
    ).first()

    if candidate is None:
        return False, "No autofind candidate found for serial"

    if candidate.is_active:
        return True, "Candidate already active"

    now = datetime.now(UTC)
    candidate.ont_unit_id = ont_unit_id or candidate.ont_unit_id
    candidate.is_active = True
    candidate.resolution_reason = None
    candidate.resolved_at = None
    candidate.last_seen_at = now
    candidate.notes = "Restored from return-to-inventory by serial lookup."

    db.flush()
    return True, "Restored candidate by serial lookup"


def resolve_candidate_authorized(
    db: Session,
    *,
    olt_id: str,
    fsp: str,
    serial_number: str,
) -> None:
    """Mark a cached autofind candidate as authorized/resolved.

    Uses SELECT FOR UPDATE with skip_locked to prevent race conditions
    when multiple authorization workflows target the same candidate.
    """
    serial_candidates = [
        normalize_serial(candidate)
        for candidate in serial_search_candidates(serial_number)
    ]
    serial_candidates = [
        candidate for candidate in dict.fromkeys(serial_candidates) if candidate
    ]
    if not serial_candidates:
        return

    candidate = db.scalars(
        select(OltAutofindCandidate)
        .where(
            OltAutofindCandidate.olt_id == olt_id,
            OltAutofindCandidate.fsp == fsp,
            or_(
                _normalized_serial_expr(OltAutofindCandidate.serial_number).in_(
                    serial_candidates
                ),
                _normalized_serial_expr(OltAutofindCandidate.serial_hex).in_(
                    serial_candidates
                ),
            ),
            OltAutofindCandidate.is_active.is_(True),  # Only resolve active candidates
        )
        .with_for_update(skip_locked=True)
    ).first()
    if not candidate:
        # Either not found, already resolved, or locked by another transaction
        return
    candidate.is_active = False
    candidate.resolution_reason = "authorized"
    candidate.resolved_at = datetime.now(UTC)
    matched_ont = _find_ont_by_serial(db, serial_number)
    if matched_ont:
        candidate.ont_unit_id = matched_ont.id
    db.flush()  # Let caller control transaction boundary


def restore_candidate(db: Session, *, candidate_id: str) -> tuple[bool, str]:
    """Restore a disappeared autofind candidate to active state.

    Uses SELECT FOR UPDATE with skip_locked to prevent race conditions
    when multiple restore requests target the same candidate.

    Args:
        db: Database session
        candidate_id: UUID of the autofind candidate

    Returns:
        Tuple of (success, message)
    """
    from app.services.common import coerce_uuid

    uuid_id = coerce_uuid(candidate_id)
    candidate = db.scalars(
        select(OltAutofindCandidate)
        .where(OltAutofindCandidate.id == uuid_id)
        .with_for_update(skip_locked=True)
    ).first()
    if not candidate:
        return False, "Autofind candidate not found or locked by another operation"
    if candidate.is_active:
        return False, "Candidate is already active"
    if candidate.resolution_reason != "disappeared":
        return (
            False,
            f"Cannot restore candidate with resolution: {candidate.resolution_reason}",
        )

    candidate.is_active = True
    candidate.resolution_reason = None
    candidate.resolved_at = None
    matched_ont = _find_ont_by_serial(db, candidate.serial_number)
    if matched_ont:
        candidate.ont_unit_id = matched_ont.id
    candidate.last_seen_at = datetime.now(UTC)
    db.flush()
    logger.info(
        "autofind_candidate_restored",
        extra={
            "event": "autofind_candidate_restored",
            "candidate_id": str(candidate.id),
            "serial_number": candidate.serial_number,
            "olt_id": str(candidate.olt_id),
        },
    )
    return True, f"Restored autofind candidate {candidate.serial_number}"


def restore_candidate_audited(
    db: Session, *, candidate_id: str, request: Request | None = None
) -> tuple[bool, str]:
    ok, message = restore_candidate(db, candidate_id=candidate_id)
    status = "success" if ok else "error"
    log_olt_audit_event(
        db,
        request=request,
        action="restore_autofind_candidate",
        entity_type="olt_autofind_candidate",
        entity_id=candidate_id,
        metadata={"result": status, "message": message},
        status_code=200 if ok else 400,
        is_success=ok,
    )
    return ok, message


def refresh_autofind_from_olt(
    db: Session,
    *,
    olt_id: str,
) -> tuple[bool, str, dict[str, int]]:
    """Refresh persisted autofind candidates from one OLT via explicit SSH query."""
    from app.services.common import coerce_uuid
    from app.services.network.olt_ssh_ont.autofind import query_ont_autofind

    uuid_id = coerce_uuid(olt_id)
    olt = db.get(OLTDevice, uuid_id)
    if olt is None:
        return False, "OLT not found", {"created": 0, "updated": 0, "disappeared": 0}

    ok, message, entries = query_ont_autofind(olt)
    if not ok:
        return False, message, {"created": 0, "updated": 0, "disappeared": 0}

    now = datetime.now(UTC)
    created = 0
    updated = 0
    seen_ids: set[object] = set()

    for entry in entries:
        serial_number = str(entry.serial_number or entry.serial_hex or "").strip()
        normalized_serial = normalize_serial(serial_number)
        if not normalized_serial or not entry.fsp:
            continue

        serial_variants = [
            normalize_serial(candidate)
            for candidate in serial_search_candidates(serial_number)
        ]
        if entry.serial_hex:
            serial_variants.extend(
                normalize_serial(candidate)
                for candidate in serial_search_candidates(entry.serial_hex)
            )
        serial_variants = [value for value in dict.fromkeys(serial_variants) if value]

        candidate = db.scalars(
            select(OltAutofindCandidate)
            .where(
                OltAutofindCandidate.olt_id == olt.id,
                OltAutofindCandidate.fsp == entry.fsp,
                or_(
                    _normalized_serial_expr(OltAutofindCandidate.serial_number).in_(
                        serial_variants
                    ),
                    _normalized_serial_expr(OltAutofindCandidate.serial_hex).in_(
                        serial_variants
                    ),
                ),
            )
            .with_for_update(skip_locked=True)
        ).first()
        matched_ont = _find_ont_by_serial(db, serial_number)

        if candidate is None:
            candidate = OltAutofindCandidate(
                olt_id=olt.id,
                ont_unit_id=matched_ont.id if matched_ont else None,
                fsp=entry.fsp,
                serial_number=serial_number,
                serial_hex=entry.serial_hex or None,
                vendor_id=entry.vendor_id or None,
                model=entry.model or None,
                software_version=entry.software_version or None,
                mac=normalize_mac_address(entry.mac),
                equipment_sn=entry.equipment_sn or None,
                autofind_time=entry.autofind_time or None,
                is_active=True,
                first_seen_at=now,
                last_seen_at=now,
                notes="Discovered via explicit OLT autofind refresh",
            )
            db.add(candidate)
            db.flush()
            created += 1
        else:
            candidate.ont_unit_id = (
                matched_ont.id if matched_ont else candidate.ont_unit_id
            )
            candidate.serial_hex = entry.serial_hex or candidate.serial_hex
            candidate.vendor_id = entry.vendor_id or candidate.vendor_id
            candidate.model = entry.model or candidate.model
            candidate.software_version = (
                entry.software_version or candidate.software_version
            )
            candidate.mac = normalize_mac_address(entry.mac) or candidate.mac
            candidate.equipment_sn = entry.equipment_sn or candidate.equipment_sn
            candidate.autofind_time = entry.autofind_time or candidate.autofind_time
            candidate.is_active = True
            candidate.resolution_reason = None
            candidate.resolved_at = None
            candidate.last_seen_at = now
            candidate.notes = "Refreshed via explicit OLT autofind query"
            updated += 1

        seen_ids.add(candidate.id)

    disappeared = 0
    active_candidates = list(
        db.scalars(
            select(OltAutofindCandidate)
            .where(OltAutofindCandidate.olt_id == olt.id)
            .where(OltAutofindCandidate.is_active.is_(True))
            .with_for_update(skip_locked=True)
        ).all()
    )
    for candidate in active_candidates:
        if candidate.id in seen_ids:
            continue
        candidate.is_active = False
        candidate.resolution_reason = "disappeared"
        candidate.resolved_at = now
        candidate.last_seen_at = now
        candidate.notes = "Marked disappeared by explicit OLT autofind refresh"
        disappeared += 1

    db.flush()  # Let caller control transaction boundary
    stats = {"created": created, "updated": updated, "disappeared": disappeared}
    return (
        True,
        (
            f"Autofind refreshed for {olt.name}: "
            f"{created} created, {updated} updated, {disappeared} disappeared"
        ),
        stats,
    )


def refresh_autofind_from_olt_audited(
    db: Session,
    *,
    olt_id: str,
    request: Request | None = None,
) -> tuple[bool, str, dict[str, int]]:
    ok, message, stats = refresh_autofind_from_olt(db, olt_id=olt_id)
    log_olt_audit_event(
        db,
        request=request,
        action="refresh_autofind_candidates",
        entity_type="olt",
        entity_id=olt_id,
        metadata={"result": "success" if ok else "error", "message": message, **stats},
        status_code=200 if ok else 400,
        is_success=ok,
    )
    return ok, message, stats


def build_unconfigured_onts_page_data(
    db: Session,
    *,
    search: str | None = None,
    olt_id: str | None = None,
    view: str | None = None,
    resolution: str | None = None,
) -> dict[str, object]:
    """Build page data for the global unconfigured ONT inventory."""
    selected_view = (view or "active").strip().lower()
    if selected_view not in {"active", "history", "all"}:
        selected_view = "active"
    selected_resolution = (resolution or "").strip().lower()
    if selected_resolution not in {"authorized", "commissioned", "disappeared"}:
        selected_resolution = ""

    # Build query using SQLAlchemy 2.0 select() pattern
    stmt = select(OltAutofindCandidate, OLTDevice).join(
        OLTDevice, OLTDevice.id == OltAutofindCandidate.olt_id
    )
    if selected_view == "active":
        stmt = stmt.where(OltAutofindCandidate.is_active.is_(True))
    elif selected_view == "history":
        stmt = stmt.where(OltAutofindCandidate.is_active.is_(False))
    if selected_resolution:
        stmt = stmt.where(OltAutofindCandidate.resolution_reason == selected_resolution)
    if olt_id:
        stmt = stmt.where(OltAutofindCandidate.olt_id == olt_id)
    if search:
        terms = [
            f"%{candidate}%"
            for candidate in dict.fromkeys(
                [search.strip(), *serial_search_candidates(search)]
            )
            if candidate
        ]
        search_filters = []
        for term in terms:
            search_filters.extend(
                [
                    OltAutofindCandidate.serial_number.ilike(term),
                    OltAutofindCandidate.serial_hex.ilike(term),
                    OltAutofindCandidate.fsp.ilike(term),
                    OltAutofindCandidate.model.ilike(term),
                    OltAutofindCandidate.mac.ilike(term),
                    OLTDevice.name.ilike(term),
                    OltAutofindCandidate.resolution_reason.ilike(term),
                ]
            )
        if search_filters:
            stmt = stmt.where(or_(*search_filters))

    stmt = stmt.order_by(
        func.coalesce(
            OltAutofindCandidate.resolved_at, OltAutofindCandidate.last_seen_at
        ).desc(),
        OLTDevice.name.asc(),
        OltAutofindCandidate.fsp.asc(),
    )
    rows = db.execute(stmt).all()
    candidate_ids = [candidate.id for candidate, _olt in rows]
    ont_ids = [
        candidate.ont_unit_id
        for candidate, _olt in rows
        if candidate.ont_unit_id is not None
    ]
    assigned_ont_ids = {
        str(ont_id)
        for ont_id in db.scalars(
            select(OntAssignment.ont_unit_id).where(
                OntAssignment.ont_unit_id.in_(ont_ids),
                OntAssignment.active.is_(True),
            )
        ).all()
    }
    from app.services.network.ont_commissioning import latest_intents_for_candidates

    commissioning_by_candidate = latest_intents_for_candidates(db, candidate_ids)
    presets = list(
        db.scalars(
            select(AuthorizationPreset)
            .where(
                AuthorizationPreset.is_active.is_(True),
            )
            .order_by(
                AuthorizationPreset.is_default.desc(),
                AuthorizationPreset.priority.desc(),
                AuthorizationPreset.name.asc(),
            )
        ).all()
    )

    def preset_options_for_olt(olt_device_id: object) -> list[dict[str, object]]:
        olt_key = str(olt_device_id)
        options: list[dict[str, object]] = []
        for preset in presets:
            scoped_olt_id = getattr(preset, "olt_device_id", None)
            if scoped_olt_id is not None and str(scoped_olt_id) != olt_key:
                continue
            label = preset.name
            if scoped_olt_id is not None:
                label = f"{label} (OLT)"
            options.append(
                {
                    "id": str(preset.id),
                    "name": preset.name,
                    "label": label,
                    "is_default": bool(getattr(preset, "is_default", False)),
                }
            )
        return options

    entries = [
        {
            "id": str(candidate.id),
            "olt_id": str(olt.id),
            "olt_name": olt.name,
            "fsp": candidate.fsp,
            "serial_number": candidate.serial_number,
            "serial_hex": candidate.serial_hex,
            "ont_unit_id": str(candidate.ont_unit_id) if candidate.ont_unit_id else "",
            "vendor_id": candidate.vendor_id,
            "model": candidate.model,
            "mac": candidate.mac,
            "software_version": candidate.software_version,
            "autofind_time": candidate.autofind_time,
            "first_seen_at": candidate.first_seen_at,
            "last_seen_at": candidate.last_seen_at,
            "is_active": candidate.is_active,
            "resolution_reason": candidate.resolution_reason,
            "resolved_at": candidate.resolved_at,
            "notes": candidate.notes,
            "authorization_presets": preset_options_for_olt(olt.id),
            "has_active_assignment": (
                str(candidate.ont_unit_id) in assigned_ont_ids
                if candidate.ont_unit_id is not None
                else False
            ),
            "commissioning": (
                {
                    "id": str(commissioning_by_candidate[str(candidate.id)].id),
                    "state": commissioning_by_candidate[str(candidate.id)].state.value,
                    "expires_at": commissioning_by_candidate[
                        str(candidate.id)
                    ].expires_at,
                    "failure_message": commissioning_by_candidate[
                        str(candidate.id)
                    ].failure_message,
                }
                if str(candidate.id) in commissioning_by_candidate
                else None
            ),
        }
        for candidate, olt in rows
    ]
    active_total = (
        db.scalar(
            select(func.count())
            .select_from(OltAutofindCandidate)
            .where(OltAutofindCandidate.is_active.is_(True))
        )
        or 0
    )
    last_seen = db.scalar(
        select(func.max(OltAutofindCandidate.last_seen_at)).where(
            OltAutofindCandidate.is_active.is_(True)
        )
    )
    history_total = (
        db.scalar(
            select(func.count())
            .select_from(OltAutofindCandidate)
            .where(OltAutofindCandidate.is_active.is_(False))
        )
        or 0
    )
    olts_with_candidates = (
        db.scalar(
            select(func.count(func.distinct(OltAutofindCandidate.olt_id)))
            .select_from(OltAutofindCandidate)
            .where(OltAutofindCandidate.is_active.is_(True))
        )
        or 0
    )
    olts = list(
        db.scalars(
            select(OLTDevice)
            .where(OLTDevice.is_active.is_(True))
            .order_by(OLTDevice.name.asc())
        ).all()
    )
    return {
        "entries": entries,
        "search": search or "",
        "selected_olt_id": olt_id or "",
        "selected_view": selected_view,
        "selected_resolution": selected_resolution,
        "olts": olts,
        "stats": {
            "active_candidates": int(active_total),
            "history_candidates": int(history_total),
            "olts_with_candidates": int(olts_with_candidates),
            "last_seen_at": last_seen,
        },
    }


def build_unconfigured_onts_redirect_url(
    *,
    search: str | None = None,
    olt_id: str | None = None,
    view: str | None = None,
    resolution: str | None = None,
    status: str | None = None,
    message: str | None = None,
) -> str:
    """Build the canonical ONT inventory URL for unconfigured ONT filters."""
    params = ["view=unconfigured"]
    if view:
        params.append(f"candidate_view={quote_plus(view)}")
    if resolution:
        params.append(f"resolution={quote_plus(resolution)}")
    if search:
        params.append(f"search={quote_plus(search)}")
    if olt_id:
        params.append(f"olt_id={quote_plus(olt_id)}")
    if status:
        params.append(f"status={quote_plus(status)}")
    if message:
        params.append(f"message={quote_plus(message)}")
    return f"/admin/network/onts?{'&'.join(params)}"


def build_unconfigured_onts_feedback_url(*, status: str, message: str) -> str:
    """Build an unconfigured ONT redirect with a status message."""
    return build_unconfigured_onts_redirect_url(status=status, message=message)


def upsert_autofind_from_syslog(
    db: Session,
    *,
    olt_id: str,
    fsp: str,
    serial_number: str,
) -> bool:
    """Upsert a single autofind candidate from a syslog event.

    This is the direct-persistence path for syslog-based ONT discovery.
    No SSH polling, no Celery tasks - just immediate database persistence.

    Args:
        db: Database session
        olt_id: OLT device ID (string UUID)
        fsp: Frame/Slot/Port string (e.g., "0/1/2")
        serial_number: ONT serial number

    Returns:
        True if candidate was created/updated, False on error
    """
    normalized_serial = normalize_serial(serial_number)
    if not normalized_serial:
        logger.warning(
            "syslog_autofind_invalid_serial",
            extra={
                "olt_id": olt_id,
                "fsp": fsp,
                "serial_number": serial_number,
            },
        )
        return False

    now = datetime.now(UTC)

    # Find existing candidate by OLT + FSP + normalized serial
    serial_variants = [
        normalize_serial(candidate)
        for candidate in serial_search_candidates(serial_number)
    ]
    serial_variants = [v for v in dict.fromkeys(serial_variants) if v]

    candidate = db.scalars(
        select(OltAutofindCandidate)
        .where(
            OltAutofindCandidate.olt_id == olt_id,
            OltAutofindCandidate.fsp == fsp,
            or_(
                _normalized_serial_expr(OltAutofindCandidate.serial_number).in_(
                    serial_variants
                ),
                _normalized_serial_expr(OltAutofindCandidate.serial_hex).in_(
                    serial_variants
                ),
            ),
        )
        .with_for_update(skip_locked=True)
    ).first()

    # Find matching ONT unit if exists
    matched_ont = _find_ont_by_serial(db, serial_number)

    if candidate is None:
        # Create new candidate
        candidate = OltAutofindCandidate(
            olt_id=olt_id,
            ont_unit_id=matched_ont.id if matched_ont else None,
            fsp=fsp,
            serial_number=serial_number,
            is_active=True,
            first_seen_at=now,
            last_seen_at=now,
            notes="Discovered via syslog",
        )
        db.add(candidate)
        logger.debug(
            "syslog_autofind_candidate_created",
            extra={
                "olt_id": olt_id,
                "fsp": fsp,
                "serial_number": serial_number,
            },
        )
    else:
        # Update existing candidate
        candidate.ont_unit_id = matched_ont.id if matched_ont else candidate.ont_unit_id
        candidate.is_active = True
        candidate.resolution_reason = None
        candidate.resolved_at = None
        candidate.last_seen_at = now
        logger.debug(
            "syslog_autofind_candidate_updated",
            extra={
                "olt_id": olt_id,
                "fsp": fsp,
                "serial_number": serial_number,
                "candidate_id": str(candidate.id),
            },
        )

    db.flush()  # Let caller control transaction boundary
    return True
