"""ERP re-home PR 4 — purchase-order (PO) origination flow (map + enqueue +
write-back + repair).

Everything runs against a MOCKED ERP: the outbox uses a fake client, and the
write-back repair uses the outbox row's stored response (no ERP call at all). The
flow is proven end-to-end WITHOUT a live ERP and WITHOUT flipping ownership in
prod — tests set ``sync_flow_ownership.purchase_order = sub`` in-test only. The
default (crm) path is asserted to send nothing (the inert guarantee).

Anchor = installation project / accepted quote (design doc 32 §D): the idempotency
key is ``po-ip-{install.id}`` and ``source_work_order_id`` carries the install UUID.
"""

from __future__ import annotations

from datetime import UTC, datetime
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
from app.models.subscriber import Subscriber
from app.models.vendor_routes import (
    InstallationProject,
    ProjectQuote,
    ProjectQuoteLineItem,
    ProjectQuoteStatus,
    Vendor,
    VendorAssignmentType,
)
from app.services.dotmac_erp import outbox, purchase_order_sync

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _seed_ownership(db, *, sub_flows: set[str] | None = None) -> None:
    sub_flows = sub_flows or set()
    for flow in FieldErpSyncFlow:
        owner = (
            SyncFlowOwner.sub.value
            if flow.value in sub_flows
            else SyncFlowOwner.crm.value
        )
        db.add(SyncFlowOwnership(flow=flow.value, owner=owner))
    db.flush()


def _subscriber(db) -> Subscriber:
    subscriber = Subscriber(
        first_name="PO",
        last_name="Customer",
        email=f"po-{uuid4().hex[:8]}@example.com",
    )
    db.add(subscriber)
    db.flush()
    return subscriber


def _approved_install(
    db,
    *,
    vendor_erp_id: str | None = "SUP-001",
    vendor_code: str | None = "SKY",
    quote_status: str = ProjectQuoteStatus.approved.value,
    with_line_item: bool = True,
    line_quantity: Decimal | int = 2,
) -> InstallationProject:
    """Build subscriber → project → vendor → install → approved quote (+ line)."""
    subscriber = _subscriber(db)
    project = Project(
        name="Fiber install — Ngozi", code="PRJ-42", subscriber_id=subscriber.id
    )
    db.add(project)
    db.flush()

    vendor = Vendor(
        name="Skyline Fiber Ltd", code=vendor_code, supplier_reference=vendor_erp_id
    )
    db.add(vendor)
    db.flush()

    install = InstallationProject(
        project_id=project.id,
        subscriber_id=subscriber.id,
        assigned_vendor_id=vendor.id,
        assignment_type=VendorAssignmentType.direct.value,
        created_by_person_id=uuid4(),
    )
    db.add(install)
    db.flush()

    quote = ProjectQuote(
        project_id=install.id,
        vendor_id=vendor.id,
        status=quote_status,
        currency="NGN",
        subtotal=Decimal("250000.00"),
        tax_total=Decimal("18750.00"),
        total=Decimal("268750.00"),
        reviewed_at=datetime.now(UTC),
        reviewed_by_person_id=uuid4(),
        created_by_person_id=uuid4(),
    )
    db.add(quote)
    db.flush()

    if with_line_item:
        db.add(
            ProjectQuoteLineItem(
                quote_id=quote.id,
                item_type="fiber_run",
                description="Aerial fiber run",
                cable_type="ADSS-24F",
                fiber_count=24,
                splice_count=2,
                quantity=Decimal(line_quantity),
                unit_price=Decimal("125000.00"),
                amount=Decimal("250000.00"),
                notes="north span",
                client_ref=uuid4(),
            )
        )
        db.flush()

    install.approved_quote_id = quote.id
    db.flush()
    db.commit()
    return install


class _FakeERPClient:
    """Mocked ERP client for the outbox: canned POST responses in order."""

    def __init__(self, post_outcomes=None):
        self._post = list(post_outcomes or [])
        self.posts: list[dict] = []
        self.closed = False

    def post(self, path, payload, idempotency_key=None, expected_status_codes=None):
        self.posts.append(
            {"path": path, "payload": payload, "idempotency_key": idempotency_key}
        )
        outcome = self._post.pop(0) if self._post else {}
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def close(self):
        self.closed = True


def _outbox_rows(db, install) -> list[FieldErpSyncEvent]:
    return (
        db.query(FieldErpSyncEvent)
        .filter(FieldErpSyncEvent.entity_id == install.id)
        .all()
    )


# ---------------------------------------------------------------------------
# Payload mapping (from the ACCEPTED quote, anchored on the install)
# ---------------------------------------------------------------------------


def test_payload_built_from_accepted_quote(db_session):
    install = _approved_install(db_session)
    payload = purchase_order_sync.build_purchase_order_payload(install)

    # Anchored on the installation project (design doc 32 §D).
    assert payload["source_work_order_id"] == str(install.id)
    assert payload["source_quote_id"] == str(install.approved_quote_id)

    # Vendor identity from the native Vendor on the accepted quote.
    assert payload["vendor_erp_id"] == "SUP-001"
    assert payload["vendor_code"] == "SKY"
    assert payload["vendor_name"] == "Skyline Fiber Ltd"

    # Totals from the accepted quote.
    assert payload["currency"] == "NGN"
    assert payload["subtotal"] == "250000.00"
    assert payload["tax_total"] == "18750.00"
    assert payload["total"] == "268750.00"

    # Project context via installation_project.project.
    assert payload["source_project_id"] == str(install.project_id)
    assert payload["project_code"] == "PRJ-42"
    assert payload["project_name"] == "Fiber install — Ngozi"
    assert payload["title"] == "Fiber install — Ngozi"

    # Line items mapped to the ERP item shape.
    assert len(payload["items"]) == 1
    line = payload["items"][0]
    assert line["item_type"] == "fiber_run"
    assert line["description"] == "Aerial fiber run"
    assert line["quantity"] == "2.000"
    assert line["unit_price"] == "125000.00"
    assert line["amount"] == "250000.00"
    assert line["cable_type"] == "ADSS-24F"
    assert line["fiber_count"] == 24
    assert line["splice_count"] == 2
    assert line["notes"] == "north span"

    # Sub has no people table → approved_by_email omitted (design doc 32 §C/§F.6).
    assert "approved_by_email" not in payload


def test_idempotency_key_is_installation_project_anchor(db_session):
    install = _approved_install(db_session)
    key = purchase_order_sync.purchase_order_idempotency_key(install)
    assert key == f"po-ip-{install.id}"


def test_zero_quantity_lines_are_skipped(db_session):
    install = _approved_install(db_session, line_quantity=0)
    # No positive-quantity line → not eligible, and payload builder drops it.
    assert (
        "no active, positive-quantity line items"
        in purchase_order_sync.purchase_order_eligibility_error(install)
    )


# ---------------------------------------------------------------------------
# Eligibility guards
# ---------------------------------------------------------------------------


def test_eligible_install_has_no_error(db_session):
    install = _approved_install(db_session)
    assert purchase_order_sync.purchase_order_eligibility_error(install) is None


def test_vendor_missing_erp_id_is_ineligible(db_session):
    install = _approved_install(db_session, vendor_erp_id=None)
    reason = purchase_order_sync.purchase_order_eligibility_error(install)
    assert "blank supplier" in reason


def test_unapproved_quote_is_ineligible(db_session):
    install = _approved_install(
        db_session, quote_status=ProjectQuoteStatus.submitted.value
    )
    reason = purchase_order_sync.purchase_order_eligibility_error(install)
    assert "cannot emit a PO" in reason


# ---------------------------------------------------------------------------
# Enqueue
# ---------------------------------------------------------------------------


def test_enqueue_writes_one_outbox_row(db_session):
    install = _approved_install(db_session)
    row = purchase_order_sync.enqueue_purchase_order(db_session, install)

    assert row is not None
    assert row.flow == FieldErpSyncFlow.purchase_order.value
    assert row.entity_type == "installation_project"
    assert row.idempotency_key == f"po-ip-{install.id}"
    assert row.status == FieldErpSyncStatus.pending.value
    assert row.payload["source_work_order_id"] == str(install.id)
    assert len(_outbox_rows(db_session, install)) == 1


def test_enqueue_vendor_missing_writes_no_row(db_session):
    install = _approved_install(db_session, vendor_erp_id=None)
    row = purchase_order_sync.enqueue_purchase_order(db_session, install)

    assert row is None
    assert _outbox_rows(db_session, install) == []


def test_reenqueue_reuses_the_same_outbox_row(db_session):
    install = _approved_install(db_session)
    first = purchase_order_sync.enqueue_purchase_order(db_session, install)
    second = purchase_order_sync.enqueue_purchase_order(db_session, install)
    assert first.id == second.id
    assert len(_outbox_rows(db_session, install)) == 1


# ---------------------------------------------------------------------------
# Outbox delivery → write-back onto the installation project
# ---------------------------------------------------------------------------


def test_delivery_writes_erp_po_id_back(db_session):
    _seed_ownership(db_session, sub_flows={FieldErpSyncFlow.purchase_order.value})
    install = _approved_install(db_session)
    purchase_order_sync.enqueue_purchase_order(db_session, install)
    client = _FakeERPClient(
        post_outcomes=[
            {
                "purchase_order_id": "PO-ERP-1",
                "po_id": str(uuid4()),
                "status": "draft",
                "source_work_order_id": str(install.id),
            }
        ]
    )

    result = outbox.deliver_pending(db_session, client=client)

    db_session.refresh(install)
    assert result.accepted == 1
    assert install.procurement_order_reference == "PO-ERP-1"
    assert install.procurement_system == "dotmac_erp"
    assert install.procurement_delivery_status == "accepted"
    assert install.procurement_delivery_error is None
    assert install.procurement_delivered_at is not None
    assert client.posts[0]["path"] == "/api/v1/sync/sub/purchase-orders"
    assert client.posts[0]["idempotency_key"] == f"po-ip-{install.id}"


# ---------------------------------------------------------------------------
# Ownership guard — the inert guarantee
# ---------------------------------------------------------------------------


def test_delivery_refused_when_flow_owned_by_crm(db_session):
    # purchase_order left at the seeded default (crm) — must NOT be sent.
    _seed_ownership(db_session)
    install = _approved_install(db_session)
    purchase_order_sync.enqueue_purchase_order(db_session, install)
    client = _FakeERPClient(post_outcomes=[{"purchase_order_id": "SHOULD-NOT-HAPPEN"}])

    result = outbox.deliver_pending(db_session, client=client)

    db_session.refresh(install)
    assert result.skipped_not_owned == 1
    assert client.posts == []
    assert install.procurement_order_reference is None
    row = _outbox_rows(db_session, install)[0]
    assert row.status == FieldErpSyncStatus.pending.value
    assert row.attempts == 0


# ---------------------------------------------------------------------------
# Write-back repair — a delivered PO whose write-back was lost, without re-emit
# ---------------------------------------------------------------------------


def test_repair_restores_lost_writeback_without_reemitting(db_session):
    _seed_ownership(db_session, sub_flows={FieldErpSyncFlow.purchase_order.value})
    install = _approved_install(db_session)
    purchase_order_sync.enqueue_purchase_order(db_session, install)
    client = _FakeERPClient(post_outcomes=[{"purchase_order_id": "PO-ERP-9"}])
    outbox.deliver_pending(db_session, client=client)

    # Simulate a DROPPED write-back: the outbox row is terminal-accepted and holds
    # the ERP id, but the install lost its back-reference.
    db_session.refresh(install)
    assert install.procurement_order_reference == "PO-ERP-9"
    install.procurement_order_reference = None
    db_session.commit()

    result = purchase_order_sync.repair_purchase_order_writebacks(db_session)

    db_session.refresh(install)
    assert result["repaired"] == 1
    assert install.procurement_order_reference == "PO-ERP-9"
    # No re-emit: still exactly one outbox row, still terminal-accepted.
    rows = _outbox_rows(db_session, install)
    assert len(rows) == 1
    assert rows[0].status == FieldErpSyncStatus.accepted.value


def test_repair_is_noop_when_writeback_already_present(db_session):
    _seed_ownership(db_session, sub_flows={FieldErpSyncFlow.purchase_order.value})
    install = _approved_install(db_session)
    purchase_order_sync.enqueue_purchase_order(db_session, install)
    client = _FakeERPClient(post_outcomes=[{"purchase_order_id": "PO-ERP-7"}])
    outbox.deliver_pending(db_session, client=client)

    result = purchase_order_sync.repair_purchase_order_writebacks(db_session)

    db_session.refresh(install)
    assert result["repaired"] == 0
    assert install.procurement_order_reference == "PO-ERP-7"


def test_repair_never_writes_back_a_rejected_or_dead_row(db_session):
    """A rejected/dead outbox row must never be repaired as if ERP had accepted it.

    Before the status filter fix, ``repair_purchase_order_writebacks`` scanned
    every ``purchase_order`` row carrying ANY ``erp_response`` -- including
    terminal ``rejected``/``dead`` rows. A row that carries an id-shaped payload
    despite being terminally rejected must be left alone: writing it back would
    mark ``procurement_delivery_status="accepted"`` on an install ERP actually
    refused.
    """
    _seed_ownership(db_session, sub_flows={FieldErpSyncFlow.purchase_order.value})
    install = _approved_install(db_session)
    row = purchase_order_sync.enqueue_purchase_order(db_session, install)

    # Simulate a terminal rejection whose stored response still carries an
    # id-shaped payload (e.g. ERP echoed a would-be id alongside the rejection).
    row.status = FieldErpSyncStatus.rejected.value
    row.erp_response = {"purchase_order_id": "PO-SHOULD-NOT-APPLY"}
    db_session.commit()

    result = purchase_order_sync.repair_purchase_order_writebacks(db_session)

    db_session.refresh(install)
    assert result["repaired"] == 0
    assert install.procurement_order_reference is None
    assert install.procurement_delivery_status != "accepted"


def test_repair_makes_no_erp_call_for_a_crm_owned_flow(db_session, monkeypatch):
    """Michael's finding: a scheduled repair must re-check ownership on every
    run, not just at write-time of the original event. A CRM-owned flow must
    see the write-back consequence skipped entirely — no mutation of the
    installation project — and the row must be counted under
    ``skipped_not_owned``, not as a success.
    """
    # Deliver a PO while sub owns the flow, so a terminal-accepted outbox row
    # with a stored ``erp_response`` exists as a repair candidate.
    _seed_ownership(db_session, sub_flows={FieldErpSyncFlow.purchase_order.value})
    install = _approved_install(db_session)
    purchase_order_sync.enqueue_purchase_order(db_session, install)
    client = _FakeERPClient(post_outcomes=[{"purchase_order_id": "PO-ERP-11"}])
    outbox.deliver_pending(db_session, client=client)

    # Simulate a DROPPED write-back, then ownership moves back to CRM before
    # the scheduled repair sweep next runs.
    db_session.refresh(install)
    install.procurement_order_reference = None
    install.procurement_system = None
    install.procurement_delivery_status = None
    db_session.commit()

    ownership_row = (
        db_session.query(SyncFlowOwnership)
        .filter(SyncFlowOwnership.flow == FieldErpSyncFlow.purchase_order.value)
        .one()
    )
    ownership_row.owner = SyncFlowOwner.crm.value
    db_session.commit()

    calls: list[object] = []
    original_apply = purchase_order_sync.apply_purchase_order_response

    def _spy_apply(installation_project, response):
        calls.append(installation_project)
        return original_apply(installation_project, response)

    monkeypatch.setattr(
        purchase_order_sync, "apply_purchase_order_response", _spy_apply
    )

    result = purchase_order_sync.repair_purchase_order_writebacks(db_session)

    db_session.refresh(install)
    assert calls == []
    assert install.procurement_order_reference is None
    assert install.procurement_delivery_status != "accepted"
    assert result["repaired"] == 0
    assert result["skipped_not_owned"] == 1
