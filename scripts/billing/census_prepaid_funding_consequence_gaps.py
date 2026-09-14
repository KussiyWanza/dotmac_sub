#!/usr/bin/env python
"""Read-only census: prepaid funding events awaiting or missing consequence.

Rebuilt (round 2) after the round-1 version was found to produce systematic
false negatives: it checked "does this account have ANY active entitlement"
instead of "does this account have an active entitlement covering THIS
SPECIFIC period," and relied on `EventStore.subscription_id`, which is never
populated at the actual `payment.received`/`account_credit.deposited` emit
sites (`app/services/billing/payments.py`).

Primary source (accurate, exact-period, no reconstruction needed): the
`prepaid_funding_trigger_executions`/`prepaid_funding_trigger_subscription_outcomes`
receipt model and `prepaid_draft_reconciliation_exceptions` review-item
table, both of which now record the exact account/subscription/period a
blocked or ambiguous case concerns (see the funding-consequence
single-owner cutover). Every case going forward is found here with no
derivation required.

Secondary source (best-effort, historical, opt-in via `--include-legacy`):
`EventHandlerAttempt` failures for `PrepaidRenewalHandler` predating the
receipt/review-item model, where the affected period cannot be exactly
reconstructed from the event alone -- reported separately and explicitly
labeled as such, never merged into the accurate primary counts.

Writes nothing.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select

from app.models.event_store import EventHandlerAttempt, EventStore
from app.models.prepaid_funding import (
    PrepaidDraftReconciliationException,
    PrepaidFundingTriggerExecution,
    PrepaidFundingTriggerSubscriptionOutcome,
)
from app.services.db_session_adapter import db_session_adapter


def _utc(value: datetime) -> datetime:
    """Defensively normalize a DB-sourced timestamp to timezone-aware UTC.

    Matches the `_utc()` convention repeated across this codebase's service
    modules (e.g. `prepaid_service_renewals.py`, `prepaid_draft_
    reconciliation.py`) -- each module defines its own local copy rather
    than importing a shared one. Needed here because SQLite does not
    preserve timezone info on round-trip regardless of what is inserted
    (2026-09, round 9): a `DateTime(timezone=True)` column comes back NAIVE
    from SQLite even when written with an aware value, so subtracting it
    from `datetime.now(UTC)` raised `TypeError: can't subtract offset-naive
    and offset-aware datetimes` in this script's SQLite-backed unit tests.
    PostgreSQL preserves timezone correctly, which is why this was invisible
    in any Postgres-backed path -- but the read side must not assume its
    caller/database always hands back an aware value.
    """

    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


_HANDLER_NAME = "PrepaidRenewalHandler"
_FUNDING_EVENT_TYPES = ("payment.received", "account_credit.deposited")
# The ONLY values `PrepaidFundingTriggerExecution.disposition` is ever
# written with are `FundingChangeRenewalResult.disposition`'s own
# `FundingChangeRenewalDisposition` members (see
# `app/services/prepaid_service_renewals.py`'s single write site,
# `_record_prepaid_funding_trigger_execution`). `renewal_review_required`
# and `draft_invoice_review_required` are the two that mean "a subscription
# on this account needs review." Earlier versions of this script filtered on
# `blocked_ambiguous`/`blocked_opening_lane_unavailable`/
# `permanent_integrity_conflict` -- none of which this codebase's current
# write sites ever produce, so that filter silently matched zero rows.
# `permanent_integrity_conflict`-shaped cases (the receipt
# fingerprint-mismatch path) never reach the receipt writer at all -- they
# raise before it -- so they are NOT discoverable through this table; they
# are exact-period-accurate in `find_open_review_items` instead.
# `opening_lane_unavailable` (raised as `PrepaidOpeningLaneUnavailableError`,
# `financial.prepaid_service_renewals.opening_lane_unavailable`) DOES write a
# durable review item out of band before it raises -- it is fully
# census-visible through `find_open_review_items`, keyed on
# `subscription_id`/`period_start`/`period_end` with
# `reason="renewal_classification_failed_opening_lane_unavailable"`. It is
# NOT written into `PrepaidFundingTriggerExecution.disposition` as its own
# distinct value, though -- like the other ambiguous cases, it is folded into
# the receipt's aggregate `renewal_review_required` disposition, so it is
# still not separately distinguishable through THIS table's disposition
# column specifically.
_BLOCKED_TRIGGER_DISPOSITIONS = (
    "renewal_review_required",
    "draft_invoice_review_required",
)
# A disposition that claims a real settlement happened. Any receipt written
# with one of these MUST carry at least one
# `PrepaidFundingTriggerSubscriptionOutcome` child row -- the exact gap
# Michael found (2026-09, round 7): a `draft_invoice_settled` receipt could
# previously commit with ZERO children, because the existing-draft
# reconciliation branch discarded its own settlement evidence instead of
# reporting it back as a `PrepaidFundingSubscriptionDecision`. Fixed at the
# write site (`app/services/prepaid_service_renewals.py`'s
# `_record_prepaid_funding_trigger_execution`, which now refuses to commit
# this exact shape).
#
# Accuracy correction (2026-09, round 8): this is a hardcoded two-string
# disposition filter, not a disposition-name-independent structural check --
# `find_successful_receipts_missing_child_evidence` only inspects rows whose
# `disposition` is one of these two exact values, so a hypothetical future
# write site that names a THIRD "this succeeded" disposition string is
# invisible to this check until that string is added here too. What IS
# structural about it is narrower: for the two dispositions it does check,
# it inspects the actual child-row count rather than trusting the
# disposition alone.
#
# Known, deliberately un-widened gap: `stage_prepaid_draft_after_funding_
# change`'s `void_duplicate` outcome (`existing_draft_voided`) is never
# reported through `FundingChangeRenewalDisposition.draft_invoice_settled`/
# `funded` on its own -- a void-only funding event (no new renewal follows
# it in the same call) currently produces no receipt at all, so it is
# invisible to BOTH this check and `find_blocked_trigger_executions`. Voiding
# a duplicate is not itself a funding consequence needing review (the
# ORIGINAL invoice it duplicates is what actually got funded), so this is
# likely fine as-is, but it is flagged here honestly rather than silently
# left unmentioned.
_SUCCESSFUL_TRIGGER_DISPOSITIONS = (
    "draft_invoice_settled",
    "funded",
)


def _uuid(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("identifier must be a UUID") from exc


def find_open_review_items(
    db, *, account_id: UUID | None = None
) -> list[dict[str, object]]:
    """Exact-period cases from the durable review-item table (open only)."""

    query = select(PrepaidDraftReconciliationException).where(
        PrepaidDraftReconciliationException.status == "open"
    )
    if account_id is not None:
        query = query.where(
            PrepaidDraftReconciliationException.account_id == account_id
        )
    rows = db.scalars(
        query.order_by(PrepaidDraftReconciliationException.created_at)
    ).all()
    now = datetime.now(UTC)
    return [
        {
            "source": "review_item",
            "review_item_id": str(row.id),
            "account_id": str(row.account_id),
            "subscription_id": (
                str(row.subscription_id) if row.subscription_id else None
            ),
            "invoice_id": str(row.invoice_id) if row.invoice_id else None,
            "period_start": row.period_start.isoformat() if row.period_start else None,
            "period_end": row.period_end.isoformat() if row.period_end else None,
            "reason": row.reason,
            "currency": row.currency,
            "required_amount": str(row.required_amount),
            "attempt_count": row.attempt_count,
            "age_days": (now - _utc(row.created_at)).days if row.created_at else None,
        }
        for row in rows
    ]


def find_blocked_trigger_executions(
    db, *, account_id: UUID | None = None
) -> list[dict[str, object]]:
    """Exact-period cases from the receipt model's blocked/conflict dispositions."""

    query = select(PrepaidFundingTriggerExecution).where(
        PrepaidFundingTriggerExecution.disposition.in_(_BLOCKED_TRIGGER_DISPOSITIONS)
    )
    if account_id is not None:
        query = query.where(PrepaidFundingTriggerExecution.account_id == account_id)
    receipts = db.scalars(
        query.order_by(PrepaidFundingTriggerExecution.created_at)
    ).all()
    now = datetime.now(UTC)
    cases: list[dict[str, object]] = []
    for receipt in receipts:
        outcomes = db.scalars(
            select(PrepaidFundingTriggerSubscriptionOutcome).where(
                PrepaidFundingTriggerSubscriptionOutcome.trigger_execution_id
                == receipt.id
            )
        ).all()
        base = {
            "source": "trigger_execution",
            "trigger_execution_id": str(receipt.id),
            "event_id": str(receipt.event_id),
            "account_id": str(receipt.account_id),
            "disposition": receipt.disposition,
            "currency": receipt.currency,
            "age_days": (
                (now - _utc(receipt.created_at)).days if receipt.created_at else None
            ),
        }
        if not outcomes:
            cases.append(
                {
                    **base,
                    "subscription_id": None,
                    "period_start": None,
                    "period_end": None,
                }
            )
            continue
        for outcome in outcomes:
            cases.append(
                {
                    **base,
                    "subscription_id": str(outcome.subscription_id),
                    "period_start": outcome.period_start.isoformat(),
                    "period_end": outcome.period_end.isoformat(),
                    "amount": str(outcome.amount),
                }
            )
    return cases


def find_successful_receipts_missing_child_evidence(
    db, *, account_id: UUID | None = None
) -> list[dict[str, object]]:
    """A receipt that CLAIMS a real settlement but proves none happened.

    A `draft_invoice_settled`/`funded` disposition with zero
    `PrepaidFundingTriggerSubscriptionOutcome` rows means the
    payment->period->invoice->entitlement consequence chain is completely
    unbound for whatever this receipt was supposed to record -- a
    false-clean result the disposition-filter-only checks above (blocked/
    review-required dispositions) structurally cannot see, because this
    receipt does not report itself as blocked at all.
    """

    query = select(PrepaidFundingTriggerExecution).where(
        PrepaidFundingTriggerExecution.disposition.in_(_SUCCESSFUL_TRIGGER_DISPOSITIONS)
    )
    if account_id is not None:
        query = query.where(PrepaidFundingTriggerExecution.account_id == account_id)
    receipts = db.scalars(
        query.order_by(PrepaidFundingTriggerExecution.created_at)
    ).all()
    now = datetime.now(UTC)
    cases: list[dict[str, object]] = []
    for receipt in receipts:
        has_child = (
            db.scalar(
                select(PrepaidFundingTriggerSubscriptionOutcome.id)
                .where(
                    PrepaidFundingTriggerSubscriptionOutcome.trigger_execution_id
                    == receipt.id
                )
                .limit(1)
            )
            is not None
        )
        if has_child:
            continue
        cases.append(
            {
                "source": "successful_receipt_missing_child_evidence",
                "trigger_execution_id": str(receipt.id),
                "event_id": str(receipt.event_id),
                "account_id": str(receipt.account_id),
                "disposition": receipt.disposition,
                "currency": receipt.currency,
                "age_days": (
                    (now - _utc(receipt.created_at)).days
                    if receipt.created_at
                    else None
                ),
            }
        )
    return cases


def find_legacy_unreconciled_handler_failures(
    db, *, account_id: UUID | None = None
) -> list[dict[str, object]]:
    """Best-effort, historical only: pre-receipt-model handler failures.

    Cannot exactly reconstruct the affected period from the event alone (see
    module docstring) -- reports the failed event/payment for manual triage
    rather than guessing a period and silently under- or over-counting.
    Excludes any event that already has a `PrepaidFundingTriggerExecution`
    receipt (those are covered, exactly, by
    :func:`find_blocked_trigger_executions`, or the event actually
    succeeded).
    """

    query = (
        select(EventStore, EventHandlerAttempt)
        .join(EventHandlerAttempt, EventHandlerAttempt.event_store_id == EventStore.id)
        .where(
            EventStore.event_type.in_(_FUNDING_EVENT_TYPES),
            EventHandlerAttempt.handler_name == _HANDLER_NAME,
            EventHandlerAttempt.status.in_(("failed", "failed_permanent")),
        )
        .order_by(EventStore.created_at)
    )
    if account_id is not None:
        query = query.where(EventStore.account_id == account_id)

    receipted_event_ids = set(
        db.scalars(select(PrepaidFundingTriggerExecution.event_id)).all()
    )
    now = datetime.now(UTC)
    cases: list[dict[str, object]] = []
    for event, attempt in db.execute(query).all():
        if event.event_id in receipted_event_ids:
            continue
        payment_id = (event.payload or {}).get("payment_id")
        cases.append(
            {
                "source": "legacy_handler_failure",
                "event_id": str(event.event_id),
                "event_type": event.event_type,
                "account_id": str(event.account_id) if event.account_id else None,
                "payment_id": payment_id,
                "handler_status": attempt.status,
                "handler_error": attempt.error,
                "age_days": (
                    (now - _utc(event.created_at)).days if event.created_at else None
                ),
                "note": (
                    "exact affected subscription/period not reconstructable "
                    "from this legacy record -- triage manually against the "
                    "account's payment/invoice history"
                ),
            }
        )
    return cases


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-id", type=_uuid)
    parser.add_argument(
        "--include-legacy",
        action="store_true",
        help="Also report best-effort pre-receipt-model handler failures.",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    with db_session_adapter.read_session() as db:
        try:
            review_items = find_open_review_items(db, account_id=args.account_id)
            trigger_cases = find_blocked_trigger_executions(
                db, account_id=args.account_id
            )
            missing_evidence_cases = find_successful_receipts_missing_child_evidence(
                db, account_id=args.account_id
            )
            legacy_cases = (
                find_legacy_unreconciled_handler_failures(
                    db, account_id=args.account_id
                )
                if args.include_legacy
                else []
            )
        finally:
            db_session_adapter.release_read_transaction(db)

    payload = {
        "open_review_items": review_items,
        "blocked_trigger_executions": trigger_cases,
        "successful_receipts_missing_child_evidence": missing_evidence_cases,
        "legacy_unreconciled_handler_failures": legacy_cases,
        "totals": {
            "open_review_items": len(review_items),
            "blocked_trigger_executions": len(trigger_cases),
            "successful_receipts_missing_child_evidence": len(missing_evidence_cases),
            "legacy_unreconciled_handler_failures": len(legacy_cases),
        },
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    else:
        print(json.dumps(payload["totals"], indent=2, sort_keys=True))
        for case in review_items:
            print(
                f"  [review_item] account={case['account_id']} "
                f"subscription={case['subscription_id']} "
                f"period=[{case['period_start']}, {case['period_end']}) "
                f"reason={case['reason']} age_days={case['age_days']}"
            )
        for case in trigger_cases:
            print(
                f"  [trigger] account={case['account_id']} "
                f"subscription={case.get('subscription_id')} "
                f"period=[{case.get('period_start')}, {case.get('period_end')}) "
                f"disposition={case['disposition']} age_days={case['age_days']}"
            )
        for case in missing_evidence_cases:
            print(
                f"  [missing_evidence] account={case['account_id']} "
                f"trigger_execution={case['trigger_execution_id']} "
                f"disposition={case['disposition']} age_days={case['age_days']} "
                "-- successful disposition with ZERO child outcome rows"
            )
        for case in legacy_cases:
            print(
                f"  [legacy] account={case['account_id']} event={case['event_id']} "
                f"payment={case['payment_id']} age_days={case['age_days']} "
                f"-- {case['note']}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
