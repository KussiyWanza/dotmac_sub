"""Purchase-order origination flow for the Sub → DotMac ERP outbox.

The vendor-facing money flow. Re-homes PO **origination** from CRM into sub and,
per the locked accounting decision (design doc 32 §0/§A), **re-anchors the PO on
the accepted quote / installation project** instead of CRM's work-order anchor.
Ports the *mapping* from ``dotmac_crm/app/services/dotmac_erp/po_sync.py`` but
sources every field from the ACCEPTED quote — never a work order, never material
requests (design doc 32 §B, the two-ledger rule).

Three responsibilities live here, structurally identical to ``material_sync`` /
``expense_sync``:

* **map + enqueue** — ``enqueue_purchase_order`` builds the ERP payload from the
  installation project's ``approved_quote``, computes the stable idempotency key
  ``po-ip-{installation_project.id}`` (the installation-project anchor Michael
  chose), and hands it to ``outbox.enqueue``. It does NOT deliver — the worker
  owns delivery, and the outbox refuses any flow sub does not own in
  ``sync_flow_ownership``.
* **write-back** — ``apply_erp_response`` runs on the outbox's accepted path and
  writes ERP's ``purchase_order_id`` back onto
  ``installation_projects.procurement_order_reference`` (the AP vendor-invoice
  ordering guard requires it). A dropped
  write-back silently loses the AP link, so it must be repairable (see below).
* **reconcile / repair** — ``repair_purchase_order_writebacks`` re-applies the PO
  id from a *delivered* outbox row whose write-back never landed on the
  installation project. ERP exposes **no** GET for a PO (its create is idempotent
  on the anchor id and returns the existing PO on resend), and CRM never polls a
  PO either — so the repair is done from the outbox row's stored ``erp_response``,
  with **no** ERP call and **no** re-emit (design doc 32 §E.4).

ANCHOR = INSTALLATION PROJECT (design doc 32 §D):

* idempotency key ``po-ip-{installation_project.id}`` — one PO per install, stable
  across re-approvals of the same install so a re-enqueue returns the existing
  outbox row and a re-delivery is a no-op on the ERP side.
* payload ``source_work_order_id = str(installation_project.id)`` — the neutral
  role carries the installation-project UUID used for ERP idempotency.

A *re-quote* (a new accepted quote superseding the old for the same install)
would ride a future ``purchase_order_variation`` flow (design doc 32 §D
"Variations", §F.3) so the AP ``superseded_po_id`` audit chain is preserved.
The current purchase-order owner leaves that slot explicit but unimplemented;
see ``VARIATION_FLOW_SLOT``.

VENDOR IDENTITY (design doc 32 §C): sourced from the **native ``Vendor``**
reachable via ``approved_quote.vendor`` — which carries a provider-scoped
``supplier_reference`` and a local ``code``. The mobile-auth ``FieldVendor``
mirror is irrelevant here. If the Dotmac ERP supplier reference is unset the PO
is skipped and surfaced (ported from CRM's ``vendor_missing`` skip): a PO must
never be emitted to a blank supplier.

``approved_by_email`` is OMITTED: sub has no people table
(``ProjectQuote.reviewed_by_person_id`` is a bare UUID), and the field is
optional in the ERP schema (design doc 32 §C/§F.6). Non-blocking.

INERT UNTIL CUTOVER: nothing here sends. Enqueue is only meant to run when the
master flag ``dotmac_erp_sync_enabled`` is on, and delivery is additionally gated
per-flow by ``sync_flow_ownership.purchase_order`` (seeded ``crm``). Both must
flip at cutover before a single PO reaches ERP (design doc 32 §E single-writer).
Unlike expense/material, sub has no quote-approval service yet to invoke the
enqueue hook, so ``enqueue_purchase_order`` is provided as the hook target for
when that flow lands (design doc 32 §C "the one genuine gap").
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy.orm import Session

from app.models.field_erp_sync import (
    FieldErpSyncEvent,
    FieldErpSyncFlow,
    FieldErpSyncStatus,
    flow_owned_by_sub,
)
from app.models.vendor_routes import (
    InstallationProject,
    ProjectQuote,
    ProjectQuoteStatus,
)
from app.services.dotmac_erp import outbox

logger = logging.getLogger(__name__)

ENTITY_TYPE = "installation_project"
PROVIDER = "dotmac_erp"

# The future scope-change flow (design doc 32 §D "Variations" / §F.3): a re-quote
# supersedes the baseline PO via ERP's ``/sync/sub/purchase-orders/variations``,
# keyed ``variation_id``, preserving the AP ``superseded_po_id`` chain. Left as a
# noted slot; not implemented by the current purchase-order owner.
VARIATION_FLOW_SLOT = "purchase_order_variation"


# ---------------------------------------------------------------------------
# Idempotency key (installation-project anchor)
# ---------------------------------------------------------------------------


def purchase_order_idempotency_key(installation_project: InstallationProject) -> str:
    """Stable per-install key: ``po-ip-{installation_project.id}``.

    Constant across re-approvals of the same install, so a re-enqueue returns the
    existing outbox row and a re-delivery is a no-op on the ERP side (ERP dedups a
    PO per ``source_work_order_id``, which carries this install's UUID).
    """
    return f"po-ip-{installation_project.id}"


# ---------------------------------------------------------------------------
# Mapping (port of CRM's _map_purchase_order, re-anchored on the accepted quote)
# ---------------------------------------------------------------------------


def _decimal(value: object) -> Decimal:
    try:
        return Decimal(str(value if value is not None else "0"))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0")


def _line_item_rows(quote: ProjectQuote) -> list[dict[str, object]]:
    """Map a quote's active line items to ERP's ``CRMPurchaseOrderItemPayload`` shape.

    Sends decimals as strings (verbatim CRM parity). Skips inactive lines and
    lines with a non-positive quantity (ERP requires ``quantity > 0``); the CRM
    legacy zero-quantity coercion is intentionally NOT ported (it fixed specific
    legacy CRM quote ids that do not exist in sub's new-only scope). Synthesises a
    non-empty ``description`` when absent, since ERP requires it.
    """
    rows: list[dict[str, object]] = []
    for item in quote.line_items:
        if not item.is_active:
            continue
        quantity = _decimal(item.quantity)
        if quantity <= 0:
            continue
        item_type = (item.item_type or "").strip() or "item"
        description = (item.description or "").strip()
        if not description:
            description = f"{item_type.replace('_', ' ').title()} item"
        row: dict[str, object] = {
            "item_type": item_type[:50],
            "description": description[:500],
            "quantity": str(quantity),
            "unit_price": str(_decimal(item.unit_price)),
            "amount": str(_decimal(item.amount)),
        }
        if item.cable_type:
            row["cable_type"] = item.cable_type
        if item.fiber_count is not None:
            row["fiber_count"] = item.fiber_count
        if item.splice_count is not None:
            row["splice_count"] = item.splice_count
        if item.notes:
            row["notes"] = item.notes
        rows.append(row)
    return rows


def _po_title(installation_project: InstallationProject) -> str:
    """A non-empty PO title (ERP requires it; sub has no WO to borrow one from).

    Uses the native project's name; falls back to the install id so the field is
    never blank.
    """
    project = installation_project.project
    name = (getattr(project, "name", None) or "").strip()
    title = name or f"Installation project {installation_project.id}"
    return title[:500]


def build_purchase_order_payload(installation_project: InstallationProject) -> dict:
    """Map an accepted quote to ERP's ``SubPurchaseOrderPayload`` contract.

    Anchors the PO on the installation project (design doc 32 §D): the idempotency
    field ``source_work_order_id`` carries ``str(installation_project.id)``. Vendor
    identity, line items, and totals come strictly from ``approved_quote`` (never
    a WO, never material requests). Assumes the project is eligible — call
    ``purchase_order_eligibility_error`` first.
    """
    quote = installation_project.approved_quote
    vendor = quote.vendor
    project = installation_project.project

    payload: dict[str, object] = {
        "source_work_order_id": str(installation_project.id),
        "source_quote_id": str(quote.id),
        "vendor_name": vendor.name,
        "title": _po_title(installation_project),
        "currency": quote.currency,
        "subtotal": str(_decimal(quote.subtotal)),
        "tax_total": str(_decimal(quote.tax_total)),
        "total": str(_decimal(quote.total)),
        "items": _line_item_rows(quote),
    }

    # Vendor → ERP supplier identity (native Vendor; provider-scoped reference
    # guaranteed by the eligibility check).
    payload["vendor_erp_id"] = vendor.supplier_reference
    vendor_code = (vendor.code or "").strip()
    if vendor_code:
        payload["vendor_code"] = vendor_code[:30]

    if project is not None:
        payload["source_project_id"] = str(project.id)
        if getattr(project, "code", None):
            payload["project_code"] = str(project.code)[:80]
        if getattr(project, "name", None):
            payload["project_name"] = str(project.name)[:200]

    if quote.reviewed_at:
        payload["approved_at"] = quote.reviewed_at.isoformat()
    # approved_by_email intentionally omitted — sub has no people table and the
    # field is optional in the ERP schema (design doc 32 §C/§F.6).

    return payload


def purchase_order_eligibility_error(
    installation_project: InstallationProject,
) -> str | None:
    """Return a reason string if the install is NOT eligible for PO sync, else None.

    Guards the money path so an invalid or premature enqueue never reaches ERP:

    * the install must have an ACCEPTED (approved) quote pinned as its scope
      authority (design doc 32 §A);
    * that quote must carry a vendor with a Dotmac ERP supplier reference — else
      the PO would be emitted to a blank supplier (CRM's ``vendor_missing``
      skip, design doc 32 §F.4);
    * the quote must have at least one active, positive-quantity line item (ERP
      requires ``items`` min length 1, ``quantity > 0``).
    """
    quote = installation_project.approved_quote
    if quote is None or installation_project.approved_quote_id is None:
        return (
            f"Installation project {installation_project.id} has no approved quote "
            "pinned — nothing to originate a PO from"
        )
    if quote.status != ProjectQuoteStatus.approved.value:
        return (
            f"Approved quote {quote.id} is in {quote.status} status, not "
            f"'{ProjectQuoteStatus.approved.value}' — cannot emit a PO"
        )
    vendor = quote.vendor
    if vendor is None:
        return (
            f"Approved quote {quote.id} has no vendor relation "
            f"(vendor_id={quote.vendor_id}) — cannot emit a PO"
        )
    if vendor.supplier_system not in {None, PROVIDER}:
        return "Vendor is linked to another procurement system"
    if not (vendor.supplier_reference or "").strip():
        return (
            f"Vendor {vendor.id} has no Dotmac ERP supplier reference "
            "— refusing to emit a PO to a blank supplier"
        )
    if not _line_item_rows(quote):
        return (
            f"Approved quote {quote.id} has no active, positive-quantity line items "
            "— cannot emit a PO"
        )
    return None


# ---------------------------------------------------------------------------
# Enqueue (quote-approval hook target — see module docstring)
# ---------------------------------------------------------------------------


def enqueue_purchase_order(
    db: Session, installation_project: InstallationProject
) -> FieldErpSyncEvent | None:
    """Enqueue the PO outbox intent for an install with a freshly accepted quote.

    Validates eligibility, builds the payload + stable key, and calls
    ``outbox.enqueue`` (idempotent on the key). Returns the outbox row, or ``None``
    when the install is not eligible (logged, not raised — an ineligible install
    must never break the approval transaction). Does NOT deliver.

    New-only (design doc 32 §E.3): this is a hook for a *new* quote acceptance;
    historical POs CRM already pushed must never be replayed here.
    """
    reason = purchase_order_eligibility_error(installation_project)
    if reason:
        logger.info(
            "purchase_order_sync: not enqueuing PO for installation project %s — %s",
            installation_project.id,
            reason,
        )
        return None

    payload = build_purchase_order_payload(installation_project)
    return outbox.enqueue(
        db,
        flow=FieldErpSyncFlow.purchase_order,
        entity_type=ENTITY_TYPE,
        entity_id=installation_project.id,
        idempotency_key=purchase_order_idempotency_key(installation_project),
        payload=payload,
        isolate=False,
    )


# ---------------------------------------------------------------------------
# Response write-back (outbox accepted path + reconcile repair)
# ---------------------------------------------------------------------------


def _extract_purchase_order_id(response: dict | None) -> str | None:
    if not isinstance(response, dict):
        return None
    erp_id = response.get("purchase_order_id") or response.get("po_id")
    return str(erp_id) if erp_id else None


def apply_purchase_order_response(
    installation_project: InstallationProject, response: dict | None
) -> bool:
    """Write ERP's ``purchase_order_id`` back onto the installation project.

    Records the AP back-reference (``procurement_order_reference``, ``String(100)``) that
    the AP vendor-invoice ordering guard requires. Idempotent and
    write-once: an already-populated back-reference is left untouched (the PO id is
    stable, and re-writing would mask a mismatch). Returns True when it set the
    field, else False.
    """
    if not isinstance(response, dict):
        return False
    if installation_project.procurement_order_reference:
        return False
    erp_id = _extract_purchase_order_id(response)
    if not erp_id:
        return False
    installation_project.procurement_system = PROVIDER
    installation_project.procurement_order_reference = erp_id[:100]
    installation_project.procurement_delivery_status = "accepted"
    installation_project.procurement_delivery_error = None
    installation_project.procurement_delivered_at = datetime.now(UTC)
    return True


def apply_erp_response(db: Session, event: FieldErpSyncEvent) -> None:
    """Outbox write-back hook: apply a delivered PO event's ERP response to its source.

    Called by ``outbox.deliver_pending`` after a 2xx classify, within the same
    transaction (the outbox commits the row). Loads the ``InstallationProject`` the
    event pushed and applies ``apply_purchase_order_response``. Missing source rows
    are logged, not raised — the event still records its terminal outcome.
    """
    installation_project = db.get(InstallationProject, event.entity_id)
    if installation_project is None:
        logger.warning(
            "purchase_order_sync: outbox event %s has no InstallationProject %s to "
            "write the ERP PO id back to",
            event.id,
            event.entity_id,
        )
        return
    if not apply_purchase_order_response(installation_project, event.erp_response):
        return
    # PO and invoice deliveries may race. Once the PO ID lands, make any
    # already-approved vendor invoice eligible for the outbox sweep.
    from app.services.dotmac_erp.purchase_invoice_sync import enqueue_purchase_invoice

    for invoice in installation_project.purchase_invoices:
        enqueue_purchase_invoice(db, invoice)


# ---------------------------------------------------------------------------
# Reconcile / repair (beat-driven, gated by dotmac_erp_sync_enabled)
# ---------------------------------------------------------------------------


def repair_purchase_order_writebacks(db: Session, *, limit: int = 100) -> dict:
    """Repair PO write-backs that were delivered to ERP but never landed on the install.

    A dropped write-back silently loses the AP link (design doc 32 §D/§E.4). ERP
    exposes no GET for a PO and its create is idempotent on the anchor id, so the
    repair needs no ERP call and no re-emit: it re-applies the ``purchase_order_id``
    already captured on the *delivered* outbox row's ``erp_response`` to any
    installation project still missing ``procurement_order_reference``.

    Scans terminal-accepted / sent ``purchase_order`` outbox rows carrying an ERP
    id whose install has an empty back-reference, and writes it back. Idempotent;
    safe to re-run. Read-only against ERP.

    Restricted to ``status IN (accepted, sent)`` — the two statuses meaning "ERP
    said yes, but our own link may be missing." A ``rejected`` or ``dead`` row can
    still carry a stale/partial ``erp_response`` payload (e.g. a prior accepted
    attempt's echoed id before a later terminal rejection), and must never be
    written back as if ERP had accepted it.

    OWNERSHIP GUARD: this is a scheduled sweep left running across cutovers —
    ``sync_flow_ownership`` can move the ``purchase_order`` flow back to CRM
    after this repair was first wired, and a stale schedule must not keep
    acting on a flow it no longer owns. Checked ONCE per run (ownership is a
    per-flow switch, not per-row), before the repair consequence below:
    re-applying a stored response mutates the installation project's
    procurement fields as if ERP had accepted the PO. A row is skipped, not
    errored, when sub does not currently own this flow, and counted under
    ``skipped_not_owned`` so the sweep's own numbers stay honest.
    """
    limit = max(1, min(int(limit or 100), 500))
    owned = flow_owned_by_sub(db, FieldErpSyncFlow.purchase_order)
    rows = (
        db.query(FieldErpSyncEvent)
        .filter(FieldErpSyncEvent.flow == FieldErpSyncFlow.purchase_order.value)
        .filter(
            FieldErpSyncEvent.status.in_(
                (
                    FieldErpSyncStatus.accepted.value,
                    FieldErpSyncStatus.sent.value,
                )
            )
        )
        .filter(FieldErpSyncEvent.erp_response.isnot(None))
        .order_by(FieldErpSyncEvent.updated_at.asc())
        .limit(limit)
        .all()
    )

    errors: list[str] = []
    result: dict[str, object] = {
        "processed": 0,
        "repaired": 0,
        "skipped_not_owned": 0,
        "errors": errors,
    }
    if not rows:
        return result

    if not owned:
        logger.info(
            "purchase_order_sync: skipping write-back repair — sub does not "
            "own flow 'purchase_order' (sync_flow_ownership)"
        )
        result["skipped_not_owned"] = len(rows)
        return result

    processed = 0
    repaired = 0
    for row in rows:
        erp_id = _extract_purchase_order_id(row.erp_response)
        if not erp_id:
            continue
        installation_project = db.get(InstallationProject, row.entity_id)
        if installation_project is None:
            errors.append(f"{row.id}: no InstallationProject {row.entity_id}")
            continue
        if installation_project.procurement_order_reference:
            continue
        processed += 1
        if apply_purchase_order_response(installation_project, row.erp_response):
            repaired += 1
            db.commit()

    result["processed"] = processed
    result["repaired"] = repaired
    return result


def run_repair_purchase_order_writebacks() -> dict:
    """Own the background session for PO write-back repair (beat entry point)."""
    from app.db import task_session

    with task_session() as db:
        return repair_purchase_order_writebacks(db)
