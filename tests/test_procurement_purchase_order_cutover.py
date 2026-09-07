from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

import app.models  # noqa: F401
from app.models.audit import AuditEvent
from app.models.field_erp_sync import (
    PRE_CUTOVER_SYNC_FLOW_OWNER,
    FieldErpSyncEvent,
    FieldErpSyncFlow,
    SyncFlowOwner,
    SyncFlowOwnership,
)
from app.models.project import Project
from app.models.subscriber import Subscriber
from app.models.vendor_routes import (
    InstallationProject,
    ProjectQuote,
    ProjectQuoteLineItem,
    ProjectQuoteStatus,
    Vendor,
)
from app.services.owner_commands import CommandContext
from app.services.procurement_purchase_order_cutover import (
    ACTION,
    ProcurementPurchaseOrderCutoverError,
    PurchaseOrderBackfillTarget,
    PurchaseOrderCutoverCommand,
    SupplierVerificationMethod,
    VerifiedErpSupplierBinding,
    cut_over_purchase_order_origination,
)


def _approved_install(db_session) -> tuple[UUID, UUID, UUID, str]:
    subscriber = Subscriber(
        first_name="Procurement",
        last_name="Test",
        email=f"procurement-{uuid4().hex[:8]}@example.com",
    )
    db_session.add(subscriber)
    db_session.flush()
    project = Project(
        name="Verified installation",
        code=f"PRJ-{uuid4().hex[:8]}",
        subscriber_id=subscriber.id,
    )
    vendor = Vendor(
        name="Verified supplier",
        supplier_system=PRE_CUTOVER_SYNC_FLOW_OWNER,
        supplier_reference=f"legacy-{uuid4()}",
    )
    db_session.add_all((project, vendor))
    db_session.flush()
    installation = InstallationProject(
        project_id=project.id,
        subscriber_id=subscriber.id,
        assigned_vendor_id=vendor.id,
    )
    db_session.add(installation)
    db_session.flush()
    quote = ProjectQuote(
        project_id=installation.id,
        vendor_id=vendor.id,
        status=ProjectQuoteStatus.approved.value,
        currency="NGN",
        subtotal=Decimal("1000.00"),
        tax_total=Decimal("75.00"),
        total=Decimal("1075.00"),
        reviewed_at=datetime.now(UTC),
    )
    db_session.add(quote)
    db_session.flush()
    db_session.add(
        ProjectQuoteLineItem(
            quote_id=quote.id,
            item_type="installation",
            description="Verified installation service",
            quantity=Decimal("1"),
            unit_price=Decimal("1000.00"),
            amount=Decimal("1000.00"),
        )
    )
    installation.approved_quote_id = quote.id
    db_session.add_all(
        (
            SyncFlowOwnership(
                flow=FieldErpSyncFlow.purchase_invoice.value,
                owner=PRE_CUTOVER_SYNC_FLOW_OWNER,
            ),
            SyncFlowOwnership(
                flow=FieldErpSyncFlow.purchase_order.value,
                owner=PRE_CUTOVER_SYNC_FLOW_OWNER,
            ),
        )
    )
    installation_id = installation.id
    quote_id = quote.id
    vendor_id = vendor.id
    supplier_reference = str(vendor.supplier_reference)
    db_session.commit()
    return installation_id, quote_id, vendor_id, supplier_reference


def _command(
    installation_id: UUID,
    quote_id: UUID,
    vendor_id: UUID,
    current_reference: str,
    *,
    command_id=None,
    verified_at: datetime | None = None,
) -> PurchaseOrderCutoverCommand:
    operation_id = command_id or uuid4()
    context = CommandContext.system(
        actor="michael",
        scope="test:purchase_order",
        reason="Verified purchase-order ownership cutover",
        command_id=operation_id,
        correlation_id=operation_id,
        idempotency_key=f"procurement-po-cutover:{operation_id}",
    )
    return PurchaseOrderCutoverCommand(
        context=context,
        targets=(
            PurchaseOrderBackfillTarget(
                installation_project_id=installation_id,
                approved_quote_id=quote_id,
                vendor_id=vendor_id,
            ),
        ),
        supplier_verifications=(
            VerifiedErpSupplierBinding(
                vendor_id=vendor_id,
                current_reference_sha256=hashlib.sha256(
                    current_reference.strip().encode()
                ).hexdigest(),
                erp_supplier_reference="ERP-SUP-001",
                verified_at=verified_at or datetime.now(UTC),
                method=SupplierVerificationMethod.supplier_code,
            ),
        ),
    )


def test_cutover_atomically_binds_vendor_flips_owner_and_stages_outbox(db_session):
    installation_id, quote_id, vendor_id, current_reference = _approved_install(
        db_session
    )
    command = _command(installation_id, quote_id, vendor_id, current_reference)

    outcome = cut_over_purchase_order_origination(db_session, command=command)

    assert outcome.owner is SyncFlowOwner.sub
    assert outcome.owned_flows == (
        FieldErpSyncFlow.purchase_invoice,
        FieldErpSyncFlow.purchase_order,
    )
    assert outcome.target_count == 1
    assert outcome.vendor_binding_count == 1
    assert outcome.replayed is False
    assert not db_session.in_transaction()

    refreshed_vendor = db_session.get(Vendor, vendor_id)
    ownership_rows = db_session.query(SyncFlowOwnership).all()
    event = db_session.query(FieldErpSyncEvent).one()
    audit = db_session.query(AuditEvent).filter(AuditEvent.action == ACTION).one()
    assert refreshed_vendor.supplier_system == "dotmac_erp"
    assert refreshed_vendor.supplier_reference == "ERP-SUP-001"
    assert refreshed_vendor.code == "ERP-SUP-001"
    assert {row.owner for row in ownership_rows} == {SyncFlowOwner.sub.value}
    assert {row.updated_by for row in ownership_rows} == {
        f"procurement-cutover:{command.context.command_id}"
    }
    assert event.id == outcome.outbox_event_ids[0]
    assert event.idempotency_key == f"po-ip-{installation_id}"
    assert event.payload["source_quote_id"] == str(quote_id)
    assert event.payload["vendor_erp_id"] == "ERP-SUP-001"
    assert audit.request_id == str(command.context.command_id)
    assert audit.details["new_owners"] == {
        FieldErpSyncFlow.purchase_invoice.value: SyncFlowOwner.sub.value,
        FieldErpSyncFlow.purchase_order.value: SyncFlowOwner.sub.value,
    }


def test_cutover_replays_same_command_without_duplicate_rows(db_session):
    installation_id, quote_id, vendor_id, current_reference = _approved_install(
        db_session
    )
    command = _command(installation_id, quote_id, vendor_id, current_reference)
    first = cut_over_purchase_order_origination(db_session, command=command)

    replay = cut_over_purchase_order_origination(db_session, command=command)

    assert replay.replayed is True
    assert replay.outbox_event_ids == first.outbox_event_ids
    assert db_session.query(FieldErpSyncEvent).count() == 1
    assert db_session.query(AuditEvent).filter(AuditEvent.action == ACTION).count() == 1


def test_cutover_rolls_back_everything_when_supplier_verification_is_stale(db_session):
    installation_id, quote_id, vendor_id, original_reference = _approved_install(
        db_session
    )
    command = _command(
        installation_id,
        quote_id,
        vendor_id,
        original_reference,
        verified_at=datetime.now(UTC) - timedelta(days=2),
    )

    with pytest.raises(ProcurementPurchaseOrderCutoverError) as exc_info:
        cut_over_purchase_order_origination(db_session, command=command)

    assert exc_info.value.code.endswith(".stale_supplier_verification")
    assert not db_session.in_transaction()
    refreshed_vendor = db_session.get(Vendor, vendor_id)
    ownership_rows = db_session.query(SyncFlowOwnership).all()
    assert refreshed_vendor.supplier_system == PRE_CUTOVER_SYNC_FLOW_OWNER
    assert refreshed_vendor.supplier_reference == original_reference
    assert {row.owner for row in ownership_rows} == {PRE_CUTOVER_SYNC_FLOW_OWNER}
    assert db_session.query(FieldErpSyncEvent).count() == 0
    assert db_session.query(AuditEvent).filter(AuditEvent.action == ACTION).count() == 0


def test_cutover_rolls_back_when_approved_quote_anchor_changed(db_session):
    installation_id, quote_id, vendor_id, current_reference = _approved_install(
        db_session
    )
    command = _command(installation_id, quote_id, vendor_id, current_reference)
    changed_command = PurchaseOrderCutoverCommand(
        context=command.context,
        targets=(
            PurchaseOrderBackfillTarget(
                installation_project_id=installation_id,
                approved_quote_id=uuid4(),
                vendor_id=vendor_id,
            ),
        ),
        supplier_verifications=command.supplier_verifications,
    )

    with pytest.raises(ProcurementPurchaseOrderCutoverError) as exc_info:
        cut_over_purchase_order_origination(db_session, command=changed_command)

    assert exc_info.value.code.endswith(".target_changed")
    assert {row.owner for row in db_session.query(SyncFlowOwnership).all()} == {
        PRE_CUTOVER_SYNC_FLOW_OWNER
    }
    assert db_session.query(FieldErpSyncEvent).count() == 0
