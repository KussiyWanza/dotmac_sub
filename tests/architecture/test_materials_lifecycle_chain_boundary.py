"""Boundary guards for the Materials/Vendor/ERP owner chain.

The chain contract (docs/designs/MATERIALS_VENDOR_ERP_CHAIN.md): Sub owns
operational need, approval, allocation, and consumption evidence; ERP owns
inventory and accounting outcomes; committed approvals reach the ERP
transports only through receipted consumers, and ERP failures stay durable
pending outbox deliveries — Sub never infers issuance or payment.
"""

from __future__ import annotations

from pathlib import Path

from app.services.sot_registry.registry import service_relationship

ROOT = Path(__file__).resolve().parents[2]


def _source(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_material_approval_emits_output_instead_of_swallowed_enqueue():
    src = _source("app/services/field/material_requests.py")
    assert "EventType.field_material_request_approved" in src
    # The best-effort swallow is retired; a failed ERP intent is a failed
    # retryable delivery, not a metadata breadcrumb.
    assert "_maybe_enqueue_backoffice_support" not in src
    assert "backoffice_delivery_pending" not in src
    assert "def consume_material_request_approved" in src
    assert "consume_owner_output" in src


def test_erp_write_back_emits_allocation_evidence():
    src = _source("app/services/field/material_requests.py")
    assert "EventType.field_material_request_fulfilled" in src


def test_consumption_owner_is_registered_and_emits_evidence():
    src = _source("app/services/field/materials.py")
    assert "EventType.field_material_consumption_recorded" in src
    assert service_relationship("operations.material_consumption").module == (
        "app.services.field.materials"
    )
    baseline = _source("tests/architecture/sot_writer_baseline.txt")
    assert "field/materials.py" not in baseline


def test_invoice_approval_reaches_payables_only_through_receipt():
    records = _source("app/services/vendor_purchase_invoice_records.py")
    assert "EventType.vendor_purchase_invoice_approved" in records
    assert "enqueue_purchase_invoice" not in records
    coordinator = _source("app/services/vendor_purchase_invoices.py")
    assert "def consume_invoice_approved" in coordinator
    assert "consume_owner_output" in coordinator


def test_payables_settlement_is_observed_never_decided():
    src = _source("app/services/dotmac_erp/purchase_invoice_sync.py")
    assert "EventType.vendor_purchase_invoice_payment_observed" in src
    # Observation projects ERP-owned facts; Sub writes no payment decision.
    assert "payment_status = observation" in src


def test_projection_handler_routes_the_chain():
    src = _source("app/services/events/handlers/materials_lifecycle_projection.py")
    assert "EventType.field_material_request_approved" in src
    assert "EventType.vendor_purchase_invoice_approved" in src
    assert "_owner_session(" in src
    assert "except Exception" not in src
    dispatcher = _source("app/services/events/dispatcher.py")
    assert "MaterialsLifecycleProjectionHandler()" in dispatcher
    controls = _source("app/services/control_relationships.py")
    assert '"MaterialsLifecycleProjectionHandler"' in controls
