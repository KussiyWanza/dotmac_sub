"""Atomic purchase-order source cutover and reconciled historical staging.

This is the only public writer allowed to move the purchase-order flow from its
pre-cutover owner to Selfcare while staging explicitly reconciled historical
quote approvals. It never calls ERP in-transaction. An operator first verifies
each supplier in ERP and supplies the exact provider reference plus a
fingerprint of the currently stored source reference. Delivery remains the
durable outbox's job.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models.audit import AuditEvent
from app.models.field_erp_sync import (
    PRE_CUTOVER_SYNC_FLOW_OWNER,
    FieldErpSyncEvent,
    FieldErpSyncFlow,
    SyncFlowOwner,
    SyncFlowOwnership,
)
from app.models.vendor_routes import InstallationProject, ProjectQuote, Vendor
from app.services.audit_adapter import AuditActor, AuditRecord, audit_adapter
from app.services.domain_errors import DomainError
from app.services.dotmac_erp import outbox, purchase_order_sync
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

OWNER = "integration.procurement_purchase_order_cutover"
CONCERN = "Selfcare procurement ERP ownership cutover and reconciled PO backfill"
ACTION = "procurement.purchase_order_cutover"
ENTITY_TYPE = "procurement_purchase_order_cutover"
MAX_TARGETS = 100
MAX_VERIFICATION_AGE = timedelta(hours=24)
CUTOVER_FLOWS = (
    FieldErpSyncFlow.purchase_invoice,
    FieldErpSyncFlow.purchase_order,
)

_CUTOVER_COMMAND = OwnerCommandDefinition(
    owner=OWNER,
    concern=CONCERN,
    name="cut_over_purchase_order_origination",
)


class SupplierVerificationMethod(StrEnum):
    erp_id = "erp_id"
    supplier_code = "supplier_code"
    display_name = "display_name"


@dataclass(frozen=True, slots=True)
class PurchaseOrderBackfillTarget:
    installation_project_id: UUID
    approved_quote_id: UUID
    vendor_id: UUID


@dataclass(frozen=True, slots=True)
class VerifiedErpSupplierBinding:
    vendor_id: UUID
    current_reference_sha256: str
    erp_supplier_reference: str
    verified_at: datetime
    method: SupplierVerificationMethod


@dataclass(frozen=True, slots=True)
class PurchaseOrderCutoverCommand:
    context: CommandContext
    targets: tuple[PurchaseOrderBackfillTarget, ...]
    supplier_verifications: tuple[VerifiedErpSupplierBinding, ...]


@dataclass(frozen=True, slots=True)
class PurchaseOrderCutoverOutcome:
    command_id: UUID
    target_count: int
    vendor_binding_count: int
    outbox_event_ids: tuple[UUID, ...]
    owner: SyncFlowOwner
    owned_flows: tuple[FieldErpSyncFlow, ...]
    replayed: bool


class ProcurementPurchaseOrderCutoverError(DomainError):
    """Fail-closed cutover validation error safe for operator display."""


def _error(suffix: str, message: str, **details: object) -> DomainError:
    return ProcurementPurchaseOrderCutoverError(
        code=f"{OWNER}.{suffix}", message=message, details=details
    )


def _sha256(value: str) -> str:
    return hashlib.sha256(value.strip().encode()).hexdigest()


def _target_digest(command: PurchaseOrderCutoverCommand) -> str:
    payload = {
        "targets": [
            {
                "installation_project_id": str(item.installation_project_id),
                "approved_quote_id": str(item.approved_quote_id),
                "vendor_id": str(item.vendor_id),
            }
            for item in sorted(
                command.targets, key=lambda item: str(item.installation_project_id)
            )
        ],
        "verifications": [
            {
                "vendor_id": str(item.vendor_id),
                "current_reference_sha256": item.current_reference_sha256,
                "erp_supplier_reference_sha256": _sha256(item.erp_supplier_reference),
                "verified_at": item.verified_at.isoformat(),
                "method": item.method.value,
            }
            for item in sorted(
                command.supplier_verifications, key=lambda item: str(item.vendor_id)
            )
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _validate_command_shape(command: PurchaseOrderCutoverCommand) -> None:
    if not command.targets or len(command.targets) > MAX_TARGETS:
        raise _error(
            "invalid_batch",
            f"Purchase-order cutover requires between 1 and {MAX_TARGETS} targets.",
            target_count=len(command.targets),
        )
    target_ids = [item.installation_project_id for item in command.targets]
    if len(set(target_ids)) != len(target_ids):
        raise _error(
            "duplicate_target", "A cutover target was supplied more than once."
        )
    verification_ids = [item.vendor_id for item in command.supplier_verifications]
    if len(set(verification_ids)) != len(verification_ids):
        raise _error(
            "duplicate_supplier_verification",
            "A supplier verification was supplied more than once.",
        )
    expected_key = f"procurement-po-cutover:{command.context.command_id}"
    if command.context.idempotency_key != expected_key:
        raise _error(
            "invalid_idempotency_key",
            "The cutover idempotency key does not match its command identifier.",
        )


def _replayed_outcome(
    db: Session,
    *,
    command: PurchaseOrderCutoverCommand,
    audit: AuditEvent,
    digest: str,
) -> PurchaseOrderCutoverOutcome:
    details = audit.details or {}
    if details.get("target_digest") != digest:
        raise _error(
            "idempotency_conflict",
            "The command identifier was already used with different cutover evidence.",
        )
    target_ids = tuple(item.installation_project_id for item in command.targets)
    events = tuple(
        db.scalars(
            select(FieldErpSyncEvent)
            .where(
                FieldErpSyncEvent.flow == FieldErpSyncFlow.purchase_order.value,
                FieldErpSyncEvent.entity_id.in_(target_ids),
            )
            .order_by(FieldErpSyncEvent.entity_id)
        )
    )
    if len(events) != len(command.targets):
        raise _error(
            "replay_drift",
            "Recorded cutover evidence no longer has every expected outbox row.",
            expected=len(command.targets),
            actual=len(events),
        )
    return PurchaseOrderCutoverOutcome(
        command_id=command.context.command_id,
        target_count=len(command.targets),
        vendor_binding_count=len(command.supplier_verifications),
        outbox_event_ids=tuple(item.id for item in events),
        owner=SyncFlowOwner.sub,
        owned_flows=CUTOVER_FLOWS,
        replayed=True,
    )


def _execute_cutover(
    db: Session,
    *,
    command: PurchaseOrderCutoverCommand,
    now: datetime,
) -> PurchaseOrderCutoverOutcome:
    ownership_rows = tuple(
        db.scalars(
            select(SyncFlowOwnership)
            .where(
                SyncFlowOwnership.flow.in_(tuple(flow.value for flow in CUTOVER_FLOWS))
            )
            .order_by(SyncFlowOwnership.flow)
            .with_for_update()
        )
    )
    ownership_by_flow = {row.flow: row for row in ownership_rows}
    missing_flows = [
        flow.value for flow in CUTOVER_FLOWS if flow.value not in ownership_by_flow
    ]
    if missing_flows:
        raise _error(
            "missing_flow_ownership",
            "A procurement flow has no explicit ownership row.",
            missing_flows=missing_flows,
        )

    digest = _target_digest(command)
    existing_audit = db.scalar(
        select(AuditEvent).where(
            AuditEvent.action == ACTION,
            AuditEvent.request_id == str(command.context.command_id),
        )
    )
    if existing_audit is not None:
        return _replayed_outcome(
            db, command=command, audit=existing_audit, digest=digest
        )
    invalid_owners = {
        flow.value: ownership_by_flow[flow.value].owner
        for flow in CUTOVER_FLOWS
        if ownership_by_flow[flow.value].owner != PRE_CUTOVER_SYNC_FLOW_OWNER
    }
    if invalid_owners:
        raise _error(
            "invalid_flow_owner",
            "A new cutover requires the pre-cutover procurement flow owners.",
            owners=invalid_owners,
        )

    target_by_id = {item.installation_project_id: item for item in command.targets}
    ordered_ids = tuple(sorted(target_by_id, key=str))
    locked_projects = tuple(
        db.scalars(
            select(InstallationProject)
            .where(InstallationProject.id.in_(ordered_ids))
            .order_by(InstallationProject.id)
            .with_for_update()
        )
    )
    if len(locked_projects) != len(ordered_ids):
        found = {item.id for item in locked_projects}
        raise _error(
            "target_not_found",
            "One or more installation projects do not exist.",
            missing_ids=[str(item) for item in ordered_ids if item not in found],
        )
    projects = tuple(
        db.scalars(
            select(InstallationProject)
            .options(
                selectinload(InstallationProject.project),
                selectinload(InstallationProject.approved_quote).selectinload(
                    ProjectQuote.vendor
                ),
                selectinload(InstallationProject.approved_quote).selectinload(
                    ProjectQuote.line_items
                ),
            )
            .where(InstallationProject.id.in_(ordered_ids))
            .order_by(InstallationProject.id)
        )
    )

    verification_by_vendor = {
        item.vendor_id: item for item in command.supplier_verifications
    }
    expected_vendor_ids = {item.vendor_id for item in command.targets}
    if set(verification_by_vendor) != expected_vendor_ids:
        raise _error(
            "supplier_verification_scope_mismatch",
            "Supplier verification must cover exactly the vendors in the cutover batch.",
        )
    vendors = tuple(
        db.scalars(
            select(Vendor)
            .where(Vendor.id.in_(tuple(sorted(expected_vendor_ids, key=str))))
            .order_by(Vendor.id)
            .with_for_update()
        )
    )
    if len(vendors) != len(expected_vendor_ids):
        raise _error(
            "vendor_not_found",
            "One or more verified vendors do not exist.",
        )

    for vendor in vendors:
        verification = verification_by_vendor[vendor.id]
        reference = (vendor.supplier_reference or "").strip()
        if not reference or _sha256(reference) != verification.current_reference_sha256:
            raise _error(
                "supplier_verification_mismatch",
                "A vendor supplier reference changed after ERP verification.",
                vendor_id=str(vendor.id),
            )
        if verification.verified_at.tzinfo is None:
            raise _error(
                "invalid_supplier_verification_time",
                "ERP supplier verification timestamps must include a timezone.",
                vendor_id=str(vendor.id),
            )
        age = now - verification.verified_at.astimezone(UTC)
        if age < -timedelta(minutes=5) or age > MAX_VERIFICATION_AGE:
            raise _error(
                "stale_supplier_verification",
                "ERP supplier verification is stale or from the future.",
                vendor_id=str(vendor.id),
            )
        erp_reference = verification.erp_supplier_reference.strip()
        if not erp_reference or len(erp_reference) > 100:
            raise _error(
                "invalid_erp_supplier_reference",
                "Verified ERP supplier reference must contain 1 to 100 characters.",
                vendor_id=str(vendor.id),
            )
        conflicting_vendor = db.scalar(
            select(Vendor).where(
                Vendor.id != vendor.id,
                Vendor.supplier_system == purchase_order_sync.PROVIDER,
                Vendor.supplier_reference == erp_reference,
            )
        )
        if conflicting_vendor is not None:
            raise _error(
                "erp_supplier_reference_conflict",
                "The verified ERP supplier reference is already assigned to another vendor.",
                vendor_id=str(vendor.id),
            )
        if verification.method is not SupplierVerificationMethod.erp_id:
            conflicting_code = db.scalar(
                select(Vendor).where(
                    Vendor.id != vendor.id,
                    Vendor.code == erp_reference,
                )
            )
            if conflicting_code is not None:
                raise _error(
                    "erp_supplier_code_conflict",
                    "The verified ERP supplier code is already assigned to another vendor.",
                    vendor_id=str(vendor.id),
                )

    for project in projects:
        target = target_by_id[project.id]
        quote = project.approved_quote
        if (
            quote is None
            or project.approved_quote_id != target.approved_quote_id
            or quote.vendor_id != target.vendor_id
        ):
            raise _error(
                "target_changed",
                "An installation project's approved quote or vendor changed after review.",
                installation_project_id=str(project.id),
            )
        if project.procurement_order_reference:
            raise _error(
                "existing_procurement_reference",
                "An installation project already has a procurement order reference.",
                installation_project_id=str(project.id),
            )

    for vendor in vendors:
        verification = verification_by_vendor[vendor.id]
        erp_reference = verification.erp_supplier_reference.strip()
        vendor.supplier_system = purchase_order_sync.PROVIDER
        vendor.supplier_reference = erp_reference
        if verification.method is not SupplierVerificationMethod.erp_id:
            vendor.code = erp_reference
    db.flush()

    for project in projects:
        reason = purchase_order_sync.purchase_order_eligibility_error(project)
        if reason:
            raise _error(
                "ineligible_target",
                "A reviewed installation project is not eligible for ERP purchase order staging.",
                installation_project_id=str(project.id),
                reason=reason,
            )

    for ownership in ownership_rows:
        ownership.owner = SyncFlowOwner.sub.value
        ownership.updated_at = now
        ownership.updated_by = f"procurement-cutover:{command.context.command_id}"
    db.flush()

    events: list[FieldErpSyncEvent] = []
    for project in projects:
        payload = purchase_order_sync.build_purchase_order_payload(project)
        key = purchase_order_sync.purchase_order_idempotency_key(project)
        existing_event = db.scalar(
            select(FieldErpSyncEvent).where(FieldErpSyncEvent.idempotency_key == key)
        )
        if existing_event is not None and (
            existing_event.flow != FieldErpSyncFlow.purchase_order.value
            or existing_event.entity_id != project.id
            or existing_event.payload != payload
        ):
            raise _error(
                "existing_outbox_payload_mismatch",
                "An existing outbox key contains different purchase-order evidence.",
                installation_project_id=str(project.id),
            )
        event = outbox.enqueue(
            db,
            flow=FieldErpSyncFlow.purchase_order,
            entity_type=purchase_order_sync.ENTITY_TYPE,
            entity_id=project.id,
            idempotency_key=key,
            payload=payload,
            isolate=False,
        )
        events.append(event)

    audit_adapter.stage(
        db,
        AuditRecord(
            action=ACTION,
            entity_type=ENTITY_TYPE,
            entity_id=str(command.context.command_id),
            actor=AuditActor.user(command.context.actor, label=command.context.actor),
            request_id=str(command.context.command_id),
            metadata={
                "correlation_id": str(command.context.correlation_id),
                "reason": command.context.reason,
                "scope": command.context.scope,
            },
            details={
                "target_digest": digest,
                "installation_project_ids": [str(item.id) for item in projects],
                "approved_quote_ids": [
                    str(target_by_id[item.id].approved_quote_id) for item in projects
                ],
                "vendor_ids": [str(item.id) for item in vendors],
                "outbox_event_ids": [str(item.id) for item in events],
                "supplier_verification_methods": {
                    str(item.vendor_id): item.method.value
                    for item in command.supplier_verifications
                },
                "previous_owners": {
                    flow.value: PRE_CUTOVER_SYNC_FLOW_OWNER for flow in CUTOVER_FLOWS
                },
                "new_owners": {
                    flow.value: SyncFlowOwner.sub.value for flow in CUTOVER_FLOWS
                },
            },
        ),
    )
    db.flush()
    return PurchaseOrderCutoverOutcome(
        command_id=command.context.command_id,
        target_count=len(projects),
        vendor_binding_count=len(vendors),
        outbox_event_ids=tuple(item.id for item in events),
        owner=SyncFlowOwner.sub,
        owned_flows=CUTOVER_FLOWS,
        replayed=False,
    )


def cut_over_purchase_order_origination(
    db: Session,
    *,
    command: PurchaseOrderCutoverCommand,
    now: datetime | None = None,
) -> PurchaseOrderCutoverOutcome:
    """Atomically assign PO ownership and stage an exact reconciled batch."""

    _validate_command_shape(command)
    effective_now = (now or datetime.now(UTC)).astimezone(UTC)
    return execute_owner_command(
        db,
        definition=_CUTOVER_COMMAND,
        context=command.context,
        operation=lambda: _execute_cutover(db, command=command, now=effective_now),
    )


__all__ = [
    "CUTOVER_FLOWS",
    "MAX_TARGETS",
    "ProcurementPurchaseOrderCutoverError",
    "PurchaseOrderBackfillTarget",
    "PurchaseOrderCutoverCommand",
    "PurchaseOrderCutoverOutcome",
    "SupplierVerificationMethod",
    "VerifiedErpSupplierBinding",
    "cut_over_purchase_order_origination",
]
