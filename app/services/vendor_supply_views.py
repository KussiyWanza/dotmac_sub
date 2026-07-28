"""Typed UI projections for vendor materials and mobilisation advances.

The material and advance owners decide eligibility and authoritative state.
This resolver composes those decisions with identity, permissions, timestamps,
and provider observations for web adapters. It never writes or commits.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload, selectinload

from app.models.vendor_routes import InstallationProject
from app.models.vendor_supply import (
    VendorAdvance,
    VendorAdvanceStatus,
    VendorMaterialRelease,
    VendorMaterialReleaseItem,
    VendorMaterialReleaseStatus,
)
from app.schemas.status_presentation import StatusPresentation, StatusTone
from app.services import vendor_advances, vendor_material_release
from app.services.common import coerce_uuid
from app.services.domain_errors import DomainError
from app.services.field import vendor_capabilities
from app.services.status_presentation import (
    vendor_advance_status_presentation,
    vendor_material_release_status_presentation,
)
from app.services.ui_contracts import Action, StateValue


class VendorSupplyProjectionError(DomainError):
    """Stable read-boundary error for vendor supply projections."""


SUPPLY_OBSERVATION_MAX_AGE = timedelta(minutes=15)


class VendorSupplyType(StrEnum):
    material = "material"
    advance = "advance"


class VendorSupplyReviewAction(StrEnum):
    approve = "approve"
    reject = "reject"


def _error(suffix: str, message: str) -> VendorSupplyProjectionError:
    return VendorSupplyProjectionError(
        code=f"ui.vendor_supply_projection.{suffix}",
        message=message,
    )


def _review_reason(value: str | None, *, required: bool) -> str | None:
    normalized = str(value or "").strip() or None
    if normalized is not None and len(normalized) > 2000:
        raise _error(
            "reason_too_long",
            "Review reason must be 2,000 characters or fewer.",
        )
    if required and normalized is None:
        raise _error("reason_required", "A rejection reason is required.")
    return normalized


@dataclass(frozen=True, slots=True)
class ProjectIdentity:
    id: UUID
    name: str
    code: str | None


@dataclass(frozen=True, slots=True)
class VendorIdentity:
    id: UUID
    name: str


@dataclass(frozen=True, slots=True)
class ProviderObservation:
    status: StateValue
    system: str | None
    reference: str | None
    detail: str


@dataclass(frozen=True, slots=True)
class MaterialLine:
    id: UUID
    item_code: str | None
    description: str
    unit: str | None
    quantity: int
    issued_quantity: int | None
    notes: str | None


@dataclass(frozen=True, slots=True)
class MaterialReleaseView:
    id: UUID
    project: ProjectIdentity
    vendor: VendorIdentity
    status: StatusPresentation
    items: tuple[MaterialLine, ...]
    notes: str | None
    requested_at: datetime | None
    reviewed_at: datetime | None
    review_notes: str | None
    provider: ProviderObservation
    approve_action: Action
    reject_action: Action


@dataclass(frozen=True, slots=True)
class AdvanceView:
    id: UUID
    project: ProjectIdentity
    vendor: VendorIdentity
    status: StatusPresentation
    amount: Decimal
    currency: str
    reason: str | None
    requested_at: datetime | None
    reviewed_at: datetime | None
    review_notes: str | None
    payables: ProviderObservation
    approve_action: Action
    reject_action: Action


@dataclass(frozen=True, slots=True)
class VendorSupplyProjectView:
    material_releases: tuple[MaterialReleaseView, ...]
    advances: tuple[AdvanceView, ...]
    material_request_action: Action
    advance_request_action: Action
    advance_quote_total: Decimal | None
    advance_ceiling: Decimal | None
    advance_committed: Decimal
    advance_remaining: Decimal
    advance_currency: str | None
    advance_max_percent: Decimal


@dataclass(frozen=True, slots=True)
class VendorSupplyQueue:
    items: tuple[MaterialReleaseView | AdvanceView, ...]
    count: int
    limit: int
    offset: int
    has_next: bool


@dataclass(frozen=True, slots=True)
class SupplyReviewPreview:
    supply_type: VendorSupplyType
    record_id: UUID
    project_id: UUID
    action: VendorSupplyReviewAction
    title: str
    summary: str
    details: tuple[tuple[str, str], ...]
    state: tuple[tuple[str, str], ...]
    reason: str | None


def _aware(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


def _observed_state(value: str, observed_at: datetime | None) -> StateValue:
    timestamp = _aware(observed_at)
    if timestamp is None:
        return StateValue.unavailable()
    if datetime.now(UTC) - timestamp > SUPPLY_OBSERVATION_MAX_AGE:
        return StateValue.stale(value, as_of=timestamp)
    return StateValue.present(value, as_of=timestamp)


def _project_identity(project: InstallationProject) -> ProjectIdentity:
    native = project.project
    return ProjectIdentity(
        id=project.id,
        name=getattr(native, "name", None)
        or getattr(native, "number", None)
        or str(project.id),
        code=getattr(native, "number", None) or getattr(native, "code", None),
    )


def _vendor_identity(row: VendorMaterialRelease | VendorAdvance) -> VendorIdentity:
    return VendorIdentity(
        id=row.vendor_id,
        name=getattr(row.vendor, "name", None) or str(row.vendor_id),
    )


def _material_provider(row: VendorMaterialRelease) -> ProviderObservation:
    if not row.support_status:
        kind = (
            StateValue.not_applicable()
            if row.status
            in {
                VendorMaterialReleaseStatus.requested.value,
                VendorMaterialReleaseStatus.rejected.value,
                VendorMaterialReleaseStatus.canceled.value,
            }
            else StateValue.unknown()
        )
        return ProviderObservation(
            status=kind,
            system=row.support_system,
            reference=row.support_reference,
            detail=(
                "Provider outcome does not apply yet."
                if kind.kind.value == "not_applicable"
                else "No material-issue outcome has been observed."
            ),
        )
    status = _observed_state(row.support_status, row.support_observed_at)
    return ProviderObservation(
        status=status,
        system=row.support_system,
        reference=row.support_reference,
        detail=(
            "The material provider returned an outcome without a timestamp."
            if not status.is_present
            else (
                "Last known material-provider outcome; refresh is delayed."
                if status.is_stale
                else "Observed from the configured material-support provider."
            )
        ),
    )


def _advance_payables(row: VendorAdvance) -> ProviderObservation:
    if not row.payables_status:
        kind = (
            StateValue.not_applicable()
            if row.status
            in {
                VendorAdvanceStatus.requested.value,
                VendorAdvanceStatus.rejected.value,
                VendorAdvanceStatus.canceled.value,
            }
            else StateValue.unknown()
        )
        return ProviderObservation(
            status=kind,
            system=row.payables_system,
            reference=row.payables_reference,
            detail=(
                "Payables observation does not apply yet."
                if kind.kind.value == "not_applicable"
                else "No payables outcome has been observed."
            ),
        )
    status = _observed_state(row.payables_status, row.payables_observed_at)
    return ProviderObservation(
        status=status,
        system=row.payables_system,
        reference=row.payables_reference,
        detail=(
            "The payables provider returned an outcome without a timestamp."
            if not status.is_present
            else (
                "Last known payables outcome; refresh is delayed."
                if status.is_stale
                else "Observed from the configured payables provider."
            )
        ),
    )


def _material_line(row: VendorMaterialReleaseItem) -> MaterialLine:
    return MaterialLine(
        id=row.id,
        item_code=row.item_code,
        description=row.description,
        unit=row.unit,
        quantity=row.quantity,
        issued_quantity=row.issued_quantity,
        notes=row.notes,
    )


def material_release_view(row: VendorMaterialRelease) -> MaterialReleaseView:
    eligibility = vendor_material_release.review_eligibility(row.status)
    return MaterialReleaseView(
        id=row.id,
        project=_project_identity(row.project),
        vendor=_vendor_identity(row),
        status=vendor_material_release_status_presentation(row.status),
        items=tuple(_material_line(item) for item in row.items if item.is_active),
        notes=row.notes,
        requested_at=row.requested_at,
        reviewed_at=row.reviewed_at,
        review_notes=row.review_notes,
        provider=_material_provider(row),
        approve_action=Action(
            key="approve_material_release",
            label="Approve release",
            allowed=eligibility.allowed,
            reason=eligibility.reason,
            permission="inventory:write",
            preview_url=(
                f"/admin/vendors/operations/material-releases/{row.id}/approve/preview"
            ),
            affected=len([item for item in row.items if item.is_active]),
            tone=StatusTone.positive,
            requires_confirmation=True,
        ),
        reject_action=Action(
            key="reject_material_release",
            label="Reject release",
            allowed=eligibility.allowed,
            reason=eligibility.reason,
            permission="inventory:write",
            preview_url=(
                f"/admin/vendors/operations/material-releases/{row.id}/reject/preview"
            ),
            affected=len([item for item in row.items if item.is_active]),
            tone=StatusTone.negative,
            requires_confirmation=True,
        ),
    )


def advance_view(row: VendorAdvance) -> AdvanceView:
    allowed, reason = vendor_advances.review_eligibility(row.status)
    return AdvanceView(
        id=row.id,
        project=_project_identity(row.project),
        vendor=_vendor_identity(row),
        status=vendor_advance_status_presentation(row.status),
        amount=Decimal(row.amount),
        currency=row.currency,
        reason=row.reason,
        requested_at=row.requested_at,
        reviewed_at=row.reviewed_at,
        review_notes=row.review_notes,
        payables=_advance_payables(row),
        approve_action=Action(
            key="approve_vendor_advance",
            label="Approve advance",
            allowed=allowed,
            reason=reason,
            permission="finance:ap:write",
            preview_url=f"/admin/vendors/operations/advances/{row.id}/approve/preview",
            affected=1,
            tone=StatusTone.positive,
            requires_confirmation=True,
        ),
        reject_action=Action(
            key="reject_vendor_advance",
            label="Reject advance",
            allowed=allowed,
            reason=reason,
            permission="finance:ap:write",
            preview_url=f"/admin/vendors/operations/advances/{row.id}/reject/preview",
            affected=1,
            tone=StatusTone.negative,
            requires_confirmation=True,
        ),
    )


def _material_query(db: Session):
    return db.query(VendorMaterialRelease).options(
        joinedload(VendorMaterialRelease.project).joinedload(
            InstallationProject.project
        ),
        joinedload(VendorMaterialRelease.vendor),
        selectinload(VendorMaterialRelease.items),
    )


def _advance_query(db: Session):
    return db.query(VendorAdvance).options(
        joinedload(VendorAdvance.project).joinedload(InstallationProject.project),
        joinedload(VendorAdvance.vendor),
        joinedload(VendorAdvance.quote),
    )


def project_workspace(
    db: Session,
    *,
    project_id: UUID | str,
    vendor_id: UUID | str,
    capabilities: frozenset[str],
) -> VendorSupplyProjectView:
    project_uuid = coerce_uuid(project_id)
    vendor_uuid = coerce_uuid(vendor_id)
    material_rows = (
        _material_query(db)
        .filter(
            VendorMaterialRelease.project_id == project_uuid,
            VendorMaterialRelease.vendor_id == vendor_uuid,
            VendorMaterialRelease.is_active.is_(True),
        )
        .order_by(VendorMaterialRelease.created_at.desc())
        .all()
    )
    advance_rows = (
        _advance_query(db)
        .filter(
            VendorAdvance.project_id == project_uuid,
            VendorAdvance.vendor_id == vendor_uuid,
            VendorAdvance.is_active.is_(True),
        )
        .order_by(VendorAdvance.created_at.desc())
        .all()
    )
    material_eligibility = vendor_material_release.request_eligibility(
        db,
        project_id=project_uuid,
        vendor_id=vendor_uuid,
    )
    can_material = vendor_capabilities.MATERIAL_REQUEST in capabilities
    material_allowed = material_eligibility.allowed and can_material
    material_reason = (
        material_eligibility.reason
        if can_material
        else "Your vendor role cannot request materials."
    )

    advance_eligibility = vendor_advances.request_eligibility(
        db,
        project_id=project_uuid,
        vendor_id=vendor_uuid,
    )
    can_advance = vendor_capabilities.ADVANCE_REQUEST in capabilities
    advance_allowed = advance_eligibility.allowed and can_advance
    advance_reason = (
        advance_eligibility.reason
        if can_advance
        else "Only a vendor owner can request an advance."
    )
    return VendorSupplyProjectView(
        material_releases=tuple(material_release_view(row) for row in material_rows),
        advances=tuple(advance_view(row) for row in advance_rows),
        material_request_action=Action(
            key="request_material_release",
            label="Request materials",
            allowed=material_allowed,
            reason=None if material_allowed else str(material_reason),
        ),
        advance_request_action=Action(
            key="request_vendor_advance",
            label="Request advance",
            allowed=advance_allowed,
            reason=None if advance_allowed else str(advance_reason),
        ),
        advance_quote_total=advance_eligibility.quote_total,
        advance_ceiling=advance_eligibility.ceiling,
        advance_committed=advance_eligibility.committed,
        advance_remaining=advance_eligibility.remaining,
        advance_currency=advance_eligibility.currency,
        advance_max_percent=advance_eligibility.max_percent,
    )


def material_review_queue(
    db: Session, *, limit: int = 100, offset: int = 0
) -> VendorSupplyQueue:
    normalized_limit = max(1, min(limit, 200))
    rows = (
        _material_query(db)
        .filter(
            VendorMaterialRelease.status == VendorMaterialReleaseStatus.requested.value,
            VendorMaterialRelease.is_active.is_(True),
        )
        .order_by(
            VendorMaterialRelease.requested_at.asc(),
            VendorMaterialRelease.id.asc(),
        )
        .offset(max(0, offset))
        .limit(normalized_limit + 1)
        .all()
    )
    return VendorSupplyQueue(
        items=tuple(material_release_view(row) for row in rows[:normalized_limit]),
        count=min(len(rows), normalized_limit),
        limit=normalized_limit,
        offset=max(0, offset),
        has_next=len(rows) > normalized_limit,
    )


def advance_review_queue(
    db: Session, *, limit: int = 100, offset: int = 0
) -> VendorSupplyQueue:
    normalized_limit = max(1, min(limit, 200))
    rows = (
        _advance_query(db)
        .filter(
            VendorAdvance.status == VendorAdvanceStatus.requested.value,
            VendorAdvance.is_active.is_(True),
        )
        .order_by(VendorAdvance.requested_at.asc(), VendorAdvance.id.asc())
        .offset(max(0, offset))
        .limit(normalized_limit + 1)
        .all()
    )
    return VendorSupplyQueue(
        items=tuple(advance_view(row) for row in rows[:normalized_limit]),
        count=min(len(rows), normalized_limit),
        limit=normalized_limit,
        offset=max(0, offset),
        has_next=len(rows) > normalized_limit,
    )


def material_detail(db: Session, release_id: UUID | str) -> MaterialReleaseView:
    row = (
        _material_query(db)
        .filter(
            VendorMaterialRelease.id == coerce_uuid(release_id),
            VendorMaterialRelease.is_active.is_(True),
        )
        .one_or_none()
    )
    if row is None:
        raise _error("material_release_not_found", "Material release not found.")
    return material_release_view(row)


def advance_detail(db: Session, advance_id: UUID | str) -> AdvanceView:
    row = (
        _advance_query(db)
        .filter(
            VendorAdvance.id == coerce_uuid(advance_id),
            VendorAdvance.is_active.is_(True),
        )
        .one_or_none()
    )
    if row is None:
        raise _error("advance_not_found", "Vendor advance not found.")
    return advance_view(row)


def latest_material_releases_for_projects(
    db: Session,
    *,
    project_ids: tuple[UUID, ...],
    vendor_id: UUID,
) -> tuple[MaterialReleaseView, ...]:
    """Return at most one latest active release per project in one bulk read."""

    if not project_ids:
        return ()
    ranked = (
        select(
            VendorMaterialRelease.id.label("row_id"),
            func.row_number()
            .over(
                partition_by=VendorMaterialRelease.project_id,
                order_by=(
                    VendorMaterialRelease.created_at.desc(),
                    VendorMaterialRelease.id.desc(),
                ),
            )
            .label("row_number"),
        )
        .where(
            VendorMaterialRelease.project_id.in_(project_ids),
            VendorMaterialRelease.vendor_id == vendor_id,
            VendorMaterialRelease.is_active.is_(True),
        )
        .subquery()
    )
    rows = (
        _material_query(db)
        .join(ranked, VendorMaterialRelease.id == ranked.c.row_id)
        .filter(ranked.c.row_number == 1)
        .order_by(VendorMaterialRelease.project_id.asc())
        .all()
    )
    return tuple(material_release_view(row) for row in rows)


def latest_advances_for_projects(
    db: Session,
    *,
    project_ids: tuple[UUID, ...],
    vendor_id: UUID,
) -> tuple[AdvanceView, ...]:
    """Return at most one latest active advance per project in one bulk read."""

    if not project_ids:
        return ()
    ranked = (
        select(
            VendorAdvance.id.label("row_id"),
            func.row_number()
            .over(
                partition_by=VendorAdvance.project_id,
                order_by=(VendorAdvance.created_at.desc(), VendorAdvance.id.desc()),
            )
            .label("row_number"),
        )
        .where(
            VendorAdvance.project_id.in_(project_ids),
            VendorAdvance.vendor_id == vendor_id,
            VendorAdvance.is_active.is_(True),
        )
        .subquery()
    )
    rows = (
        _advance_query(db)
        .join(ranked, VendorAdvance.id == ranked.c.row_id)
        .filter(ranked.c.row_number == 1)
        .order_by(VendorAdvance.project_id.asc())
        .all()
    )
    return tuple(advance_view(row) for row in rows)


def material_review_preview(
    db: Session,
    *,
    release_id: UUID | str,
    action: VendorSupplyReviewAction,
    reason: str | None = None,
    for_update: bool = False,
) -> SupplyReviewPreview:
    normalized_reason = _review_reason(
        reason,
        required=action is VendorSupplyReviewAction.reject,
    )
    query = _material_query(db).filter(
        VendorMaterialRelease.id == coerce_uuid(release_id),
        VendorMaterialRelease.is_active.is_(True),
    )
    if for_update:
        query = query.with_for_update(of=VendorMaterialRelease)
    row = query.one_or_none()
    if row is None:
        raise _error("material_release_not_found", "Material release not found.")
    eligibility = vendor_material_release.review_eligibility(row.status)
    if not eligibility.allowed:
        raise _error("material_not_reviewable", str(eligibility.reason))
    active_items = tuple(item for item in row.items if item.is_active)
    result = (
        "Approved for provider issue"
        if action is VendorSupplyReviewAction.approve
        else "Rejected"
    )
    return SupplyReviewPreview(
        supply_type=VendorSupplyType.material,
        record_id=row.id,
        project_id=row.project_id,
        action=action,
        title=f"{action.value.title()} material release",
        summary=(
            "This records Dotmac's release decision. The material provider "
            "still owns stock issue and delivery."
        ),
        details=(
            ("Project", _project_identity(row.project).name),
            ("Vendor", _vendor_identity(row).name),
            ("Requested lines", str(len(active_items))),
            (
                "Requested quantity",
                str(sum(item.quantity for item in active_items)),
            ),
            ("Result", result),
            ("Review note", normalized_reason or "No additional note"),
            ("Provider effect", "Issue remains a separate provider observation"),
        ),
        state=(
            ("record_id", str(row.id)),
            ("project_id", str(row.project_id)),
            ("vendor_id", str(row.vendor_id)),
            ("status", row.status),
            ("updated_at", str(_aware(row.updated_at))),
            (
                "items",
                "|".join(
                    f"{item.id}:{item.description}:{item.quantity}:{item.unit or ''}"
                    for item in sorted(active_items, key=lambda value: str(value.id))
                ),
            ),
            ("action", action.value),
            ("reason", normalized_reason or ""),
        ),
        reason=normalized_reason,
    )


def advance_review_preview(
    db: Session,
    *,
    advance_id: UUID | str,
    action: VendorSupplyReviewAction,
    reason: str | None = None,
    for_update: bool = False,
) -> SupplyReviewPreview:
    normalized_reason = _review_reason(
        reason,
        required=action is VendorSupplyReviewAction.reject,
    )
    query = _advance_query(db).filter(
        VendorAdvance.id == coerce_uuid(advance_id),
        VendorAdvance.is_active.is_(True),
    )
    if for_update:
        query = query.with_for_update(of=VendorAdvance)
    row = query.one_or_none()
    if row is None:
        raise _error("advance_not_found", "Vendor advance not found.")
    allowed, blocked_reason = vendor_advances.review_eligibility(row.status)
    if not allowed:
        raise _error("advance_not_reviewable", str(blocked_reason))
    eligibility = vendor_advances.request_eligibility(
        db,
        project_id=row.project_id,
        vendor_id=row.vendor_id,
    )
    result = (
        "Approved; payment remains pending"
        if action is VendorSupplyReviewAction.approve
        else "Rejected"
    )
    return SupplyReviewPreview(
        supply_type=VendorSupplyType.advance,
        record_id=row.id,
        project_id=row.project_id,
        action=action,
        title=f"{action.value.title()} vendor advance",
        summary=(
            "This records Dotmac's advance decision. The payables provider "
            "still owns payment and settlement."
        ),
        details=(
            ("Project", _project_identity(row.project).name),
            ("Vendor", _vendor_identity(row).name),
            ("Requested amount", f"{row.currency} {Decimal(row.amount):,.2f}"),
            (
                "Approved quote",
                (
                    f"{row.currency} {eligibility.quote_total:,.2f}"
                    if eligibility.quote_total is not None
                    else "Unavailable"
                ),
            ),
            (
                "Advance ceiling",
                (
                    f"{row.currency} {eligibility.ceiling:,.2f}"
                    if eligibility.ceiling is not None
                    else "Unavailable"
                ),
            ),
            ("Already committed", f"{row.currency} {eligibility.committed:,.2f}"),
            ("Result", result),
            ("Review note", normalized_reason or "No additional note"),
            ("Payment effect", "Payment remains a separate provider action"),
        ),
        state=(
            ("record_id", str(row.id)),
            ("project_id", str(row.project_id)),
            ("vendor_id", str(row.vendor_id)),
            ("quote_id", str(row.quote_id)),
            ("status", row.status),
            ("amount", str(Decimal(row.amount))),
            ("currency", row.currency),
            ("updated_at", str(_aware(row.updated_at))),
            ("quote_total", str(eligibility.quote_total or "")),
            ("ceiling", str(eligibility.ceiling or "")),
            ("committed", str(eligibility.committed)),
            ("action", action.value),
            ("reason", normalized_reason or ""),
        ),
        reason=normalized_reason,
    )
