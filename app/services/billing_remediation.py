"""Finance remediation executor for billing-integrity violations.

Executes finance's APPROVED dispositions from the #287 worklist against invoice
lines. This is the most dangerous tool in the post-cutover hardening — every
guard below is load-bearing. See docs/POST_CUTOVER_BILLING_VIOLATIONS.md.

Hard rules (enforced here, tested in tests/test_billing_remediation.py):
  - Never DELETE a row (void = is_active=False; credit = a CreditNote record).
  - Never mutate a row not present in the approved CSV.
  - Never credit an unpaid line; never void a paid line (use the credit path).
  - Never process a row whose amount/status no longer matches the approved
    snapshot (state changed since approval) or since the dry-run manifest.

Actions: ``void_unpaid_line`` | ``credit_paid_line`` | ``mark_valid_historical``.
``mark_valid_historical`` is a record-only no-op. Dry-run is the default;
``apply_remediation`` only writes when ``dry_run=False``.
"""

from __future__ import annotations

import csv
import logging
from decimal import Decimal, InvalidOperation
from typing import Any

from app.models.billing import Invoice, InvoiceLine
from app.schemas.billing import CreditNoteIssuePreviewRequest
from app.services.billing._common import _recalculate_invoice_totals
from app.services.billing.credit_notes import CreditNotes
from app.services.common import coerce_uuid

logger = logging.getLogger(__name__)

ACTIONS = ("void_unpaid_line", "credit_paid_line", "mark_valid_historical")
_WRITE_ACTIONS = ("void_unpaid_line", "credit_paid_line")
_REQUIRED_COLS = ("invoice_line_id", "action")
# Snapshot columns compared against live state to refuse if anything drifted
# since finance approved the row.
_SNAPSHOT_COLS = ("line_amount", "invoice_status", "invoice_balance_due")


def _money(x: Any) -> Decimal:
    try:
        return Decimal(str(x if x not in (None, "") else "0")).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return Decimal("NaN")


def load_disposition_csv(path: str) -> list[dict[str, str]]:
    with open(path, newline="") as fh:
        reader = csv.DictReader(fh)
        missing = [
            c
            for c in (*_REQUIRED_COLS, *_SNAPSHOT_COLS)
            if c not in (reader.fieldnames or [])
        ]
        if missing:
            raise ValueError(f"approved CSV missing required columns: {missing}")
        return [dict(row) for row in reader]


def _amount_paid(invoice: Invoice) -> Decimal:
    return _money(invoice.total) - _money(invoice.balance_due)


def _refuse(line_id: str, action: str, reason: str) -> dict[str, Any]:
    return {
        "invoice_line_id": line_id,
        "action": action,
        "decision": "refuse",
        "reason": reason,
    }


def plan_row(db, row: dict[str, str]) -> dict[str, Any]:
    """Validate one approved row against live state. Returns a plan item with
    decision ``apply`` | ``skip`` | ``refuse``. NEVER writes."""
    line_id = (row.get("invoice_line_id") or "").strip()
    action = (row.get("action") or "").strip()
    if action not in ACTIONS:
        return _refuse(line_id, action, "unknown_action")
    if not line_id:
        return _refuse(line_id, action, "missing_invoice_line_id")

    try:
        line = db.get(InvoiceLine, coerce_uuid(line_id))
    except Exception:
        return _refuse(line_id, action, "bad_invoice_line_id")
    if line is None or not line.is_active:
        return _refuse(line_id, action, "line_missing_or_inactive")
    invoice = db.get(Invoice, line.invoice_id)
    if invoice is None or not invoice.is_active:
        return _refuse(line_id, action, "invoice_missing_or_inactive")

    # State must still match the approved snapshot.
    if _money(line.amount) != _money(row.get("line_amount")):
        return _refuse(line_id, action, "line_amount_changed")
    if str(getattr(invoice.status, "value", invoice.status)) != (
        row.get("invoice_status") or ""
    ):
        return _refuse(line_id, action, "invoice_status_changed")
    if _money(invoice.balance_due) != _money(row.get("invoice_balance_due")):
        return _refuse(line_id, action, "invoice_balance_changed")

    paid = _amount_paid(invoice)
    before = {
        "line_is_active": True,
        "line_amount": str(_money(line.amount)),
        "invoice_id": str(invoice.id),
        "invoice_status": str(getattr(invoice.status, "value", invoice.status)),
        "invoice_total": str(_money(invoice.total)),
        "invoice_balance_due": str(_money(invoice.balance_due)),
        "amount_paid": str(paid),
    }

    if action == "mark_valid_historical":
        return {
            "invoice_line_id": line_id,
            "action": action,
            "decision": "skip",
            "reason": "valid_historical_no_change",
            "before": before,
        }
    if action == "void_unpaid_line":
        if paid > 0:
            return _refuse(line_id, action, "invoice_paid_use_credit")
        return {
            "invoice_line_id": line_id,
            "action": action,
            "decision": "apply",
            "before": before,
        }
    if action == "credit_paid_line":
        if paid <= 0:
            return _refuse(line_id, action, "invoice_unpaid_use_void")
        return {
            "invoice_line_id": line_id,
            "action": action,
            "decision": "apply",
            "before": before,
        }
    return _refuse(line_id, action, "unhandled_action")  # unreachable


def plan_remediation(db, rows: list[dict[str, str]]) -> dict[str, Any]:
    items = [plan_row(db, r) for r in rows]
    from collections import Counter

    by_decision = Counter(i["decision"] for i in items)
    by_action = Counter(i["action"] for i in items if i["decision"] == "apply")
    return {
        "items": items,
        "counts": {
            "apply": by_decision.get("apply", 0),
            "skip": by_decision.get("skip", 0),
            "refuse": by_decision.get("refuse", 0),
            "by_action": dict(by_action),
        },
    }


def apply_remediation(
    db, plan: dict[str, Any], *, dry_run: bool = True
) -> dict[str, Any]:
    """Execute the ``apply`` items. DRY-RUN by default. Commits per item so a
    mid-run failure leaves prior, already-verified work durable. Returns a
    manifest with exact before/after state per applied item (the rollback set)."""
    applied: list[dict[str, Any]] = []
    errors = 0
    for item in plan["items"]:
        if item["decision"] != "apply":
            continue
        if dry_run:
            applied.append({**item, "applied": False})
            continue
        try:
            after = _execute(db, item)
            db.commit()
            applied.append({**item, "applied": True, "after": after})
        except Exception:
            db.rollback()
            errors += 1
            logger.exception(
                "remediation failed for line %s (%s)",
                item["invoice_line_id"],
                item["action"],
            )
            applied.append({**item, "applied": False, "error": True})
    return {
        "dry_run": dry_run,
        "applied": applied,
        "errors": errors,
        "applied_count": sum(1 for a in applied if a.get("applied")),
    }


def _execute(db, item: dict[str, Any]) -> dict[str, Any]:
    line = db.get(InvoiceLine, coerce_uuid(item["invoice_line_id"]))
    invoice = db.get(Invoice, line.invoice_id)
    if item["action"] == "void_unpaid_line":
        line.is_active = False
        _recalculate_invoice_totals(db, invoice)
        return {
            "line_is_active": False,
            "invoice_total": str(_money(invoice.total)),
            "invoice_balance_due": str(_money(invoice.balance_due)),
        }
    if item["action"] == "credit_paid_line":
        amount = _money(line.amount)
        cn = CreditNotes.issue_system(
            db,
            CreditNoteIssuePreviewRequest(
                account_id=invoice.account_id,
                invoice_id=invoice.id,
                subtotal=amount,
                total=amount,
                currency=invoice.currency or "NGN",
                memo=f"Billing-integrity correction for line {line.id}",
                line_description=f"Billing-integrity correction for line {line.id}",
            ),
            idempotency_key=f"billing-remediation-credit-{line.id}",
            commit=False,
            # Remediation must stay reversible: rollback_remediation undoes this
            # by voiding the note, and a note cannot be voided once applied —
            # there is no un-apply path. So this credit is held deliberately
            # rather than settling the invoice at issue, and the rollback
            # contract keeps working.
            apply_on_issue=False,
        ).credit_note
        return {"credit_note_id": str(cn.id), "credit_amount": str(amount)}
    raise ValueError(f"non-write action reached _execute: {item['action']}")


def rollback_remediation(db, manifest: dict[str, Any]) -> dict[str, Any]:
    """Reverse ONLY what the apply manifest recorded: reactivate voided lines
    (and recalc), void created credit notes. Idempotent."""
    reversed_count = 0
    for item in manifest.get("applied", []):
        if not item.get("applied"):
            continue
        try:
            if item["action"] == "void_unpaid_line":
                line = db.get(InvoiceLine, coerce_uuid(item["invoice_line_id"]))
                if line is not None and not line.is_active:
                    line.is_active = True
                    _recalculate_invoice_totals(db, db.get(Invoice, line.invoice_id))
                    reversed_count += 1
            elif item["action"] == "credit_paid_line":
                cn_id = (item.get("after") or {}).get("credit_note_id")
                if cn_id:
                    CreditNotes.void_system(
                        db,
                        cn_id,
                        idempotency_key=f"billing-remediation-void-{cn_id}",
                        memo="Rollback approved billing remediation",
                        commit=False,
                    )
                    reversed_count += 1
            db.commit()
        except Exception:
            db.rollback()
            logger.exception("rollback failed for %s", item.get("invoice_line_id"))
    return {"reversed": reversed_count}
