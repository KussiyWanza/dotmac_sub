"""Repair prepaid billing anchors that lag their exact funded coverage.

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

This is NOT the same cohort as ``backfill_next_billing_at.py``, which repairs
NULL or historically-past anchors with no coverage evidence at all.

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
            "Advance billing anchor to exact funded entitlement coverage after "
            "the payment-allocation funding-change event gap"
        ),
        help="Why the repair is being run; recorded as durable audit evidence.",
    )
    args = parser.parse_args()

    db = SessionLocal()
    try:
        preview = preview_stale_prepaid_billing_anchor_repair(db, limit=args.limit)
        print(f"cohort candidates: {preview.cohort_size}")
        print(f"preview fingerprint: {preview.fingerprint}")
        if preview.truncated:
            print(
                "more candidates remain beyond --limit; re-run until the cohort "
                "reaches zero"
            )
        for candidate in preview.candidates[:20]:
            print(
                f"  {candidate.subscription_id}  "
                f"{candidate.current_next_billing_at.isoformat()} -> "
                f"{candidate.coverage_end.isoformat()}  "
                f"(+{candidate.drift.days}d)"
            )
        if len(preview.candidates) > 20:
            print(f"  ... {len(preview.candidates) - 20} more")

        if not args.apply:
            print("\nDRY RUN — nothing written. Re-run with --apply.")
            return

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

        remaining = preview_stale_prepaid_billing_anchor_repair(db, limit=args.limit)
        print(f"  remaining cohort (this page): {remaining.cohort_size}")
        if remaining.truncated or remaining.cohort_size:
            print("  re-run until the remaining cohort is zero")
    finally:
        db.close()


if __name__ == "__main__":
    main()
