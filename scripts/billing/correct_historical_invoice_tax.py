"""Preview or apply one reviewed historical invoice VAT correction."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.system_user import SystemUser
from app.services.auth_dependencies import has_permission
from app.services.db_session_adapter import db_session_adapter
from app.services.domain_errors import DomainError
from app.services.historical_invoice_tax_corrections import (
    CORRECTION_SCOPE,
    CorrectHistoricalInvoiceTaxCommand,
    HistoricalInvoiceTaxCorrectionPreview,
    HistoricalInvoiceTaxCorrectionQuery,
    correct_historical_invoice_tax,
    preview_historical_invoice_tax_correction,
)
from app.services.owner_commands import CommandContext
from app.services.system_user_assignments import system_user_role_names


def _aware_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected an ISO-8601 datetime") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("datetime must include a UTC offset")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-id", type=UUID, required=True)
    parser.add_argument("--source-invoice-id", type=UUID, required=True)
    parser.add_argument("--source-invoice-line-id", type=UUID, required=True)
    parser.add_argument("--void-evidence-invoice-id", type=UUID, required=True)
    parser.add_argument("--subscription-invoice-id", type=UUID, required=True)
    parser.add_argument("--payment-id", type=UUID, required=True)
    parser.add_argument("--tax-rate-id", type=UUID, required=True)
    parser.add_argument("--issued-at", type=_aware_datetime, required=True)
    parser.add_argument("--due-at", type=_aware_datetime, required=True)
    parser.add_argument("--currency", default="NGN")
    parser.add_argument("--reason", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-preview-fingerprint")
    parser.add_argument("--command-id", type=UUID)
    parser.add_argument("--actor")
    parser.add_argument("--actor-system-user-id", type=UUID)
    parser.add_argument("--idempotency-key")
    return parser


def _query(args: argparse.Namespace) -> HistoricalInvoiceTaxCorrectionQuery:
    return HistoricalInvoiceTaxCorrectionQuery(
        account_id=args.account_id,
        source_invoice_id=args.source_invoice_id,
        source_invoice_line_id=args.source_invoice_line_id,
        void_evidence_invoice_id=args.void_evidence_invoice_id,
        subscription_invoice_id=args.subscription_invoice_id,
        payment_id=args.payment_id,
        tax_rate_id=args.tax_rate_id,
        issued_at=args.issued_at,
        due_at=args.due_at,
        currency=args.currency,
    )


def _preview_dict(value: HistoricalInvoiceTaxCorrectionPreview) -> dict[str, object]:
    return {
        "disposition": value.disposition.value,
        "actionable": value.actionable,
        "reason": value.reason,
        "account_id": str(value.account_id),
        "source_invoice_id": str(value.source_invoice_id),
        "source_invoice_number": value.source_invoice_number,
        "void_evidence_invoice_id": str(value.void_evidence_invoice_id),
        "subscription_invoice_id": str(value.subscription_invoice_id),
        "subscription_invoice_number": value.subscription_invoice_number,
        "payment_id": str(value.payment_id),
        "tax_rate_id": str(value.tax_rate_id),
        "currency": value.currency,
        "source_subtotal": str(value.source_subtotal),
        "source_tax_total": str(value.source_tax_total),
        "subscription_total": str(value.subscription_total),
        "tax_rate_percent": str(value.tax_rate_percent),
        "tax_amount": str(value.tax_amount),
        "replacement_total": str(value.replacement_total),
        "payment_amount": str(value.payment_amount),
        "payment_available_before": str(value.payment_available_before),
        "payment_available_after_void": str(value.payment_available_after_void),
        "projected_final_payment_available": str(
            value.projected_final_payment_available
        ),
        "account_credit_before": str(value.account_credit_before),
        "projected_final_account_credit": str(value.projected_final_account_credit),
        "source_void_fingerprint": value.source_void_fingerprint,
        "source_payment_allocation_id": (
            str(value.source_payment_allocation_id)
            if value.source_payment_allocation_id is not None
            else None
        ),
        "replacement_invoice_id": (
            str(value.replacement_invoice_id)
            if value.replacement_invoice_id is not None
            else None
        ),
        "preview_fingerprint": value.fingerprint,
    }


def _permission_granted(db: Session, actor_system_user_id: UUID | None) -> bool:
    if actor_system_user_id is None:
        return False
    user = db.get(SystemUser, actor_system_user_id)
    if user is None or not user.is_active:
        return False
    return has_permission(
        {
            "principal_id": str(actor_system_user_id),
            "principal_type": "system_user",
            "roles": set(system_user_role_names(db, actor_system_user_id)),
        },
        db,
        CORRECTION_SCOPE,
    )


def main() -> int:
    args = _parser().parse_args()
    query = _query(args)
    try:
        with db_session_adapter.read_session() as db:
            preview = preview_historical_invoice_tax_correction(db, query)
            permission_granted = _permission_granted(db, args.actor_system_user_id)
        if not args.apply:
            print(
                json.dumps(
                    {"applied": False, "preview": _preview_dict(preview)},
                    sort_keys=True,
                )
            )
            return 0
        if not all(
            (
                args.expected_preview_fingerprint,
                args.command_id,
                args.actor,
                args.actor_system_user_id,
                args.idempotency_key,
            )
        ):
            print(
                json.dumps(
                    {
                        "applied": False,
                        "error": (
                            "--apply requires fingerprint, command ID, actor, "
                            "staff ID, and idempotency key"
                        ),
                        "preview": _preview_dict(preview),
                    },
                    sort_keys=True,
                )
            )
            return 2
        with db_session_adapter.owner_command_session() as db:
            outcome = correct_historical_invoice_tax(
                db,
                CorrectHistoricalInvoiceTaxCommand(
                    query=query,
                    expected_preview_fingerprint=args.expected_preview_fingerprint,
                    permission_granted=permission_granted,
                    authorized_system_user_id=args.actor_system_user_id,
                ),
                context=CommandContext.system(
                    actor=args.actor,
                    scope=CORRECTION_SCOPE,
                    reason=args.reason,
                    command_id=args.command_id,
                    correlation_id=args.command_id,
                    idempotency_key=args.idempotency_key,
                ),
            )
    except DomainError as exc:
        print(
            json.dumps(
                {
                    "applied": False,
                    "code": exc.code,
                    "message": exc.message,
                    "details": exc.details,
                },
                sort_keys=True,
            )
        )
        return 1

    print(
        json.dumps(
            {
                "applied": True,
                "source_invoice_id": str(outcome.source_invoice_id),
                "source_invoice_closure_id": str(outcome.source_invoice_closure_id),
                "source_payment_allocation_id": str(
                    outcome.source_payment_allocation_id
                ),
                "subscription_invoice_id": str(outcome.subscription_invoice_id),
                "replacement_invoice_id": str(outcome.replacement_invoice_id),
                "payment_id": str(outcome.payment_id),
                "subscription_payment_allocation_id": str(
                    outcome.subscription_payment_allocation_id
                ),
                "replacement_payment_allocation_id": str(
                    outcome.replacement_payment_allocation_id
                ),
                "source_subtotal": str(outcome.source_subtotal),
                "subscription_total": str(outcome.subscription_total),
                "tax_amount": str(outcome.tax_amount),
                "replacement_total": str(outcome.replacement_total),
                "currency": outcome.currency,
                "preview_fingerprint": outcome.preview_fingerprint,
                "replayed": outcome.replayed,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
