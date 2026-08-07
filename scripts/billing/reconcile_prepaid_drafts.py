#!/usr/bin/env python
"""Preview or apply stranded prepaid draft invoice reconciliation.

Preview is the default and writes nothing. Apply is deliberately one existing
invoice or one explicitly named missing-document entity at a time and requires
the exact reviewed fingerprint, idempotency key, actor, and reason. A shortfall,
including NGN 0.50, is never rounded or waived.
"""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from uuid import UUID

from app.services.db_session_adapter import db_session_adapter
from app.services.owner_commands import CommandContext
from app.services.prepaid_draft_reconciliation import (
    AdoptFundedPrepaidProformaCommand,
    CreateReviewedPaidPrepaidInvoiceCommand,
    MissingPaidPrepaidInvoiceRepairQuery,
    PaidPrepaidInvoiceRepairQuery,
    PrepaidDraftReconciliationPreview,
    PrepaidProformaAdoptionQuery,
    ReconcilePrepaidDraftCommand,
    RepairHistoricalPaidPrepaidInvoiceCommand,
    adopt_funded_prepaid_proforma,
    create_reviewed_paid_prepaid_invoice,
    preview_funded_prepaid_proforma_adoption,
    preview_historical_paid_prepaid_invoice_repair,
    preview_missing_paid_prepaid_invoice_repair,
    preview_prepaid_draft_cohort,
    preview_prepaid_draft_reconciliation,
    reconcile_prepaid_draft_invoice,
    repair_historical_paid_prepaid_invoice,
)


def _uuid(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("identifier must be a UUID") from exc


def _timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("timestamp must be ISO 8601") from exc
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("timestamp must include a timezone offset")
    return parsed


def _date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must be YYYY-MM-DD") from exc


def _money(value: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError("money must be a decimal amount") from exc
    if not parsed.is_finite():
        raise argparse.ArgumentTypeError("money must be finite")
    return parsed


def _preview_payload(preview) -> dict[str, object]:
    return {
        "invoice_id": str(preview.invoice_id),
        "account_id": str(preview.account_id),
        "invoice_number": preview.invoice_number,
        "disposition": preview.disposition.value,
        "recommended_action": preview.recommended_action.value,
        "currency": preview.currency,
        "invoice_total": str(preview.invoice_total),
        "balance_due": str(preview.balance_due),
        "payment_backed_credit": str(preview.payment_backed_credit),
        "authoritative_funding": str(preview.authoritative_funding),
        "opening_funding_available": str(preview.opening_funding_available),
        "opening_funding_required": str(preview.opening_funding_required),
        "opening_funding_baseline_id": (
            str(preview.opening_funding_baseline_id)
            if preview.opening_funding_baseline_id
            else None
        ),
        "unbacked_credit": str(preview.unbacked_credit),
        "shortfall": str(preview.shortfall),
        "subscription_ids": [str(value) for value in preview.subscription_ids],
        "entitlement_ids": [str(value) for value in preview.entitlement_ids],
        "renewal_adjustment_ids": [
            str(value) for value in preview.renewal_adjustment_ids
        ],
        "reason": preview.reason,
        "fingerprint": preview.fingerprint,
    }


def _proforma_adoption_preview_payload(preview) -> dict[str, object]:
    return {
        "invoice_id": str(preview.invoice_id),
        "account_id": str(preview.account_id),
        "invoice_number": preview.invoice_number,
        "subscription_id": str(preview.subscription_id),
        "line_id": str(preview.line_id) if preview.line_id else None,
        "settlement_payment_id": (
            str(preview.settlement_payment_id)
            if preview.settlement_payment_id
            else None
        ),
        "settlement_effective_at": (
            preview.settlement_effective_at.isoformat()
            if preview.settlement_effective_at
            else None
        ),
        "billing_period_start": (
            preview.billing_period_start.isoformat()
            if preview.billing_period_start
            else None
        ),
        "billing_period_end": (
            preview.billing_period_end.isoformat()
            if preview.billing_period_end
            else None
        ),
        "disposition": preview.disposition.value,
        "currency": preview.currency,
        "invoice_total": str(preview.invoice_total),
        "payment_backed_credit": str(preview.payment_backed_credit),
        "actionable": preview.actionable,
        "reason": preview.reason,
        "fingerprint": preview.fingerprint,
    }


def _paid_invoice_repair_preview_payload(preview) -> dict[str, object]:
    return {
        "invoice_id": str(preview.invoice_id),
        "account_id": str(preview.account_id),
        "invoice_number": preview.invoice_number,
        "subscription_id": str(preview.subscription_id),
        "line_id": str(preview.line_id) if preview.line_id else None,
        "allocation_id": str(preview.allocation_id) if preview.allocation_id else None,
        "settlement_id": str(preview.settlement_id) if preview.settlement_id else None,
        "payment_id": str(preview.payment_id) if preview.payment_id else None,
        "settlement_effective_at": (
            preview.settlement_effective_at.isoformat()
            if preview.settlement_effective_at
            else None
        ),
        "billing_period_start": (
            preview.billing_period_start.isoformat()
            if preview.billing_period_start
            else None
        ),
        "billing_period_end": (
            preview.billing_period_end.isoformat()
            if preview.billing_period_end
            else None
        ),
        "disposition": preview.disposition.value,
        "currency": preview.currency,
        "invoice_total": str(preview.invoice_total),
        "allocated_amount": str(preview.allocated_amount),
        "actionable": preview.actionable,
        "reason": preview.reason,
        "fingerprint": preview.fingerprint,
    }


def _missing_paid_invoice_preview_payload(preview) -> dict[str, object]:
    return {
        "account_id": str(preview.account_id),
        "subscription_id": str(preview.subscription_id),
        "payment_id": str(preview.payment_id),
        "existing_invoice_id": (
            str(preview.existing_invoice_id) if preview.existing_invoice_id else None
        ),
        "disposition": preview.disposition.value,
        "actionable": preview.actionable,
        "issued_at": preview.issued_at.isoformat() if preview.issued_at else None,
        "paid_at": preview.paid_at.isoformat() if preview.paid_at else None,
        "due_at": preview.due_at.isoformat() if preview.due_at else None,
        "billing_period_start": (
            preview.billing_period_start.isoformat()
            if preview.billing_period_start
            else None
        ),
        "billing_period_end": (
            preview.billing_period_end.isoformat()
            if preview.billing_period_end
            else None
        ),
        "subtotal": str(preview.subtotal),
        "tax_total": str(preview.tax_total),
        "total": str(preview.total),
        "currency": preview.currency,
        "account_credit_before": str(preview.account_credit_before),
        "expected_remaining_credit": str(preview.expected_remaining_credit),
        "selected_payment_available": str(preview.selected_payment_available),
        "reason": preview.reason,
        "fingerprint": preview.fingerprint,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--invoice-id", type=_uuid)
    parser.add_argument("--subscription-id", type=_uuid)
    parser.add_argument("--account-id", type=_uuid)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--adopt-proforma", action="store_true")
    parser.add_argument("--repair-paid-invoice", action="store_true")
    parser.add_argument("--repair-missing-paid-invoice", action="store_true")
    parser.add_argument("--payment-id", type=_uuid)
    parser.add_argument("--issued-on", type=_date)
    parser.add_argument("--due-on", type=_date)
    parser.add_argument("--next-billing-on", type=_date)
    parser.add_argument("--expected-total", type=_money)
    parser.add_argument("--expected-remaining-credit", type=_money)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--fingerprint")
    parser.add_argument("--effective-at", type=_timestamp)
    parser.add_argument("--idempotency-key")
    parser.add_argument("--actor")
    parser.add_argument("--reason")
    args = parser.parse_args()

    document_modes = sum(
        bool(value)
        for value in (
            args.adopt_proforma,
            args.repair_paid_invoice,
            args.repair_missing_paid_invoice,
        )
    )
    if document_modes > 1:
        parser.error("document repair modes are mutually exclusive")

    missing_only_values = (
        args.payment_id,
        args.issued_on,
        args.due_on,
        args.next_billing_on,
        args.expected_total,
        args.expected_remaining_credit,
    )
    if not args.repair_missing_paid_invoice and any(
        value is not None for value in missing_only_values
    ):
        parser.error(
            "payment, business-date, and expected-credit arguments require "
            "--repair-missing-paid-invoice"
        )
    if args.repair_missing_paid_invoice and (
        args.invoice_id is not None
        or args.limit is not None
        or args.effective_at is not None
    ):
        parser.error(
            "--invoice-id, --limit, and --effective-at cannot be used with "
            "--repair-missing-paid-invoice"
        )

    missing_query_values = (
        args.account_id,
        args.subscription_id,
        args.payment_id,
        args.issued_on,
        args.due_on,
        args.next_billing_on,
        args.expected_total,
        args.expected_remaining_credit,
    )
    if args.repair_missing_paid_invoice and any(
        value is None for value in missing_query_values
    ):
        parser.error(
            "--repair-missing-paid-invoice requires --account-id, "
            "--subscription-id, --payment-id, --issued-on, --due-on, "
            "--next-billing-on, --expected-total, and "
            "--expected-remaining-credit"
        )
    missing_query = (
        MissingPaidPrepaidInvoiceRepairQuery(
            account_id=args.account_id,
            subscription_id=args.subscription_id,
            payment_id=args.payment_id,
            issued_on=args.issued_on,
            due_on=args.due_on,
            next_billing_on=args.next_billing_on,
            expected_total=args.expected_total,
            expected_remaining_credit=args.expected_remaining_credit,
        )
        if args.repair_missing_paid_invoice
        else None
    )

    if args.limit is not None and args.limit <= 0:
        parser.error("--limit must be positive")
    if args.apply:
        required = [
            ("--fingerprint", args.fingerprint),
            ("--idempotency-key", args.idempotency_key),
            ("--actor", args.actor),
            ("--reason", args.reason),
        ]
        if not args.repair_missing_paid_invoice:
            if args.adopt_proforma or args.repair_paid_invoice:
                required.append(("--invoice-id", args.invoice_id))
                required.append(("--subscription-id", args.subscription_id))
            else:
                required.append(("--invoice-id", args.invoice_id))
                required.append(("--effective-at", args.effective_at))
        missing = [name for name, value in required if not value]
        if missing:
            parser.error("--apply requires " + ", ".join(missing))
        if not args.repair_missing_paid_invoice and (
            args.account_id is not None or args.limit is not None
        ):
            parser.error("--account-id and --limit are preview-only")
        with db_session_adapter.owner_command_session() as db:
            context = CommandContext.system(
                actor=args.actor,
                scope="prepaid_draft_reconciliation",
                reason=args.reason,
                idempotency_key=args.idempotency_key,
            )
            if args.repair_missing_paid_invoice:
                assert missing_query is not None
                missing_repair = create_reviewed_paid_prepaid_invoice(
                    db,
                    CreateReviewedPaidPrepaidInvoiceCommand(
                        context=context,
                        query=missing_query,
                        preview_fingerprint=args.fingerprint,
                    ),
                )
            elif args.adopt_proforma:
                adoption = adopt_funded_prepaid_proforma(
                    db,
                    AdoptFundedPrepaidProformaCommand(
                        context=context,
                        invoice_id=args.invoice_id,
                        subscription_id=args.subscription_id,
                        preview_fingerprint=args.fingerprint,
                    ),
                )
            elif args.repair_paid_invoice:
                repair = repair_historical_paid_prepaid_invoice(
                    db,
                    RepairHistoricalPaidPrepaidInvoiceCommand(
                        context=context,
                        invoice_id=args.invoice_id,
                        subscription_id=args.subscription_id,
                        preview_fingerprint=args.fingerprint,
                    ),
                )
            else:
                result = reconcile_prepaid_draft_invoice(
                    db,
                    ReconcilePrepaidDraftCommand(
                        context=context,
                        invoice_id=args.invoice_id,
                        preview_fingerprint=args.fingerprint,
                        effective_at=args.effective_at,
                    ),
                )
        if args.repair_missing_paid_invoice:
            print(
                json.dumps(
                    {
                        "invoice_id": str(missing_repair.invoice_id),
                        "invoice_number": missing_repair.invoice_number,
                        "subscription_id": str(missing_repair.subscription_id),
                        "payment_id": str(missing_repair.payment_id),
                        "entitlement_id": str(missing_repair.entitlement_id),
                        "issued_at": missing_repair.issued_at.isoformat(),
                        "paid_at": missing_repair.paid_at.isoformat(),
                        "due_at": missing_repair.due_at.isoformat(),
                        "billing_period_start": (
                            missing_repair.billing_period_start.isoformat()
                        ),
                        "billing_period_end": (
                            missing_repair.billing_period_end.isoformat()
                        ),
                        "total": str(missing_repair.total),
                        "remaining_credit": str(missing_repair.remaining_credit),
                        "preview_fingerprint": (missing_repair.preview_fingerprint),
                        "replayed": missing_repair.replayed,
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        if args.adopt_proforma:
            print(
                json.dumps(
                    {
                        "invoice_id": str(adoption.invoice_id),
                        "subscription_id": str(adoption.subscription_id),
                        "line_id": str(adoption.line_id),
                        "settlement_payment_id": str(adoption.settlement_payment_id),
                        "settlement_effective_at": (
                            adoption.settlement_effective_at.isoformat()
                        ),
                        "billing_period_start": (
                            adoption.billing_period_start.isoformat()
                        ),
                        "billing_period_end": adoption.billing_period_end.isoformat(),
                        "preview_fingerprint": adoption.preview_fingerprint,
                        "replayed": adoption.replayed,
                        "next_step": "preview and apply the financial draft reconciliation",
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        if args.repair_paid_invoice:
            print(
                json.dumps(
                    {
                        "invoice_id": str(repair.invoice_id),
                        "subscription_id": str(repair.subscription_id),
                        "line_id": str(repair.line_id),
                        "allocation_id": str(repair.allocation_id),
                        "settlement_id": str(repair.settlement_id),
                        "payment_id": str(repair.payment_id),
                        "entitlement_id": str(repair.entitlement_id),
                        "access_consequence_id": str(repair.access_consequence_id),
                        "billing_period_start": repair.billing_period_start.isoformat(),
                        "billing_period_end": repair.billing_period_end.isoformat(),
                        "subscriptions_restored": repair.subscriptions_restored,
                        "preview_fingerprint": repair.preview_fingerprint,
                        "replayed": repair.replayed,
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        print(
            json.dumps(
                {
                    "invoice_id": str(result.invoice_id),
                    "source_disposition": result.disposition.value,
                    "action": result.action.value,
                    "final_status": result.final_status.value,
                    "applied_amount": str(result.applied_amount),
                    "payment_applied_amount": str(result.payment_applied_amount),
                    "opening_funding_applied_amount": str(
                        result.opening_funding_applied_amount
                    ),
                    "opening_funding_consumption_id": (
                        str(result.opening_funding_consumption_id)
                        if result.opening_funding_consumption_id
                        else None
                    ),
                    "preview_fingerprint": result.preview_fingerprint,
                    "replayed": result.replayed,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    if (args.adopt_proforma or args.repair_paid_invoice) and (
        args.invoice_id is None or args.subscription_id is None
    ):
        parser.error(
            "document repair preview requires --invoice-id and --subscription-id"
        )
    if args.subscription_id is not None and not (
        args.adopt_proforma
        or args.repair_paid_invoice
        or args.repair_missing_paid_invoice
    ):
        parser.error(
            "--subscription-id requires --adopt-proforma or --repair-paid-invoice"
        )
    if (args.adopt_proforma or args.repair_paid_invoice) and (
        args.account_id is not None or args.limit is not None
    ):
        parser.error("--account-id and --limit cannot be used with document repair")

    previews: tuple[PrepaidDraftReconciliationPreview, ...]
    with db_session_adapter.read_session() as db:
        if args.repair_missing_paid_invoice:
            assert missing_query is not None
            missing_preview = preview_missing_paid_prepaid_invoice_repair(
                db,
                missing_query,
            )
            payload = {
                "dry_run": True,
                "operation": "create_reviewed_paid_prepaid_invoice",
                "item": _missing_paid_invoice_preview_payload(missing_preview),
            }
        elif args.adopt_proforma:
            adoption_preview = preview_funded_prepaid_proforma_adoption(
                db,
                PrepaidProformaAdoptionQuery(
                    invoice_id=args.invoice_id,
                    subscription_id=args.subscription_id,
                ),
            )
            payload = {
                "dry_run": True,
                "operation": "adopt_funded_prepaid_proforma",
                "item": _proforma_adoption_preview_payload(adoption_preview),
            }
        elif args.repair_paid_invoice:
            repair_preview = preview_historical_paid_prepaid_invoice_repair(
                db,
                PaidPrepaidInvoiceRepairQuery(
                    invoice_id=args.invoice_id,
                    subscription_id=args.subscription_id,
                ),
            )
            payload = {
                "dry_run": True,
                "operation": "repair_historical_paid_prepaid_invoice",
                "item": _paid_invoice_repair_preview_payload(repair_preview),
            }
        elif args.invoice_id is not None:
            previews = (preview_prepaid_draft_reconciliation(db, args.invoice_id),)
            payload = {
                "dry_run": True,
                "candidate_count": len(previews),
                "actionable_count": sum(item.actionable for item in previews),
                "items": [_preview_payload(item) for item in previews],
            }
        else:
            previews = preview_prepaid_draft_cohort(
                db,
                account_id=args.account_id,
                limit=args.limit,
            )
            payload = {
                "dry_run": True,
                "candidate_count": len(previews),
                "actionable_count": sum(item.actionable for item in previews),
                "items": [_preview_payload(item) for item in previews],
            }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
