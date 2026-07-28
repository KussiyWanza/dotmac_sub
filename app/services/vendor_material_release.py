"""Canonical owner for material Dotmac releases to a vendor for a project.

A contractor drawing our cable for a buildout had no way to ask for it.
``FieldMaterialRequest`` is work-order scoped with a ``TechnicianProfile``
requester, so it models an employee on a customer job — not a vendor on a
project. This owner models the other case.

Boundary (``docs/BACKOFFICE_INTEGRATION_BOUNDARY.md``): Sub decides *whether*
material is released and records the evidence; the configured provider owns the
stock issue. Sub never posts stock, never selects a warehouse, and never treats
a provider reference as authority. A provider failure is recorded for retry and
never reverses an approval Sub already committed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.vendor_routes import InstallationProject, InstallationProjectStatus
from app.models.vendor_supply import (
    VendorMaterialRelease,
    VendorMaterialReleaseItem,
    VendorMaterialReleaseStatus,
)
from app.services.common import coerce_uuid
from app.services.events import emit_event
from app.services.events.types import EventType

# Material is released to execute work, so the project must be past award and
# not yet finished with. Releasing against a draft or verified project would be
# releasing stock for work nobody has agreed to or that is already done.
_RELEASABLE_PROJECT_STATUSES = frozenset(
    {
        InstallationProjectStatus.approved.value,
        InstallationProjectStatus.in_progress.value,
    }
)
_EDITABLE = frozenset(
    {
        VendorMaterialReleaseStatus.draft.value,
        VendorMaterialReleaseStatus.requested.value,
    }
)


class VendorMaterialReleaseError(ValueError):
    def __init__(self, code: str, message: str, *, kind: str = "invalid") -> None:
        super().__init__(message)
        self.code = code
        self.kind = kind


def _error(code: str, message: str, *, kind: str = "invalid"):
    return VendorMaterialReleaseError(code, message, kind=kind)


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class RequestMaterialRelease:
    project_id: UUID
    vendor_id: UUID
    requested_by_person_id: UUID | None
    items: tuple[dict, ...]
    notes: str | None = None


@dataclass(frozen=True, slots=True)
class MaterialReleaseEligibility:
    """Owner-supplied decision used by commands and read projections."""

    allowed: bool
    reason: str | None


def request_eligibility(
    db: Session,
    *,
    project_id: UUID | str,
    vendor_id: UUID | str,
) -> MaterialReleaseEligibility:
    project = db.get(InstallationProject, coerce_uuid(project_id))
    if project is None or not project.is_active:
        return MaterialReleaseEligibility(
            allowed=False,
            reason="Installation project not found.",
        )
    if str(project.assigned_vendor_id or "") != str(vendor_id):
        return MaterialReleaseEligibility(
            allowed=False,
            reason="Material can only be requested by the assigned vendor.",
        )
    if project.status not in _RELEASABLE_PROJECT_STATUSES:
        return MaterialReleaseEligibility(
            allowed=False,
            reason="Materials are available for approved or in-progress work only.",
        )
    return MaterialReleaseEligibility(allowed=True, reason=None)


def review_eligibility(status: str) -> MaterialReleaseEligibility:
    if status != VendorMaterialReleaseStatus.requested.value:
        return MaterialReleaseEligibility(
            allowed=False,
            reason="Only a requested material release can be reviewed.",
        )
    return MaterialReleaseEligibility(allowed=True, reason=None)


def _release(
    db: Session, release_id: UUID | str, *, for_update: bool = False
) -> VendorMaterialRelease:
    query = db.query(VendorMaterialRelease).filter(
        VendorMaterialRelease.id == coerce_uuid(release_id)
    )
    if for_update:
        query = query.with_for_update(of=VendorMaterialRelease)
    row = query.one_or_none()
    if row is None or not row.is_active:
        raise _error(
            "release_not_found", "Material release not found.", kind="not_found"
        )
    return row


def request_release(
    db: Session, command: RequestMaterialRelease
) -> VendorMaterialRelease:
    """Stage one vendor material request. Caller owns commit."""

    project = (
        db.query(InstallationProject)
        .filter(InstallationProject.id == coerce_uuid(command.project_id))
        .with_for_update(of=InstallationProject)
        .one_or_none()
    )
    if project is None or not project.is_active:
        raise _error(
            "project_not_found", "Installation project not found.", kind="not_found"
        )
    eligibility = request_eligibility(
        db,
        project_id=project.id,
        vendor_id=command.vendor_id,
    )
    if not eligibility.allowed:
        code = (
            "project_not_assigned"
            if str(project.assigned_vendor_id or "") != str(command.vendor_id)
            else "project_not_releasable"
        )
        raise _error(code, str(eligibility.reason))

    lines = [
        item for item in command.items if str(item.get("description") or "").strip()
    ]
    if not lines:
        raise _error("items_required", "At least one material line is required.")
    for item in lines:
        try:
            quantity = int(item.get("quantity") or 0)
        except (TypeError, ValueError) as exc:
            raise _error(
                "invalid_quantity", "Quantity must be a whole number."
            ) from exc
        if quantity <= 0:
            raise _error("invalid_quantity", "Quantity must be greater than zero.")

    release = VendorMaterialRelease(
        project_id=project.id,
        vendor_id=coerce_uuid(command.vendor_id),
        status=VendorMaterialReleaseStatus.requested.value,
        requested_by_person_id=(
            coerce_uuid(command.requested_by_person_id)
            if command.requested_by_person_id
            else None
        ),
        requested_at=_now(),
        notes=(command.notes or "").strip() or None,
    )
    db.add(release)
    db.flush()
    for item in lines:
        db.add(
            VendorMaterialReleaseItem(
                release_id=release.id,
                item_code=(str(item.get("item_code") or "").strip() or None),
                description=str(item["description"]).strip()[:255],
                unit=(str(item.get("unit") or "").strip() or None),
                quantity=int(item["quantity"]),
                notes=(str(item.get("notes") or "").strip() or None),
            )
        )
    db.flush()
    emit_event(
        db,
        EventType.vendor_material_release_requested,
        {
            "schema_version": 1,
            "release_id": str(release.id),
            "project_id": str(release.project_id),
            "vendor_id": str(release.vendor_id),
            "line_count": len(lines),
        },
        actor=str(command.requested_by_person_id or "vendor"),
    )
    return release


def approve(
    db: Session,
    release_id: UUID | str,
    *,
    actor_id: UUID | str,
    notes: str | None = None,
) -> VendorMaterialRelease:
    """Staff approve the release. This is the Sub decision the provider acts on."""
    release = _release(db, release_id, for_update=True)
    eligibility = review_eligibility(release.status)
    if not eligibility.allowed:
        raise _error("not_reviewable", str(eligibility.reason))
    release.status = VendorMaterialReleaseStatus.approved.value
    release.reviewed_by_person_id = coerce_uuid(actor_id)
    release.reviewed_at = _now()
    release.review_notes = (notes or "").strip() or None
    db.flush()
    _emit_review(db, release, actor_id)
    return release


def reject(
    db: Session,
    release_id: UUID | str,
    *,
    actor_id: UUID | str,
    reason: str,
) -> VendorMaterialRelease:
    release = _release(db, release_id, for_update=True)
    eligibility = review_eligibility(release.status)
    if not eligibility.allowed:
        raise _error("not_reviewable", str(eligibility.reason))
    normalized = (reason or "").strip()
    if not normalized:
        raise _error("reason_required", "A rejection reason is required.")
    release.status = VendorMaterialReleaseStatus.rejected.value
    release.reviewed_by_person_id = coerce_uuid(actor_id)
    release.reviewed_at = _now()
    release.review_notes = normalized
    db.flush()
    _emit_review(db, release, actor_id)
    return release


def _emit_review(db: Session, release, actor_id) -> None:
    emit_event(
        db,
        EventType.vendor_material_release_reviewed,
        {
            "schema_version": 1,
            "release_id": str(release.id),
            "project_id": str(release.project_id),
            "vendor_id": str(release.vendor_id),
            "status": release.status,
        },
        actor=str(actor_id),
    )


def apply_provider_outcome(
    db: Session,
    release_id: UUID | str,
    *,
    support_system: str,
    support_reference: str | None,
    support_status: str | None,
    issued_quantities: dict[str, int] | None = None,
) -> VendorMaterialRelease:
    """Project one provider issue/refusal outcome into the Sub workflow.

    Idempotent by construction: re-applying the same outcome writes the same
    values. A provider refusal is recorded as an observation and never reverses
    the Sub approval — staff decide what to do about it.
    """
    release = _release(db, release_id, for_update=True)
    if release.status not in {
        VendorMaterialReleaseStatus.approved.value,
        VendorMaterialReleaseStatus.issued.value,
    }:
        raise _error(
            "outcome_not_applicable",
            "Only an approved release can carry a provider outcome.",
        )
    release.support_system = support_system
    release.support_reference = support_reference
    release.support_status = support_status
    release.support_observed_at = _now()
    if (support_status or "").strip().lower() in {"issued", "completed", "delivered"}:
        release.status = VendorMaterialReleaseStatus.issued.value
        if issued_quantities:
            for item in release.items:
                issued = issued_quantities.get(str(item.id))
                if issued is not None:
                    item.issued_quantity = int(issued)
        emit_event(
            db,
            EventType.vendor_material_release_issued,
            {
                "schema_version": 1,
                "release_id": str(release.id),
                "project_id": str(release.project_id),
                "vendor_id": str(release.vendor_id),
                "support_system": support_system,
                "support_reference": support_reference,
            },
            actor=f"provider:{support_system}",
        )
    db.flush()
    return release


def list_for_project(
    db: Session, project_id: UUID | str
) -> list[VendorMaterialRelease]:
    return list(
        db.scalars(
            select(VendorMaterialRelease)
            .where(VendorMaterialRelease.project_id == coerce_uuid(project_id))
            .where(VendorMaterialRelease.is_active.is_(True))
            .order_by(VendorMaterialRelease.created_at.desc())
        )
    )


# --- committed wrappers -----------------------------------------------------
# The staging functions above are transaction-neutral so they can participate
# in a caller's transaction. These own the commit, so adapters never do —
# owning a transaction in a route is what the adapter boundary forbids.


def request_release_committed(db: Session, command: RequestMaterialRelease):
    row = request_release(db, command)
    db.commit()
    db.refresh(row)
    return row


def approve_committed(db: Session, row_id, *, actor_id, notes: str | None = None):
    row = approve(db, row_id, actor_id=actor_id, notes=notes)
    db.commit()
    db.refresh(row)
    return row


def reject_committed(db: Session, row_id, *, actor_id, reason: str):
    row = reject(db, row_id, actor_id=actor_id, reason=reason)
    db.commit()
    db.refresh(row)
    return row
