"""Expense-claim flow for the Sub → DotMac ERP outbox.

This is the first real money flow to move onto sub's ``field_erp_sync_events``
outbox. It ports ``dotmac_crm/app/services/dotmac_erp/expense_request_sync.py``
onto sub's native ``FieldExpenseRequest`` (which already carries the ERP mirror
fields ``expense_claim_reference`` / ``expense_claim_number`` / ``expense_claim_status``).

Three responsibilities live here:

* **map + enqueue** — ``enqueue_expense_claim`` builds the ERP payload (a verbatim
  port of CRM's ``_map_expense_request`` shape), computes the stable idempotency
  key ``exp-{id}-submit-v1``, and hands it to ``outbox.enqueue``. It does NOT
  deliver — the worker owns delivery, and the outbox refuses any flow sub does
  not own in ``sync_flow_ownership``. So enqueuing is inert until cutover.
* **write-back** — ``apply_erp_response`` runs on the outbox's accepted/rejected
  path and writes the ERP claim id / number / status back onto the source row
  (the "dropped money link" mitigation from the review doc).
* **reconcile** — ``refresh_expense_claim_statuses`` polls ERP for in-flight
  claims and refreshes the mirror fields (ports CRM's status-poll refresh).

INERT UNTIL CUTOVER: nothing here sends. Manager approval stages a durable event
only when ``sync_flow_ownership.expense_claim`` belongs to Sub (seeded ``crm``).
The worker resolves the ERP capability when it delivers that event. Ownership
must move to Sub at cutover before a single claim reaches ERP.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy.orm import Session, selectinload

from app.models.field_erp_sync import FieldErpSyncEvent, FieldErpSyncFlow
from app.models.field_expense import FieldExpenseRequest
from app.services.dotmac_erp import outbox
from app.services.dotmac_erp.client import DotMacERPClient
from app.services.integrations.erp_capability import (
    ErpCapabilityClient,
    capability_client,
)

logger = logging.getLogger(__name__)

ENTITY_TYPE = "field_expense_request"
PROVIDER = "dotmac_erp"

# The sub-side statuses a claim can still change while ERP owns approval/payment;
# only these get polled for a status refresh.
_IN_FLIGHT_STATUSES = ("submitted", "approved")

# ERP claim statuses that map onto sub FieldExpenseRequest statuses. Anything not
# listed (draft/submitted/pending_approval) leaves the sub row where it is. Ported
# from CRM's ``_ERP_TERMINAL_STATUS_MAP``.
_ERP_TERMINAL_STATUS_MAP = {
    "approved": "approved",
    "rejected": "rejected",
    "paid": "paid",
    "cancelled": "canceled",
    "canceled": "canceled",
}


# ---------------------------------------------------------------------------
# Mapping + idempotency key (verbatim port of CRM's _map_expense_request)
# ---------------------------------------------------------------------------


def expense_claim_idempotency_key(request: FieldExpenseRequest) -> str:
    """Stable per-request key: ``exp-{id}-submit-v1``.

    The historical ``submit-v1`` spelling is retained to deduplicate any row
    staged by an older deployment. Approval replays therefore return the same
    outbox row instead of creating a second ERP claim.
    """
    return f"exp-{request.id}-submit-v1"


def _requester_email(request: FieldExpenseRequest) -> str | None:
    """Resolve the requesting employee's email (ERP matches employees by email)."""
    user = request.requested_by_system_user
    email = (getattr(user, "email", None) or "").strip()
    return email or None


def build_expense_claim_payload(request: FieldExpenseRequest) -> dict:
    """Map a ``FieldExpenseRequest`` to ERP's ``SubExpenseClaimPayload`` shape.

    Ports the historical mapper into a neutral contract: ``source_claim_id`` is
    Sub's request UUID,
    amounts stringified, dates ISO-formatted, and each line carries
    ``category_code`` / ``claimed_amount`` / ``expense_date``. Fidelity notes vs
    CRM are documented in the ERP ownership contract — chiefly that ticket/project ids come from the
    work-order provenance and ``reference_number`` from the retained imported
    expense-request reference (Sub has no native expense number).
    """
    item_rows: list[dict[str, object]] = []
    for item in request.items:
        row: dict[str, object] = {
            "category_code": item.category_code,
            "description": item.description,
            "claimed_amount": str(item.amount),
            "expense_date": (
                item.expense_date or request.expense_date or request.created_at.date()
            ).isoformat(),
        }
        if item.vendor_name:
            row["vendor_name"] = item.vendor_name
        if item.receipt_url:
            row["receipt_url"] = item.receipt_url
        if item.notes:
            row["notes"] = item.notes
        item_rows.append(row)

    claim_date = (
        request.expense_date or (request.submitted_at or request.created_at).date()
    ).isoformat()

    mirror = request.work_order_mirror
    reference_number = request.crm_expense_request_id or None

    return {
        "source_claim_id": str(request.id),
        "purpose": request.purpose,
        "claim_date": claim_date,
        "requested_by_email": _requester_email(request),
        "ticket_source_reference": getattr(mirror, "crm_ticket_id", None),
        "project_source_reference": getattr(mirror, "crm_project_id", None),
        "currency_code": request.currency,
        "remarks": request.notes or "",
        "reference_number": reference_number[:50] if reference_number else None,
        "items": item_rows,
    }


def expense_claim_eligibility_error(request: FieldExpenseRequest) -> str | None:
    """Return a reason string if the request is NOT eligible for ERP sync, else None.

    Only locally approved expenses may cross the ERP delivery boundary. A claim
    must also have at least one line and a requester email so ERP can match the
    employee.
    """
    if request.status != "approved":
        return (
            f"Expense request {request.id} is in {request.status} status and "
            "cannot be synced"
        )
    if not request.items:
        return f"Expense request {request.id} has no lines — cannot sync to ERP"
    if not _requester_email(request):
        return "Requester has no email address; ERP needs it to match the employee"
    return None


# ---------------------------------------------------------------------------
# Enqueue (manager-approval release point)
# ---------------------------------------------------------------------------


def enqueue_expense_claim(
    db: Session, request: FieldExpenseRequest, *, isolate: bool = True
) -> FieldErpSyncEvent | None:
    """Enqueue the expense-claim outbox intent for an approved request.

    Validates eligibility, builds the payload + stable key, and calls
    ``outbox.enqueue`` (idempotent on the key). Returns the outbox row, or ``None``
    when the request is not eligible (logged, not raised — an ineligible request
    must never break the submit transaction). Does NOT deliver.
    """
    reason = expense_claim_eligibility_error(request)
    if reason:
        logger.info(
            "expense_sync: not enqueuing expense request %s — %s", request.id, reason
        )
        return None

    payload = build_expense_claim_payload(request)
    return outbox.enqueue(
        db,
        flow=FieldErpSyncFlow.expense_claim,
        entity_type=ENTITY_TYPE,
        entity_id=request.id,
        idempotency_key=expense_claim_idempotency_key(request),
        payload=payload,
        isolate=isolate,
    )


# ---------------------------------------------------------------------------
# Response write-back (outbox accepted/rejected path + status reconcile)
# ---------------------------------------------------------------------------


def _extract_claim_id(response: dict | None) -> str | None:
    if not isinstance(response, dict):
        return None
    erp_id = (
        response.get("claim_id")
        or response.get("expense_claim_id")
        or response.get("claim_number")
    )
    return str(erp_id) if erp_id else None


def _extract_claim_status(response: dict | None) -> str | None:
    if not isinstance(response, dict):
        return None
    raw = response.get("claim_status") or response.get("status")
    if not raw:
        return None
    status = str(raw).strip().lower().replace("-", "_").replace(" ", "_")
    return status[:40] if status else None


def apply_claim_response(request: FieldExpenseRequest, response: dict | None) -> None:
    """Write an ERP claim response back onto a ``FieldExpenseRequest``.

    Shared by the outbox accepted/rejected path and the status reconcile. Ports
    CRM's ``_apply_erp_response``: records claim id / number / status and, when
    ERP has made a terminal decision, mirrors it onto the sub row's status +
    timestamps. Idempotent — safe to run on every poll.
    """
    if not isinstance(response, dict):
        return

    erp_id = _extract_claim_id(response)
    claim_number = response.get("claim_number")
    claim_status = _extract_claim_status(response)

    if request.expense_system not in {None, PROVIDER}:
        raise ValueError(
            f"Expense source changed: {request.expense_system} -> {PROVIDER}"
        )
    request.expense_system = PROVIDER
    if erp_id and not request.expense_claim_reference:
        request.expense_claim_reference = str(erp_id)[:120]
    if claim_number:
        request.expense_claim_number = str(claim_number)[:60]
    if not claim_status:
        return

    request.expense_claim_status = claim_status
    mapped = _ERP_TERMINAL_STATUS_MAP.get(claim_status)
    now = datetime.now(UTC)
    if mapped and request.status == "submitted":
        request.status = mapped
        if mapped == "approved":
            request.approved_at = request.approved_at or now
        elif mapped == "rejected":
            request.rejected_at = request.rejected_at or now
            reason = response.get("rejection_reason")
            if reason:
                request.rejection_reason = str(reason)[:500]
        elif mapped == "paid":
            request.approved_at = request.approved_at or now
            request.paid_at = request.paid_at or now
    elif mapped == "paid" and request.status == "approved":
        request.paid_at = request.paid_at or now
        request.status = mapped


def apply_erp_response(db: Session, event: FieldErpSyncEvent) -> None:
    """Outbox write-back hook: apply a delivered event's ERP response to its source.

    Called by ``outbox.deliver_pending`` after a 2xx classify, within the same
    transaction (the outbox commits the row). Loads the ``FieldExpenseRequest``
    the event pushed and applies ``apply_claim_response``. Missing source rows are
    logged, not raised — the event still records its terminal outcome.
    """
    request = db.get(FieldExpenseRequest, event.entity_id)
    if request is None:
        logger.warning(
            "expense_sync: outbox event %s has no FieldExpenseRequest %s to "
            "write ERP response back to",
            event.id,
            event.entity_id,
        )
        return
    apply_claim_response(request, event.erp_response)


# ---------------------------------------------------------------------------
# Status reconcile (beat-driven, gated by dotmac_erp_sync_enabled)
# ---------------------------------------------------------------------------


def refresh_expense_claim_statuses(
    db: Session,
    *,
    client: DotMacERPClient | ErpCapabilityClient | None = None,
    limit: int = 100,
) -> dict:
    """Poll ERP for in-flight expense claims and refresh their mirror fields.

    Selects synced (``expense_claim_reference`` set) requests still awaiting an ERP
    decision (``submitted`` / ``approved``), polls
    ``get_expense_claim_status(request.id)`` for each, and applies the response
    via ``apply_claim_response``. Ports CRM's
    ``refresh_pending_expense_request_erp_statuses``. Read-only against ERP;
    idempotent; safe to re-run.
    """
    limit = max(1, min(int(limit or 100), 200))
    pending = (
        db.query(FieldExpenseRequest)
        .options(selectinload(FieldExpenseRequest.items))
        .filter(FieldExpenseRequest.is_active.is_(True))
        .filter(FieldExpenseRequest.expense_system == PROVIDER)
        .filter(FieldExpenseRequest.expense_claim_reference.isnot(None))
        .filter(FieldExpenseRequest.status.in_(_IN_FLIGHT_STATUSES))
        .order_by(FieldExpenseRequest.updated_at.asc())
        .limit(limit)
        .all()
    )

    errors: list[str] = []
    result: dict[str, object] = {"processed": 0, "updated": 0, "errors": errors}
    if not pending:
        return result

    owned_client = client
    created_client = False
    if owned_client is None:
        owned_client = capability_client(db)
        created_client = True

    processed = 0
    updated = 0
    try:
        for request in pending:
            processed += 1
            try:
                response = owned_client.get_expense_claim_status(str(request.id))
            except Exception as exc:  # noqa: BLE001 — one bad claim can't stall the batch
                db.rollback()
                errors.append(f"{request.id}: {exc}")
                logger.warning(
                    "expense_sync: status refresh failed for %s: %s", request.id, exc
                )
                continue
            if not response:
                continue
            before = request.expense_claim_status
            apply_claim_response(request, response)
            if request.expense_claim_status != before:
                updated += 1
            db.commit()
    finally:
        if created_client:
            owned_client.close()

    result["processed"] = processed
    result["updated"] = updated
    return result


def run_refresh_expense_claim_statuses() -> dict[str, object]:
    """Own the background session for expense-claim reconciliation."""
    from app.db import task_session

    with task_session() as db:
        return refresh_expense_claim_statuses(db)
