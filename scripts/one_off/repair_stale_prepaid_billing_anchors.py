"""Repair prepaid billing anchors that diverge from exact funded coverage.

The cohort: an ACTIVE ``ServiceEntitlement`` ends after the subscription's
``next_billing_at``. The customer has already paid for that period — the
entitlement is the exact funded-coverage evidence — but the billing anchor
still says the period is due, so the runner re-invoices it and enforcement
suspends the account for service it was already paid for.

The drift exists because the payment-allocation path committed entitlement
evidence without ever emitting a durable ``payment.received`` event, so
``financial.prepaid_service_renewals`` — the sole owner of billing-anchor
advancement — was never invoked. The emission is fixed forward; this command
repairs the accumulated backlog.

The same owner also admits an active prepaid subscription with a NULL anchor
when an active entitlement proves its exact paid-through boundary. NULL rows
without that evidence remain review stock.

An anchor ahead of exact coverage is excluded from bulk discovery. Operators
may select explicit subscription UUIDs and opt into unsupported-lead repair;
an applied service extension still quarantines the row rather than being
silently clawed back.

Preview-then-apply, idempotent, and evidence-producing:
  * preview is read-only and fingerprint-bound;
  * apply re-reads every candidate under an account lock and skips any row
    whose coverage changed since the preview;
  * each repair reserves an idempotency key and stages an audit event, so a
    replay is a no-op;
  * a repaired subscription leaves the cohort permanently, so repeated runs
    drive the cohort to zero.

No money is posted, moved, or forgiven. Dry run by default.
"""

from __future__ import annotations

import argparse
from uuid import UUID

from app.db import SessionLocal
from app.services.prepaid_service_renewals import (
    apply_stale_prepaid_billing_anchor_repair,
    preview_stale_prepaid_billing_anchor_repair,
)

DEFAULT_LIMIT = 500


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the repair. Default is a read-only preview.",
    )
    parser.add_argument(
        "--reviewed-sha256",
        help="Exact preview fingerprint approved for this apply pass.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help=f"Maximum candidates per pass (default {DEFAULT_LIMIT}).",
    )
    parser.add_argument(
        "--actor",
        default="operator:repair_stale_prepaid_billing_anchors",
        help="Who is running the repair; recorded as durable audit evidence.",
    )
    parser.add_argument(
        "--reason",
        default=(
            "Align billing anchor to exact funded entitlement coverage after "
            "operator review of projection drift"
        ),
        help="Why the repair is being run; recorded as durable audit evidence.",
    )
    parser.add_argument(
        "--subscription-id",
        action="append",
        default=[],
        type=UUID,
        help="Limit the preview/apply to this subscription UUID; repeatable.",
    )
    parser.add_argument(
        "--include-unsupported-leads",
        action="store_true",
        help=(
            "Allow an explicitly selected evidence-free anchor lead to be "
            "retracted to exact entitlement coverage. Requires --subscription-id."
        ),
    )
    args = parser.parse_args()

    if args.include_unsupported_leads and not args.subscription_id:
        parser.error("--include-unsupported-leads requires --subscription-id")

    db = SessionLocal()
    try:
        preview = preview_stale_prepaid_billing_anchor_repair(
            db,
            limit=args.limit,
            subscription_ids=tuple(args.subscription_id),
            include_unsupported_leads=args.include_unsupported_leads,
        )
        print(f"cohort candidates: {preview.cohort_size}")
        print(f"preview fingerprint: {preview.fingerprint}")
        if preview.truncated:
            print(
                "more candidates remain beyond --limit; re-run until the cohort "
                "reaches zero"
            )
        for candidate in preview.candidates:
            previous = (
                candidate.current_next_billing_at.isoformat()
                if candidate.current_next_billing_at
                else "NULL"
            )
            print(
                f"  {candidate.subscription_id}  "
                f"{previous} -> "
                f"{candidate.coverage_end.isoformat()}  "
                + (
                    f"({candidate.drift.days:+d}d)"
                    if candidate.drift is not None
                    else "(exact entitlement evidence)"
                )
            )
        if not args.apply:
            print(
                "\nDRY RUN — nothing written. Review the cohort, then re-run "
                "with --apply --reviewed-sha256 <preview fingerprint>."
            )
            return
        if args.reviewed_sha256 != preview.fingerprint:
            parser.error("--reviewed-sha256 must equal the exact preview fingerprint")

        result = apply_stale_prepaid_billing_anchor_repair(
            db,
            preview,
            actor=args.actor,
            reason=args.reason,
        )
        print("\nAPPLIED")
        print(f"  scanned: {result.scanned}")
        print(f"  repaired: {result.repaired}")
        print(f"  already_correct: {result.already_correct}")
        print(f"  skipped_changed: {result.skipped_changed}")
        print(f"  replayed: {result.replayed}")

        remaining = preview_stale_prepaid_billing_anchor_repair(
            db,
            limit=args.limit,
            subscription_ids=tuple(args.subscription_id),
            include_unsupported_leads=args.include_unsupported_leads,
        )
        print(f"  remaining cohort (this page): {remaining.cohort_size}")
        if remaining.truncated or remaining.cohort_size:
            print("  re-run until the remaining cohort is zero")
    finally:
        db.close()


if __name__ == "__main__":
    main()
