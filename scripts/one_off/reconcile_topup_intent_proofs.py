#!/usr/bin/env python
"""Reconcile submitted direct-transfer intents whose exact proof is terminal.

Dry-run is the default. Apply mode invokes one owner-managed reconciliation
transaction per exact candidate and never creates, edits, allocates, or reverses
money.
"""

from __future__ import annotations

import argparse
import json
from uuid import NAMESPACE_URL, uuid5

from app.db import SessionLocal
from app.services import topup_intent_proof_reconciliation as reconciliation
from app.services.owner_commands import CommandContext


def _context(
    candidate: reconciliation.TopupIntentProofDriftCandidate,
) -> CommandContext:
    evidence_key = (
        f"topup-intent-proof-reconciliation:{candidate.intent_id}:"
        f"{candidate.proof_id}:{candidate.proof_status.value}"
    )
    command_id = uuid5(NAMESPACE_URL, evidence_key)
    return CommandContext.system(
        actor="system:topup-intent-proof-reconciliation",
        scope=reconciliation.RECONCILIATION_SCOPE,
        reason="Repair submitted intent from exact terminal payment-proof evidence",
        command_id=command_id,
        correlation_id=command_id,
        idempotency_key=evidence_key,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply canonical intent projections. Default: dry-run.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=500,
        help="Maximum exact candidates to inspect or apply (1-10000).",
    )
    args = parser.parse_args()

    preview_db = SessionLocal()
    try:
        candidates = reconciliation.inspect_terminal_proof_drift(
            preview_db,
            limit=args.limit,
        )
    finally:
        preview_db.close()

    counts = {action.value: 0 for action in reconciliation.TopupIntentProofRepairAction}
    for candidate in candidates:
        counts[candidate.action.value] += 1

    errors: list[dict[str, str]] = []
    changed = 0
    unchanged = 0
    result: dict[str, object] = {
        "mode": "apply" if args.apply else "dry_run",
        "candidate_count": len(candidates),
        "classification": counts,
        "changed": changed,
        "unchanged": unchanged,
        "requires_review": counts[
            reconciliation.TopupIntentProofRepairAction.requires_review.value
        ],
        "errors": errors,
    }
    if not args.apply:
        print(json.dumps(result, indent=2, default=str))
        return 0

    for candidate in candidates:
        if (
            candidate.action
            is reconciliation.TopupIntentProofRepairAction.requires_review
        ):
            continue
        db = SessionLocal()
        try:
            outcome = reconciliation.reconcile_terminal_proof(
                db,
                reconciliation.ReconcileTopupIntentProofCommand(
                    intent_id=candidate.intent_id,
                    proof_id=candidate.proof_id,
                    proof_status=candidate.proof_status,
                    payment_id=candidate.payment_id,
                ),
                context=_context(candidate),
            )
            if outcome.changed:
                changed += 1
            else:
                unchanged += 1
        except Exception as exc:
            errors.append(
                {
                    "intent_id": str(candidate.intent_id),
                    "proof_id": str(candidate.proof_id),
                    "error_type": type(exc).__name__,
                }
            )
        finally:
            db.close()

    result["changed"] = changed
    result["unchanged"] = unchanged
    verify_db = SessionLocal()
    try:
        remaining = reconciliation.inspect_terminal_proof_drift(
            verify_db,
            limit=args.limit,
        )
    finally:
        verify_db.close()
    result["remaining_candidate_count"] = len(remaining)
    print(json.dumps(result, indent=2, default=str))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
