"""Expense-claim flow for the Sub → DotMac ERP outbox.

This is the first real money flow to move onto sub's ``field_erp_sync_events``
outbox. It ports ``dotmac_crm/app/services/dotmac_erp/expense_request_sync.py``
onto sub's native ``FieldExpenseRequest`` (which already carries the ERP mirror
fields ``expense_claim_reference`` / ``expense_claim_number`` / ``expense_claim_status``).

Three responsibilities live here:

* **map + release** — manager approval builds a versioned claim-and-receipt
  payload and hands it to ``outbox.enqueue``. Submission remains local and
  creates no ERP event. The worker owns delivery, and the outbox refuses any
  flow Sub does not own in ``sync_flow_ownership``.
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
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy.orm import Session, selectinload

from app.models.field_erp_sync import (
    FieldErpSyncEvent,
    FieldErpSyncFlow,
    FieldErpSyncStatus,
    flow_owned_by_sub,
)
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


class ExpenseErpAction(StrEnum):
    SUBMIT = "submit"
    APPROVE = "approve"
    REJECT = "reject"
    RELEASE_APPROVED = "release_approved_v2"
    SUBMIT_V3 = "expense_submit_v3"
    APPROVE_V3 = "expense_approve_v3"
    REJECT_V3 = "expense_reject_v3"
    INITIATE_PAYMENT = "initiate_payment"


# The sub-side statuses a claim can still change while ERP owns settlement;
# only these get polled for a status refresh.
_IN_FLIGHT_STATUSES = ("submitted", "approved")

# ERP claim statuses that map onto sub FieldExpenseRequest statuses. Anything not
# listed (draft/submitted/pending_approval) leaves the sub row where it is. Ported
# from CRM's ``_ERP_TERMINAL_STATUS_MAP``.
# ---------------------------------------------------------------------------
# Mapping + idempotency key (verbatim port of CRM's _map_expense_request)
# ---------------------------------------------------------------------------


def expense_release_idempotency_key(request: FieldExpenseRequest) -> str:
    return f"exp-{request.id}-approved-release-v2"


def expense_submission_idempotency_key(request: FieldExpenseRequest) -> str:
    return f"exp-{request.id}-submitted-v3"


def expense_decision_idempotency_key(
    request: FieldExpenseRequest, action: ExpenseErpAction, decision_id: UUID
) -> str:
    if action not in {ExpenseErpAction.APPROVE_V3, ExpenseErpAction.REJECT_V3}:
        raise ValueError("A v3 approval or rejection action is required")
    verb = "approved" if action is ExpenseErpAction.APPROVE_V3 else "rejected"
    return f"exp-{request.id}-{verb}-{decision_id}-v3"


@dataclass(frozen=True, slots=True)
class ExpenseDeliveryIdentity:
    request_id: UUID
    client_ref: UUID
    source_claim_id: UUID


def require_expense_delivery_identity(
    request: FieldExpenseRequest, *, source_claim_id: UUID | None = None
) -> ExpenseDeliveryIdentity:
    """Fail closed when a token-bearing request is not one UUID end to end."""
    effective_source_id = source_claim_id or request.id
    if request.client_ref is None:
        raise ValueError("Expense delivery identity is incomplete")
    if request.payment_destination_token and (
        request.client_ref != request.id or effective_source_id != request.id
    ):
        raise ValueError("Payment destination token is bound to another expense")
    return ExpenseDeliveryIdentity(
        request_id=request.id,
        client_ref=request.client_ref,
        source_claim_id=effective_source_id,
    )


def expense_payment_idempotency_key(
    request: FieldExpenseRequest, command_id: UUID
) -> str:
    return f"exp-{request.id}-pay-{command_id}-v1"


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
            "source_line_id": str(item.id),
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
        "_expense_action": ExpenseErpAction.SUBMIT.value,
        "source_claim_id": str(request.id),
        "purpose": request.purpose,
        "claim_date": claim_date,
        "requested_by_email": _requester_email(request),
        "requested_approver_id": (
            str(request.selected_approver_erp_id)
            if request.selected_approver_erp_id
            else None
        ),
        "payment_destination_token": request.payment_destination_token,
        "ticket_source_reference": getattr(mirror, "crm_ticket_id", None),
        "project_source_reference": getattr(mirror, "crm_project_id", None),
        "currency_code": request.currency,
        "remarks": request.notes or "",
        "reference_number": reference_number[:50] if reference_number else None,
        "items": item_rows,
    }


def build_expense_submission_payload(request: FieldExpenseRequest) -> dict:
    require_expense_delivery_identity(request)
    payload = build_expense_claim_payload(request)
    payload.update(
        {
            "_expense_action": ExpenseErpAction.SUBMIT_V3.value,
            "_expense_contract_version": "work-order-expense.v3",
            "_receipt_attachments": [
                {
                    "source_line_id": str(item.id),
                    "source_attachment_id": str(item.receipt_attachment_id),
                }
                for item in request.items
                if item.receipt_attachment_id is not None
            ],
        }
    )
    return payload


def build_approved_expense_release_payload(
    request: FieldExpenseRequest,
    *,
    decision_id: UUID,
    decided_by_email: str,
    decided_at: datetime,
    notes: str | None = None,
) -> dict:
    payload = build_expense_claim_payload(request)
    payload["_expense_action"] = ExpenseErpAction.RELEASE_APPROVED.value
    payload["_expense_contract_version"] = "work-order-expense.v2"
    payload["_receipt_attachments"] = [
        {
            "source_line_id": str(item.id),
            "source_attachment_id": str(item.receipt_attachment_id),
        }
        for item in request.items
        if item.receipt_attachment_id is not None
    ]
    payload["_approval"] = {
        "decision_id": str(decision_id),
        "decided_by_email": decided_by_email,
        "decided_at": decided_at.isoformat(),
        **({"notes": notes} if notes else {}),
    }
    return payload


def expense_claim_eligibility_error(request: FieldExpenseRequest) -> str | None:
    """Return a reason string if the request is NOT eligible for ERP sync, else None.

    Manager-approved expenses alone cross the ERP boundary. A claim must also
    have at least one line and a requester email so ERP can match the employee.
    """
    if request.status not in {"approved", "paid"} or request.approved_at is None:
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
# Enqueue (manager approval release point plus ordered payment action)
# ---------------------------------------------------------------------------


def enqueue_expense_decision(
    db: Session,
    request: FieldExpenseRequest,
    *,
    action: ExpenseErpAction,
    decision_id: UUID,
    decided_by_email: str,
    decided_at: datetime,
    reason: str | None = None,
    notes: str | None = None,
    isolate: bool = False,
) -> FieldErpSyncEvent:
    if action not in {ExpenseErpAction.APPROVE_V3, ExpenseErpAction.REJECT_V3}:
        raise ValueError("Only a v3 manager decision may be staged here")
    require_expense_delivery_identity(request)
    expected_status = (
        "approved" if action is ExpenseErpAction.APPROVE_V3 else "rejected"
    )
    if request.status != expected_status:
        raise ValueError(f"Only a {expected_status} expense may stage this decision")
    decision: dict[str, object] = {
        "decision_id": str(decision_id),
        "decided_by_email": decided_by_email,
        "decided_at": decided_at.isoformat(),
        **({"notes": notes} if notes else {}),
    }
    if action is ExpenseErpAction.REJECT_V3:
        if not reason:
            raise ValueError("A rejection reason is required")
        decision["reason"] = reason
    return outbox.enqueue(
        db,
        flow=FieldErpSyncFlow.expense_claim,
        entity_type=ENTITY_TYPE,
        entity_id=request.id,
        idempotency_key=expense_decision_idempotency_key(request, action, decision_id),
        payload={
            "_expense_action": action.value,
            "_expense_contract_version": "work-order-expense.v3",
            "_depends_on_idempotency_key": expense_submission_idempotency_key(request),
            "source_claim_id": str(request.id),
            **decision,
        },
        isolate=isolate,
    )


def enqueue_expense_submission(
    db: Session, request: FieldExpenseRequest, *, isolate: bool = False
) -> FieldErpSyncEvent:
    require_expense_delivery_identity(request)
    if request.status != "submitted" or request.submitted_at is None:
        raise ValueError("Only a submitted expense may be sent to ERP")
    if not request.items or not _requester_email(request):
        raise ValueError("Submitted expense delivery evidence is incomplete")
    return outbox.enqueue(
        db,
        flow=FieldErpSyncFlow.expense_claim,
        entity_type=ENTITY_TYPE,
        entity_id=request.id,
        idempotency_key=expense_submission_idempotency_key(request),
        payload=build_expense_submission_payload(request),
        isolate=isolate,
    )


def enqueue_expense_payment(
    db: Session,
    request: FieldExpenseRequest,
    *,
    command_id: UUID,
    initiated_by_email: str,
    initiated_at: datetime,
    isolate: bool = False,
) -> FieldErpSyncEvent:
    approval_event = (
        db.query(FieldErpSyncEvent)
        .filter(
            FieldErpSyncEvent.flow == FieldErpSyncFlow.expense_claim.value,
            FieldErpSyncEvent.entity_id == request.id,
        )
        .order_by(FieldErpSyncEvent.created_at.desc())
        .all()
    )
    approval_dependency = next(
        (
            event.idempotency_key
            for event in approval_event
            if str((event.payload or {}).get("_expense_action"))
            in {"expense_approve_v3", "release_approved_v2"}
        ),
        expense_release_idempotency_key(request),
    )
    payload: dict[str, object] = {
        "_expense_action": ExpenseErpAction.INITIATE_PAYMENT.value,
        "_depends_on_idempotency_key": approval_dependency,
        "command_id": str(command_id),
        "initiated_by_email": initiated_by_email,
        "initiated_at": initiated_at.isoformat(),
    }
    return outbox.enqueue(
        db,
        flow=FieldErpSyncFlow.expense_claim,
        entity_type="field_expense_payment",
        entity_id=request.id,
        idempotency_key=expense_payment_idempotency_key(request, command_id),
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
    if erp_id and request.payment_destination_token:
        # ERP has persisted the encrypted destination snapshot. Retain immutable
        # outbox evidence, but remove the no-longer-needed token from the source row.
        request.payment_destination_token = None
    if claim_number:
        request.expense_claim_number = str(claim_number)[:60]
    approver_id = response.get("requested_approver_id")
    if approver_id:
        try:
            request.selected_approver_erp_id = UUID(str(approver_id))
        except ValueError:
            pass
    approver_name = response.get("requested_approver_name")
    if approver_name:
        request.selected_approver_name = str(approver_name)[:200]
    destination_mode = response.get("payment_destination_mode")
    if destination_mode in {"erp_profile", "expense_override"}:
        request.payment_destination_mode = str(destination_mode)
    bank_name = response.get("recipient_bank_name")
    if bank_name:
        request.recipient_bank_name = str(bank_name)[:100]
    masked_account = response.get("masked_account_number")
    if masked_account:
        request.recipient_account_last4 = str(masked_account)[-4:]
    beneficiary = response.get("verified_beneficiary_name")
    if beneficiary:
        request.verified_beneficiary_name = str(beneficiary)[:150]
    _apply_payment_projection(request, response)
    if not claim_status:
        return

    request.expense_claim_status = claim_status
    now = datetime.now(UTC)
    if claim_status == "paid" and request.status == "approved":
        request.paid_at = request.paid_at or now
        request.status = "paid"


def _apply_payment_projection(
    request: FieldExpenseRequest, response: dict[str, object]
) -> None:
    raw_status = response.get("payment_status")
    raw_intent_id = response.get("payment_intent_id")
    if not raw_status and not raw_intent_id:
        return
    metadata = dict(request.metadata_ or {})
    current = dict(metadata.get("erp_payment") or {})
    if raw_status:
        current["status"] = str(raw_status).strip().lower()[:40]
    if raw_intent_id:
        current["intent_id"] = str(raw_intent_id)[:120]
    current["updated_at"] = datetime.now(UTC).isoformat()
    current.pop("error", None)
    metadata["erp_payment"] = current
    request.metadata_ = metadata


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


def _poll_unlinked_expense_claims(
    db: Session,
    *,
    client: DotMacERPClient | ErpCapabilityClient,
    limit: int,
) -> tuple[int, int, int, list[str]]:
    """Poll ``sent``/``accepted`` outbox rows whose request never got a reference.

    A ``sent`` row was never eligible for the reference-gated query below (it
    has no reference BY DEFINITION — that's the dead end this closes). An
    ``accepted`` row can also land here if the same-transaction write-back
    failed after delivery. Keyed on Sub's own request id, same as the linked
    poll below — see ``client.get_expense_claim_status``'s docstring.

    OWNERSHIP GUARD: ``flow_owned_by_sub`` is checked once up front, since
    ownership is a per-flow switch, not per-row. A status poll is a real ERP
    API call about a row that may belong to a flow ownership has since moved
    back to CRM — skipped, not polled, when not owned. Skipped rows are
    counted separately from ``processed``/``updated`` so the caller's own
    sweep numbers stay honest.
    """
    processed = 0
    updated = 0
    skipped_not_owned = 0
    errors: list[str] = []
    owned = flow_owned_by_sub(db, FieldErpSyncFlow.expense_claim)
    for row in outbox.unlinked_delivered_events(
        db, flow=FieldErpSyncFlow.expense_claim, limit=limit
    ):
        request = db.get(FieldExpenseRequest, row.entity_id)
        if request is None or request.expense_claim_reference:
            continue
        if not owned:
            skipped_not_owned += 1
            logger.info(
                "expense_sync: skipping unlinked status poll for %s — sub does "
                "not own flow 'expense_claim' (sync_flow_ownership)",
                row.id,
            )
            continue
        processed += 1
        try:
            response = client.get_expense_claim_status(str(request.id))
        except Exception as exc:  # noqa: BLE001 — one bad claim can't stall the batch
            db.rollback()
            errors.append(f"{row.id}: {exc}")
            logger.warning(
                "expense_sync: unlinked status poll failed for %s: %s", row.id, exc
            )
            continue
        if not response:
            continue
        outbox.record_polled_outcome(db, row, response)
        db.commit()
        if request.expense_claim_reference:
            updated += 1
    return processed, updated, skipped_not_owned, errors


def refresh_expense_claim_statuses(
    db: Session,
    *,
    client: DotMacERPClient | ErpCapabilityClient | None = None,
    limit: int = 100,
) -> dict:
    """Poll ERP for in-flight expense claims and refresh their mirror fields.

    Two candidate sets, both keyed by Sub's own request id (never the ERP id):

    1. Already-linked requests (``expense_claim_reference`` set) still awaiting
       an ERP decision (``submitted`` / ``approved``) — the historical
       behaviour, ported from CRM's
       ``refresh_pending_expense_request_erp_statuses``.
    2. Delivered-but-unlinked outbox rows (``sent``/``accepted`` with no
       reference yet) — the dead end where a ``sent`` row could never satisfy
       set 1's ``.isnot(None)`` filter since it never carries a reference by
       construction. Routed through ``outbox.record_polled_outcome`` so the
       response is classified and written back through the same path a fresh
       delivery uses.

    Read-only against ERP; idempotent; safe to re-run.
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

    owned_client = client
    created_client = False
    if owned_client is None:
        owned_client = capability_client(db)
        created_client = True

    processed = 0
    updated = 0
    skipped_not_owned = 0
    try:
        (
            unlinked_processed,
            unlinked_updated,
            unlinked_skipped_not_owned,
            unlinked_errors,
        ) = _poll_unlinked_expense_claims(db, client=owned_client, limit=limit)
        processed += unlinked_processed
        updated += unlinked_updated
        skipped_not_owned += unlinked_skipped_not_owned
        errors.extend(unlinked_errors)

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
    result["skipped_not_owned"] = skipped_not_owned
    return result


def repair_expense_claim_writebacks(db: Session, *, limit: int = 100) -> dict:
    """Repair a delivered expense-claim write-back that never landed on the request.

    Mirrors ``purchase_order_sync.repair_purchase_order_writebacks``'s shape and
    rationale: ``outbox._dispatch_flow_writeback`` catches and logs an
    ``apply_erp_response`` failure so a delivery attempt is never failed by a
    projection bug, but that can leave a terminal (``accepted``/``sent``)
    outbox row whose ``FieldExpenseRequest.expense_claim_reference`` never got
    set. No new ERP call: this re-applies the response ALREADY stored on the
    delivered row.

    Restricted to ``status IN ('accepted', 'sent')`` so a ``rejected``/``dead``
    row's response is never written back as if ERP had accepted it.

    SAFE-SCOPE NOTE (this function is intentionally NOT wired into the Celery
    beat schedule — see the task registry / scheduler config, unchanged by
    this change): ``docs/runbooks/EXPENSE_CLAIM_ERP_CUTOVER.md`` prohibits
    backfilling ERP delivery for expenses that were approved before a flow's
    cutover to Sub. A row can only exist in ``field_erp_sync_events`` with
    status ``sent``/``accepted`` if ``outbox.deliver_pending`` actually posted
    it while sub owned the flow at THAT time — so no candidate row here is a
    pre-cutover historical row the runbook's "no backfill" prohibition is
    about. But ownership is not a one-way gate: it can move back to CRM after
    delivery (the cutover/shadow-phase model this codebase uses), and this is
    a repair a schedule could leave running indefinitely — so it does NOT
    infer current ownership from a row's past delivery. See the OWNERSHIP
    GUARD note below. Wiring the scheduler call itself is still left to
    Michael's explicit confirmation rather than resolved here, since the
    runbook's prohibition is a data-safety rule this change does not own.

    OWNERSHIP GUARD: ``flow_owned_by_sub`` is checked once up front (ownership
    is a per-flow switch, not per-row). Re-applying a stored response is a
    state mutation implying ERP involvement — skipped, not repaired, for
    every row when sub does not currently own this flow, and counted under
    ``skipped_not_owned`` so this sweep's own numbers stay honest.
    """
    limit = max(1, min(int(limit or 100), 500))
    owned = flow_owned_by_sub(db, FieldErpSyncFlow.expense_claim)
    rows = (
        db.query(FieldErpSyncEvent)
        .filter(FieldErpSyncEvent.flow == FieldErpSyncFlow.expense_claim.value)
        .filter(
            FieldErpSyncEvent.status.in_(
                (FieldErpSyncStatus.accepted.value, FieldErpSyncStatus.sent.value)
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

    processed = 0
    repaired = 0
    skipped_not_owned = 0
    if not owned:
        logger.info(
            "expense_sync: skipping write-back repair — sub does not own flow "
            "'expense_claim' (sync_flow_ownership)"
        )
        result["skipped_not_owned"] = len(rows)
        return result

    for row in rows:
        erp_id = _extract_claim_id(row.erp_response)
        if not erp_id:
            continue
        request = db.get(FieldExpenseRequest, row.entity_id)
        if request is None:
            errors.append(f"{row.id}: no FieldExpenseRequest {row.entity_id}")
            continue
        if request.expense_claim_reference:
            continue
        processed += 1
        apply_claim_response(request, row.erp_response)
        if request.expense_claim_reference:
            repaired += 1
            db.commit()

    result["processed"] = processed
    result["repaired"] = repaired
    result["skipped_not_owned"] = skipped_not_owned
    return result


def run_repair_expense_claim_writebacks() -> dict[str, object]:
    """Own the background session for expense-claim write-back repair.

    Provided so the repair is one call away from being scheduled. NOT
    registered in ``app/tasks/dotmac_erp_outbox.py`` /
    ``app/services/task_reliability.py`` / ``app/services/scheduler_config.py``
    — see ``repair_expense_claim_writebacks``'s docstring for why wiring this
    into the beat schedule is left as an explicit decision.
    """
    from app.db import task_session

    with task_session() as db:
        return repair_expense_claim_writebacks(db)


def run_refresh_expense_claim_statuses() -> dict[str, object]:
    """Own the background session for expense-claim reconciliation."""
    from app.db import task_session

    with task_session() as db:
        return refresh_expense_claim_statuses(db)
