"""Purchase-invoice ERP dead-end fixes: the ``sent``-drain poll and the repair
sweep that no longer erases its own diagnostic evidence.

Mirrors the mocked-ERP pattern used by the other ``dotmac_erp`` flow tests:
the outbox uses a fake client and delivery/repair are proven end-to-end
without a live ERP.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import app.models  # noqa: F401 — registers every model on Base.metadata
from app.models.field_erp_sync import (
    FieldErpSyncEvent,
    FieldErpSyncFlow,
    FieldErpSyncStatus,
    SyncFlowOwner,
    SyncFlowOwnership,
)
from app.models.project import Project
from app.models.vendor_routes import (
    InstallationProject,
    Vendor,
    VendorPurchaseInvoice,
    VendorPurchaseInvoiceLineItem,
    VendorPurchaseInvoiceStatus,
)
from app.services.dotmac_erp import outbox, purchase_invoice_sync

_NON_SUB_OWNER = next(
    owner.value for owner in SyncFlowOwner if owner is not SyncFlowOwner.sub
)


def _seed_ownership(db, *, sub_flows: set[str] | None = None) -> None:
    sub_flows = sub_flows or set()
    for flow in FieldErpSyncFlow:
        owner = SyncFlowOwner.sub.value if flow.value in sub_flows else _NON_SUB_OWNER
        db.add(SyncFlowOwnership(flow=flow.value, owner=owner))
    db.flush()


def _approved_invoice(db, *, po_reference="PO-2026-EXISTING") -> VendorPurchaseInvoice:
    project = Project(name="ERP dead-end regression")
    vendor = Vendor(
        name="Repair Vendor",
        code=f"RV-{uuid4().hex[:6]}",
        supplier_reference="SUP-REPAIR-1",
    )
    db.add_all([project, vendor])
    db.flush()
    install = InstallationProject(
        project_id=project.id,
        assigned_vendor_id=vendor.id,
        procurement_order_reference=po_reference,
    )
    db.add(install)
    db.flush()
    invoice = VendorPurchaseInvoice(
        project_id=install.id,
        vendor_id=vendor.id,
        invoice_number=f"VENDOR-{uuid4().hex[:8]}",
        status=VendorPurchaseInvoiceStatus.approved.value,
        currency="NGN",
        subtotal=Decimal("100000.00"),
        tax_total=Decimal("0.00"),
        total=Decimal("100000.00"),
    )
    db.add(invoice)
    db.flush()
    db.add(
        VendorPurchaseInvoiceLineItem(
            invoice_id=invoice.id,
            item_type="material",
            description="Repair-flow fixture line",
            quantity=Decimal("1"),
            unit_price=Decimal("100000.00"),
            amount=Decimal("100000.00"),
            is_active=True,
        )
    )
    db.commit()
    return invoice


class _FakeERPClient:
    """Mocked ERP client for outbox delivery + unlinked status polling."""

    def __init__(self, post_outcomes=None, status_outcomes=None):
        self._post = list(post_outcomes or [])
        self._status = list(status_outcomes or [])
        self.posts: list[dict] = []
        self.status_calls: list[str] = []
        self.closed = False

    def post(self, path, payload, idempotency_key=None, expected_status_codes=None):
        self.posts.append(
            {"path": path, "payload": payload, "idempotency_key": idempotency_key}
        )
        outcome = self._post.pop(0) if self._post else {}
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def get_purchase_invoice_status(self, source_invoice_id):
        self.status_calls.append(source_invoice_id)
        outcome = self._status.pop(0) if self._status else None
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def close(self):
        self.closed = True


def _outbox_rows(db, invoice) -> list[FieldErpSyncEvent]:
    return (
        db.query(FieldErpSyncEvent)
        .filter(FieldErpSyncEvent.entity_id == invoice.id)
        .all()
    )


# ---------------------------------------------------------------------------
# The sent-dead-end: a delivered-but-unlinked row must still be pollable
# ---------------------------------------------------------------------------


def test_status_refresh_drains_a_sent_row_the_linked_query_could_never_select(
    db_session,
):
    _seed_ownership(db_session, sub_flows={FieldErpSyncFlow.purchase_invoice.value})
    invoice = _approved_invoice(db_session)
    purchase_invoice_sync.enqueue_purchase_invoice(db_session, invoice)
    # Delivered with no terminal decision → sent, no reference on the invoice.
    outbox.deliver_pending(db_session, client=_FakeERPClient(post_outcomes=[{}]))
    db_session.refresh(invoice)
    assert invoice.payables_document_reference is None
    row = _outbox_rows(db_session, invoice)[0]
    assert row.status == FieldErpSyncStatus.sent.value

    client = _FakeERPClient(
        status_outcomes=[{"purchase_invoice_id": "ERP-PINV-LATE", "status": "created"}]
    )
    result = purchase_invoice_sync.refresh_purchase_invoice_statuses(
        db_session, client=client
    )

    db_session.refresh(invoice)
    row = _outbox_rows(db_session, invoice)[0]
    assert row.status == FieldErpSyncStatus.accepted.value
    assert invoice.payables_document_reference == "ERP-PINV-LATE"
    assert client.status_calls == [str(invoice.id)]
    # The unlinked poll folds into processed/errors, not observed/changed —
    # those stay reserved for the validated payment-observation loop.
    assert result["processed"] == 1
    assert result["observed"] == 0
    assert result["changed"] == 0
    assert result["errors"] == []


# ---------------------------------------------------------------------------
# Repair: non-vacuity — a usable stored response gets applied, no ERP call
# ---------------------------------------------------------------------------


def test_repair_applies_a_usable_stored_response_without_a_new_erp_call(db_session):
    _seed_ownership(db_session, sub_flows={FieldErpSyncFlow.purchase_invoice.value})
    invoice = _approved_invoice(db_session)
    purchase_invoice_sync.enqueue_purchase_invoice(db_session, invoice)
    outbox.deliver_pending(
        db_session,
        client=_FakeERPClient(
            post_outcomes=[{"purchase_invoice_id": "ERP-PINV-9", "status": "created"}]
        ),
    )
    db_session.refresh(invoice)
    assert invoice.payables_document_reference == "ERP-PINV-9"

    # Simulate a DROPPED write-back: the outbox row is terminal-accepted and
    # holds the ERP id, but the invoice lost its back-reference.
    invoice.payables_document_reference = None
    invoice.payables_submission_error = "stale evidence from a prior failure"
    db_session.commit()

    result = purchase_invoice_sync.repair_purchase_invoice_sync(db_session)

    db_session.refresh(invoice)
    assert result["enqueued"] == 0
    assert result["unlinked"] == 0
    assert invoice.payables_document_reference == "ERP-PINV-9"
    assert invoice.payables_submission_error is None
    # No re-emit: still exactly one outbox row, still terminal-accepted.
    rows = _outbox_rows(db_session, invoice)
    assert len(rows) == 1
    assert rows[0].status == FieldErpSyncStatus.accepted.value


# ---------------------------------------------------------------------------
# Repair: must not erase evidence when the stored response has no usable id
# ---------------------------------------------------------------------------


def test_repair_does_not_erase_evidence_when_the_stored_response_has_no_id(
    db_session,
):
    _seed_ownership(db_session, sub_flows={FieldErpSyncFlow.purchase_invoice.value})
    invoice = _approved_invoice(db_session)
    purchase_invoice_sync.enqueue_purchase_invoice(db_session, invoice)
    # Delivered (2xx) but the stored response carries no usable ERP id.
    outbox.deliver_pending(db_session, client=_FakeERPClient(post_outcomes=[{}]))
    db_session.refresh(invoice)
    assert invoice.payables_document_reference is None
    invoice.payables_submission_error = "diagnostic from the original attempt"
    db_session.commit()

    result = purchase_invoice_sync.repair_purchase_invoice_sync(db_session)

    db_session.refresh(invoice)
    assert result["enqueued"] == 0
    assert result["unlinked"] == 1
    assert invoice.payables_document_reference is None
    # The only diagnostic evidence must survive an unproductive repair pass.
    assert invoice.payables_submission_error == "diagnostic from the original attempt"


# ---------------------------------------------------------------------------
# Non-regression: the "no outbox row yet" enqueue case still works as before
# ---------------------------------------------------------------------------


def test_repair_still_enqueues_a_genuinely_new_invoice(db_session):
    _seed_ownership(db_session, sub_flows={FieldErpSyncFlow.purchase_invoice.value})
    invoice = _approved_invoice(db_session)
    invoice.payables_submission_error = "eligibility failed before"
    db_session.commit()

    result = purchase_invoice_sync.repair_purchase_invoice_sync(db_session)

    db_session.refresh(invoice)
    assert result["enqueued"] == 1
    assert result["unlinked"] == 0
    rows = _outbox_rows(db_session, invoice)
    assert len(rows) == 1
    assert rows[0].status == FieldErpSyncStatus.pending.value
    # A genuinely new enqueue does clear the prior submission error.
    assert invoice.payables_submission_error is None


# ---------------------------------------------------------------------------
# Ownership guard — a non-Sub-owned flow must cause no ERP call and no attachment
# upload, even for an invoice a repair sweep would otherwise act on.
# ---------------------------------------------------------------------------


class _AttachmentSpyERPClient(_FakeERPClient):
    """Extends the fake client with an upload spy for the attachment path."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.upload_calls: list[tuple] = []

    def upload_purchase_invoice_attachment(
        self, reference, payload, *, idempotency_key
    ):
        self.upload_calls.append((reference, payload, idempotency_key))
        return {"ok": True}


class _NullContextClient:
    """Minimal context-manager wrapper mirroring ``capability_client``'s usage."""

    def __init__(self, client):
        self._client = client

    def __enter__(self):
        return self._client

    def __exit__(self, *exc_info):
        return False


def test_repair_makes_no_erp_call_for_a_non_sub_owned_flow(db_session, monkeypatch):
    """Michael's finding: a scheduled repair must re-check ownership on every
    run, not just at write-time of the original event. A non-Sub-owned flow must
    see NO ERP call — including the attachment-upload consequence — and the
    row must be counted under ``skipped_not_owned``, not as a success.
    """
    from app.models.stored_file import StoredFile

    # Flow is explicitly not Sub-owned.
    _seed_ownership(db_session)
    invoice = _approved_invoice(db_session)
    # Already linked to ERP — this is the exact branch that calls
    # upload_attachment (a real ERP call) inside the repair sweep.
    invoice.payables_document_reference = "ERP-PINV-ALREADY-LINKED"
    invoice.payables_system = purchase_invoice_sync.PROVIDER
    db_session.commit()

    attachment = StoredFile(
        entity_type="vendor_purchase_invoice",
        entity_id=str(invoice.id),
        original_filename="invoice.pdf",
        storage_key_or_relative_path=f"attachments/{uuid4().hex}",
        file_size=4,
        content_type="application/pdf",
        storage_provider="s3",
    )
    db_session.add(attachment)
    db_session.flush()
    invoice.attachment_stored_file_id = attachment.id
    db_session.commit()

    monkeypatch.setattr(
        purchase_invoice_sync.file_uploads,
        "stream_file",
        lambda _attachment: type("S", (), {"chunks": iter([b"data"])})(),
    )
    spy_client = _AttachmentSpyERPClient()
    monkeypatch.setattr(
        purchase_invoice_sync,
        "capability_client",
        lambda db: _NullContextClient(spy_client),
    )

    result = purchase_invoice_sync.repair_purchase_invoice_sync(db_session)

    assert result["attachments"] == 0
    assert result["enqueued"] == 0
    assert result["unlinked"] == 0
    assert result["skipped_not_owned"] == 1
    assert spy_client.upload_calls == []
    db_session.refresh(invoice)
    assert invoice.payables_attachment_submitted_at is None


def test_repair_does_not_reapply_for_a_non_sub_owned_flow(db_session):
    """The 'delivered row with a usable stored response' branch re-applies
    ``apply_erp_response`` — a state mutation implying ERP involvement. It
    must also be skipped for a currently non-Sub-owned flow, even though the
    outbox row was, by construction, delivered while sub owned the flow.
    """
    _seed_ownership(db_session, sub_flows={FieldErpSyncFlow.purchase_invoice.value})
    invoice = _approved_invoice(db_session)
    purchase_invoice_sync.enqueue_purchase_invoice(db_session, invoice)
    outbox.deliver_pending(
        db_session,
        client=_FakeERPClient(
            post_outcomes=[{"purchase_invoice_id": "ERP-PINV-9", "status": "created"}]
        ),
    )
    db_session.refresh(invoice)
    assert invoice.payables_document_reference == "ERP-PINV-9"

    # Simulate a DROPPED write-back.
    invoice.payables_document_reference = None
    invoice.payables_submission_error = "stale evidence from a prior failure"
    db_session.commit()

    # Ownership moves away from Sub before the scheduled repair runs again.
    ownership_row = (
        db_session.query(SyncFlowOwnership)
        .filter(SyncFlowOwnership.flow == FieldErpSyncFlow.purchase_invoice.value)
        .one()
    )
    ownership_row.owner = _NON_SUB_OWNER
    db_session.commit()

    result = purchase_invoice_sync.repair_purchase_invoice_sync(db_session)

    db_session.refresh(invoice)
    assert result["enqueued"] == 0
    assert result["unlinked"] == 0
    assert result["skipped_not_owned"] == 1
    # No re-apply happened: the invoice stays exactly as it was left.
    assert invoice.payables_document_reference is None
    assert invoice.payables_submission_error == "stale evidence from a prior failure"


def test_unlinked_status_poll_skips_a_non_sub_owned_flow(db_session):
    """The poll-drain path (``_poll_unlinked_purchase_invoices``, reached via
    ``refresh_purchase_invoice_statuses``) makes a real ERP call
    (``get_purchase_invoice_status``). It must also be skipped for a
    currently non-Sub-owned flow.
    """
    _seed_ownership(db_session, sub_flows={FieldErpSyncFlow.purchase_invoice.value})
    invoice = _approved_invoice(db_session)
    purchase_invoice_sync.enqueue_purchase_invoice(db_session, invoice)
    outbox.deliver_pending(db_session, client=_FakeERPClient(post_outcomes=[{}]))
    db_session.refresh(invoice)
    assert invoice.payables_document_reference is None
    row = _outbox_rows(db_session, invoice)[0]
    assert row.status == FieldErpSyncStatus.sent.value

    # Ownership moves away from Sub before the poll runs.
    ownership_row = (
        db_session.query(SyncFlowOwnership)
        .filter(SyncFlowOwnership.flow == FieldErpSyncFlow.purchase_invoice.value)
        .one()
    )
    ownership_row.owner = _NON_SUB_OWNER
    db_session.commit()

    client = _FakeERPClient(
        status_outcomes=[{"purchase_invoice_id": "SHOULD-NOT-HAPPEN"}]
    )
    result = purchase_invoice_sync.refresh_purchase_invoice_statuses(
        db_session, client=client
    )

    assert client.status_calls == []
    assert result["skipped_not_owned"] == 1
    db_session.refresh(invoice)
    assert invoice.payables_document_reference is None
    row = _outbox_rows(db_session, invoice)[0]
    assert row.status == FieldErpSyncStatus.sent.value


def test_enqueue_does_not_clear_submission_error_on_a_no_op_return(db_session):
    """``enqueue_purchase_invoice`` returning the EXISTING row (idempotent
    no-op) must not clear ``payables_submission_error`` — only a genuinely
    new enqueue may.
    """
    _seed_ownership(db_session, sub_flows={FieldErpSyncFlow.purchase_invoice.value})
    invoice = _approved_invoice(db_session)
    first = purchase_invoice_sync.enqueue_purchase_invoice(db_session, invoice)
    db_session.commit()
    assert first is not None
    invoice.payables_submission_error = "unrelated diagnostic"
    db_session.commit()

    second = purchase_invoice_sync.enqueue_purchase_invoice(db_session, invoice)
    db_session.commit()

    assert second.id == first.id
    db_session.refresh(invoice)
    assert invoice.payables_submission_error == "unrelated diagnostic"
