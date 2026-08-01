"""Operator CLI for the ADR 0007 billing-target shadow machinery.

The expand phases write shadow records beside current billing behavior; this
is the operator surface that drives and inspects them while the cutover-gate
evidence is collected. It is a thin adapter: it opens the session, parses
arguments, and calls the registered owners. It owns no business decision.

Subcommands::

    poetry run python -m scripts.billing.billing_target_shadow position --account <id> --currency NGN
    poetry run python -m scripts.billing.billing_target_shadow open-obligations --account <id> --currency NGN
    poetry run python -m scripts.billing.billing_target_shadow rate --version <id> --line-key <key> --index 0
    poetry run python -m scripts.billing.billing_target_shadow preview-addon-contract --subscription <id> --index 1
    poetry run python -m scripts.billing.billing_target_shadow capture-addon-contract --subscription <id> --index 1 --preview-fingerprint <sha256> --idempotency-key <key>
    poetry run python -m scripts.billing.billing_target_shadow verify-rating-cohort --cutoff <ISO> --window-start <ISO> --window-end <ISO> --code-version <sha> --schema-version <revision> --idempotency-key <key>
    poetry run python -m scripts.billing.billing_target_shadow fire-due-timers [--limit 200]
    poetry run python -m scripts.billing.billing_target_shadow advance-collections --obligation <id>
    poetry run python -m scripts.billing.billing_target_shadow funding-status --order <id>
    poetry run python -m scripts.billing.billing_target_shadow pending-erp-exports [--limit 50]

Everything stays shadow: consequence requests, timers, and exports carry the
authority their owners derive from the manifest migration state, and nothing
here can promote them.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from uuid import UUID

from app.db import SessionLocal
from app.models.billing_contract import BillingObligation
from app.services.billing.addon_contract_backfill import (
    BillingAddonContractBackfill,
    CaptureRecurringAddonBackfillCommand,
)
from app.services.billing.contracts import BillingContracts
from app.services.billing.customer_subledger import resolve_position
from app.services.billing.obligations import BillingObligations
from app.services.billing.rating import rate_line_period
from app.services.billing.shadow_verification import (
    BillingShadowVerification,
    RecordPhase2VerificationCommand,
)
from app.services.collections.lifecycle import CollectionsLifecycle
from app.services.collections.postpaid_policy import plan_postpaid_consequence
from app.services.collections.prepaid_policy import plan_prepaid_consequence
from app.services.dotmac_erp.billing_adapter import pending_exports
from app.services.owner_commands import CommandContext
from app.services.runtime_durable_timers import fire_due_timers
from app.services.sales_order_funding import SalesOrderFunding


def _context(reason: str, *, idempotency_key: str | None = None) -> CommandContext:
    from uuid import uuid4

    return CommandContext.system(
        actor="operator:billing_target_shadow",
        scope="billing-target-shadow",
        reason=reason,
        idempotency_key=idempotency_key or f"billing-target-shadow:{uuid4()}",
    )


def _emit(payload: dict) -> None:
    print(json.dumps(payload, indent=2, default=str))


def _cmd_position(db, args) -> int:
    position = resolve_position(
        db, account_id=UUID(args.account), currency=args.currency
    )
    _emit(
        {
            "account_id": position.account_id,
            "currency": position.currency,
            "authority": position.authority.value,
            "collectible_receivable": position.collectible_receivable,
            "unapplied_customer_credit": position.unapplied_customer_credit,
            "prepaid_funding_reserved": position.prepaid_funding_reserved,
            "prepaid_funding_consumed": position.prepaid_funding_consumed,
            "written_off_total": position.written_off_total,
            "refunded_total": position.refunded_total,
            "adjustment_total": position.adjustment_total,
        }
    )
    return 0


def _cmd_open_obligations(db, args) -> int:
    rows = BillingObligations.open_obligations_for_account(
        db, account_id=UUID(args.account), currency=args.currency
    )
    _emit(
        {
            "count": len(rows),
            "obligations": [
                {
                    "id": row.id,
                    "period_start": row.period_start,
                    "period_end": row.period_end,
                    "gross_amount": row.gross_amount,
                    "resolved_amount": row.resolved_amount,
                    "state": row.state.value,
                    "treatment": row.accounting_treatment.value,
                }
                for row in rows
            ],
        }
    )
    return 0


def _cmd_rate(db, args) -> int:
    from app.models.billing_contract import BillingContractVersion
    from app.services.billing.cadence import service_period

    version = db.get(BillingContractVersion, UUID(args.version))
    if version is None:
        print("contract version not found", file=sys.stderr)
        return 1
    starts_at = version.starts_at
    if starts_at.tzinfo is None:
        starts_at = starts_at.replace(tzinfo=UTC)
    period = service_period(
        cadence=BillingContracts.cadence_of(version),
        contract_start=starts_at,
        index=args.index,
    )
    rated = rate_line_period(
        db,
        contract_version_id=version.id,
        contract_line_key=UUID(args.line_key),
        period=period,
    )
    _emit(
        {
            "period_start": rated.period.starts_at,
            "period_end": rated.period.ends_at,
            "currency": rated.currency,
            "net_amount": rated.net_amount,
            "tax_amount": rated.tax_amount,
            "gross_amount": rated.gross_amount,
            "rate_units": rated.rate_units,
            "proration": rated.proration,
            "tax_treatment_code": rated.tax_treatment_code,
        }
    )
    return 0


def _cmd_preview_addon_contract(db, args) -> int:
    preview = BillingAddonContractBackfill.preview(
        db,
        subscription_id=UUID(args.subscription),
        period_index=args.index,
    )
    _emit(
        {
            "account_id": preview.account_id,
            "subscription_id": preview.subscription_id,
            "contract_id": preview.contract_id,
            "current_contract_version_id": preview.current_contract_version_id,
            "sales_order_id": preview.sales_order_id,
            "target_period_start": preview.target_period.starts_at,
            "target_period_end": preview.target_period.ends_at,
            "recurring_addon_count": len(preview.terms),
            "change_required": preview.change_required,
            "preview_fingerprint": preview.fingerprint,
            "terms": [
                {
                    "subscription_add_on_id": term.subscription_add_on_id,
                    "add_on_id": term.add_on_id,
                    "add_on_price_id": term.add_on_price_id,
                    "description": term.description,
                    "quantity": term.quantity,
                    "unit_price": term.unit_price,
                    "currency": term.currency,
                    "source_started_at": term.source_started_at,
                    "source_ends_at": term.source_ends_at,
                }
                for term in preview.terms
            ],
            "authority_moved": False,
            "repair_requested": False,
        }
    )
    return 0


def _cmd_capture_addon_contract(db, args) -> int:
    result = BillingAddonContractBackfill.capture(
        db,
        CaptureRecurringAddonBackfillCommand(
            subscription_id=UUID(args.subscription),
            period_index=args.index,
            preview_fingerprint=args.preview_fingerprint,
        ),
        context=_context(
            "capture reviewed recurring add-on terms into the shadow owner chain",
            idempotency_key=args.idempotency_key,
        ),
    )
    _emit(
        {
            "event_id": result.event_id,
            "preview_fingerprint": result.preview_fingerprint,
            "recurring_addon_count": result.recurring_addon_count,
            "replayed": result.replayed,
            "authority_moved": False,
            "repair_requested": False,
        }
    )
    return 0


def _cmd_preview_renewal_terms(db, args) -> int:
    from app.services.prepaid_renewal_terms_backfill import (
        preview_prepaid_renewal_terms_backfill,
    )

    preview = preview_prepaid_renewal_terms_backfill(db)
    _emit(
        {
            "as_of": preview.as_of,
            "blocked_subscriptions": len(preview.items),
            "repairable": preview.repairable_count,
            "unresolved": preview.unresolved_count,
            "preview_fingerprint": preview.fingerprint,
            "items": [
                {
                    "subscription_id": item.subscription_id,
                    "account_id": item.account_id,
                    "decision": item.decision.value,
                    "contracted_amount": item.contracted_amount,
                    "distinct_paid_amounts": list(item.distinct_paid_amounts),
                    "paid_line_count": item.paid_line_count,
                }
                for item in preview.items
            ],
            "authority_moved": False,
            "repair_requested": False,
        }
    )
    return 0


def _cmd_capture_renewal_terms(db, args) -> int:
    from app.services.prepaid_renewal_terms_backfill import (
        CaptureRenewalTermsBackfillCommand,
        capture_prepaid_renewal_terms_backfill,
    )

    # The fingerprint binds the evidence only; as_of stamps the finance
    # work-item SLA, so capture time is the correct moment.
    result = capture_prepaid_renewal_terms_backfill(
        db,
        CaptureRenewalTermsBackfillCommand(
            preview_fingerprint=args.preview_fingerprint,
            as_of=datetime.now(UTC),
        ),
        context=_context(
            "restore reviewed contracted prepaid renewal amounts from paid "
            "invoice evidence",
            idempotency_key=args.idempotency_key,
        ),
    )
    _emit(
        {
            "repaired_count": result.repaired_count,
            "work_item_count": result.work_item_count,
            "preview_fingerprint": result.fingerprint,
            "authority_moved": False,
            "repair_requested": True,
        }
    )
    return 0


def _instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("timestamp must include a timezone")
    return parsed


def _cmd_verify_rating_cohort(db, args) -> int:
    result = BillingShadowVerification.record_phase2_run(
        db,
        RecordPhase2VerificationCommand(
            cutoff_at=args.cutoff,
            observation_started_at=args.window_start,
            observation_ended_at=args.window_end,
            code_version=args.code_version,
            database_schema_version=args.schema_version,
            cohort_name=args.cohort,
        ),
        context=_context(
            "record ADR 0007 Phase 2 migration evidence",
            idempotency_key=args.idempotency_key,
        ),
    )
    _emit(
        {
            "run_id": result.run_id,
            "phase": "phase_2",
            "cohort_count": result.cohort_count,
            "covered_count": result.covered_count,
            "expected_difference_count": result.expected_difference_count,
            "blocker_count": result.blocker_count,
            "replayed": result.replayed,
            "authority_moved": False,
            "repair_requested": False,
        }
    )
    return 0


def _cmd_fire_due_timers(db, args) -> int:
    fired = fire_due_timers(
        db,
        now=datetime.now(UTC),
        context=_context("operator due-timer dispatch"),
        batch_limit=args.limit,
    )
    _emit(
        {
            "fired": len(fired),
            "triggers": [
                {
                    "timer_id": item.timer_id,
                    "owner": item.owner,
                    "purpose": item.purpose,
                    "generation": item.generation,
                    "output_event_type": item.output_event_type,
                    "event_id": item.event_id,
                }
                for item in fired
            ],
        }
    )
    return 0


def _cmd_advance_collections(db, args) -> int:
    obligation = db.get(BillingObligation, UUID(args.obligation))
    if obligation is None:
        print("obligation not found", file=sys.stderr)
        return 1
    now = datetime.now(UTC)
    proposal = plan_postpaid_consequence(
        db, obligation=obligation, now=now
    ) or plan_prepaid_consequence(db, obligation=obligation, now=now)
    if proposal is None:
        _emit({"advanced": False, "reason": "no actionable policy proposal"})
        return 0
    db.rollback()  # close the read transaction before the owner command
    result = CollectionsLifecycle.advance(
        db,
        proposal,
        context=_context("operator shadow collections advance"),
        now=now,
    )
    _emit(
        {
            "advanced": True,
            "case_id": result.case_id,
            "state": result.state.value,
            "consequence_event_id": result.consequence_event_id,
        }
    )
    return 0


def _cmd_funding_status(db, args) -> int:
    status = SalesOrderFunding.gate_status(db, sales_order_id=UUID(args.order))
    if status is None:
        _emit({"gate": None})
        return 0
    _emit(
        {
            "gate_id": status.gate_id,
            "state": status.state.value,
            "total_obligations": status.total_obligations,
            "resolved_obligations": status.resolved_obligations,
            "funded_event_id": status.funded_event_id,
        }
    )
    return 0


def _cmd_pending_erp_exports(db, args) -> int:
    rows = pending_exports(db, limit=args.limit)
    _emit(
        {
            "count": len(rows),
            "exports": [
                {
                    "id": row.id,
                    "flow": row.flow.value,
                    "source_kind": row.source_kind,
                    "source_id": row.source_id,
                    "payload_version": row.payload_version,
                    "attempts": row.attempts,
                    "created_at": row.created_at,
                }
                for row in rows
            ],
        }
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("position", help="typed per-currency subledger position")
    p.add_argument("--account", required=True)
    p.add_argument("--currency", default="NGN")
    p.set_defaults(func=_cmd_position)

    p = sub.add_parser("open-obligations", help="open obligations for an account")
    p.add_argument("--account", required=True)
    p.add_argument("--currency", default="NGN")
    p.set_defaults(func=_cmd_open_obligations)

    p = sub.add_parser("rate", help="deterministically rate one line period")
    p.add_argument("--version", required=True)
    p.add_argument("--line-key", required=True)
    p.add_argument("--index", type=int, default=0)
    p.set_defaults(func=_cmd_rate)

    p = sub.add_parser(
        "preview-addon-contract",
        help="preview one future-period recurring add-on contract snapshot",
    )
    p.add_argument("--subscription", required=True)
    p.add_argument("--index", type=int, default=1)
    p.set_defaults(func=_cmd_preview_addon_contract)

    p = sub.add_parser(
        "capture-addon-contract",
        help="emit one confirmed recurring add-on snapshot into the shadow chain",
    )
    p.add_argument("--subscription", required=True)
    p.add_argument("--index", type=int, default=1)
    p.add_argument("--preview-fingerprint", required=True)
    p.add_argument("--idempotency-key", required=True)
    p.set_defaults(func=_cmd_capture_addon_contract)

    p = sub.add_parser(
        "preview-renewal-terms",
        help="classify blocked prepaid subscriptions against paid evidence",
    )
    p.set_defaults(func=_cmd_preview_renewal_terms)

    p = sub.add_parser(
        "capture-renewal-terms",
        help="apply the reviewed renewal-terms backfill (fingerprint-bound)",
    )
    p.add_argument("--preview-fingerprint", required=True)
    p.add_argument("--idempotency-key", required=True)
    p.set_defaults(func=_cmd_capture_renewal_terms)

    p = sub.add_parser(
        "verify-rating-cohort",
        help="record durable Phase 2 parity/topology evidence",
    )
    p.add_argument("--cutoff", required=True, type=_instant)
    p.add_argument("--window-start", required=True, type=_instant)
    p.add_argument("--window-end", required=True, type=_instant)
    p.add_argument("--code-version", required=True)
    p.add_argument("--schema-version", required=True)
    p.add_argument("--idempotency-key", required=True)
    p.add_argument("--cohort", default="active_subscriptions")
    p.set_defaults(func=_cmd_verify_rating_cohort)

    p = sub.add_parser("fire-due-timers", help="emit due durable-timer triggers")
    p.add_argument("--limit", type=int, default=200)
    p.set_defaults(func=_cmd_fire_due_timers)

    p = sub.add_parser(
        "advance-collections", help="advance the shadow case for one obligation"
    )
    p.add_argument("--obligation", required=True)
    p.set_defaults(func=_cmd_advance_collections)

    p = sub.add_parser("funding-status", help="finite funding gate of one order")
    p.add_argument("--order", required=True)
    p.set_defaults(func=_cmd_funding_status)

    p = sub.add_parser("pending-erp-exports", help="undelivered ERP export batch")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=_cmd_pending_erp_exports)

    args = parser.parse_args()
    db = SessionLocal()
    try:
        return args.func(db, args)
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
