#!/usr/bin/env python
"""Preview or apply stranded prepaid draft invoice reconciliation.

Preview is the default and writes nothing. Apply is deliberately one invoice at
a time and requires the exact reviewed fingerprint, timestamp, idempotency key,
actor, and reason. A shortfall, including NGN 0.50, is never rounded or waived.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from uuid import UUID

from app.services.db_session_adapter import db_session_adapter
from app.services.owner_commands import CommandContext
from app.services.prepaid_draft_reconciliation import (
    ReconcilePrepaidDraftCommand,
    preview_prepaid_draft_cohort,
    preview_prepaid_draft_reconciliation,
    reconcile_prepaid_draft_invoice,
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--invoice-id", type=_uuid)
    parser.add_argument("--account-id", type=_uuid)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--fingerprint")
    parser.add_argument("--effective-at", type=_timestamp)
    parser.add_argument("--idempotency-key")
    parser.add_argument("--actor")
    parser.add_argument("--reason")
    args = parser.parse_args()

    if args.limit is not None and args.limit <= 0:
        parser.error("--limit must be positive")
    if args.apply:
        missing = [
            name
            for name, value in (
                ("--invoice-id", args.invoice_id),
                ("--fingerprint", args.fingerprint),
                ("--effective-at", args.effective_at),
                ("--idempotency-key", args.idempotency_key),
                ("--actor", args.actor),
                ("--reason", args.reason),
            )
            if not value
        ]
        if missing:
            parser.error("--apply requires " + ", ".join(missing))
        if args.account_id is not None or args.limit is not None:
            parser.error("--account-id and --limit are preview-only")
        with db_session_adapter.owner_command_session() as db:
            result = reconcile_prepaid_draft_invoice(
                db,
                ReconcilePrepaidDraftCommand(
                    context=CommandContext.system(
                        actor=args.actor,
                        scope="prepaid_draft_reconciliation",
                        reason=args.reason,
                        idempotency_key=args.idempotency_key,
                    ),
                    invoice_id=args.invoice_id,
                    preview_fingerprint=args.fingerprint,
                    effective_at=args.effective_at,
                ),
            )
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

    with db_session_adapter.read_session() as db:
        if args.invoice_id is not None:
            previews = (preview_prepaid_draft_reconciliation(db, args.invoice_id),)
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
