#!/usr/bin/env python
"""Preview or apply repair for an affected prepaid funding-consequence case.

Preview is the default and writes nothing. Apply requires the exact
fingerprint the preview just returned -- the same fingerprint-bound
preview/apply pattern this codebase already uses elsewhere
(`reconcile_prepaid_drafts.py`, `preview_stale_prepaid_billing_anchor_repair`/
`apply_stale_prepaid_billing_anchor_repair`).

**Historical vs. current funding state (explicit decision, not a guess):**
this repair ALWAYS re-evaluates against the account's CURRENT funding
position, as of NOW -- never the historical event's effective time. The
review item / receipt identifies WHICH subscription+period needs another
look; it does not pre-authorize spending today's balance against a stale,
historical snapshot. The preview fingerprint is computed from CURRENT
classification (`classify_prospective_prepaid_funding`) at preview time, and
`--apply` must supply that exact fingerprint -- so a balance change between
preview and apply (e.g. the operator previews, funding drains for an
unrelated reason, then applies) is caught as a stale-fingerprint rejection by
the real entry point, not silently funded from money that arrived for a
different reason.

Re-runs the SAME classification/settlement code path production uses
(`financial.prepaid_service_renewals.execute_prepaid_service_after_settlement`)
for one exact case identified by the read-only census
(`census_prepaid_funding_consequence_gaps.py`) or a
`PrepaidDraftReconciliationException` review-item id -- never a second,
parallel decision implementation.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from uuid import UUID, uuid4

from app.models.catalog import Subscription
from app.models.prepaid_funding import PrepaidDraftReconciliationException
from app.models.system_user import SystemUser
from app.services.auth_dependencies import has_permission
from app.services.db_session_adapter import db_session_adapter
from app.services.owner_commands import CommandContext
from app.services.prepaid_draft_reconciliation import (
    REPAIR_SCOPE,
    classify_prospective_prepaid_funding,
    resolve_prepaid_draft_reconciliation_exception_for_owner,
)
from app.services.prepaid_service_renewals import (
    EvaluatePrepaidServiceAfterSettlementCommand,
    execute_prepaid_service_after_settlement,
    resolve_prepaid_monthly_charge_detail,
)
from app.services.system_user_assignments import system_user_role_names


def _uuid(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("identifier must be a UUID") from exc


def _resolve_repair_permission_granted(
    db, *, actor_system_user_id: UUID | None
) -> bool:
    """Check a real staff principal's granted roles, never a free-text actor.

    Mirrors `reconcile_prepaid_drafts.py`'s exact convention: `--actor` is
    only an audit label, never `auth_dependencies.user_role_names` (dead for
    this purpose), and a deactivated staff account never resolves.
    """

    if actor_system_user_id is None:
        return False
    system_user = db.get(SystemUser, actor_system_user_id)
    if system_user is None or not system_user.is_active:
        return False
    roles = system_user_role_names(db, actor_system_user_id)
    auth = {
        "principal_id": str(actor_system_user_id),
        "principal_type": "system_user",
        "roles": set(roles),
    }
    return has_permission(auth, db, REPAIR_SCOPE)


def _resolve_review_item(
    db, review_item_id: UUID
) -> PrepaidDraftReconciliationException:
    review_item = db.get(PrepaidDraftReconciliationException, review_item_id)
    if review_item is None:
        raise SystemExit(f"review item {review_item_id} not found")
    return review_item


#: Disposition strings that mean "a real settlement actually happened for
#: this one subscription" -- see `PrepaidFundingSubscriptionDecision.
#: disposition` write sites in `app.services.prepaid_service_renewals` /
#: `app.services.prepaid_draft_reconciliation`.
_SETTLED_DECISION_DISPOSITIONS = frozenset(
    {"created_canonical_renewal", "existing_draft_settled"}
)


def matching_settled_decision(evaluation, *, subscription_id: UUID):
    """Find THIS repair's own evidence that subscription_id was settled.

    Pure/no I/O so it can be tested directly with a fake evaluation object
    (2026-09, round 8): this repair path deliberately skips the trigger
    receipt (`skip_receipt_for_repair=True`), so there is no receipt-child
    row to check afterward -- the in-memory `subscription_decisions` the
    settlement call just returned is the ONLY evidence available. Returns
    `None` when no matching, genuinely-settled decision exists -- the
    caller must not resolve the review item in that case.
    """

    renewal = evaluation.renewal
    return next(
        (
            decision
            for decision in (renewal.subscription_decisions if renewal else ())
            if decision.subscription_id == subscription_id
            and decision.disposition in _SETTLED_DECISION_DISPOSITIONS
        ),
        None,
    )


def preview_repair(
    db, review_item: PrepaidDraftReconciliationException
) -> dict[str, object]:
    """Read-only: classify CURRENT funding for the review item's account/currency.

    Returns a fingerprint binding exactly what `--apply` must reproduce:
    the review item id, PERIOD, CHARGE/TAX, and the current classification's
    PAYMENT and OPENING-FUNDING identity (2026-09, round 7) -- not just the
    review item id and a coarse funding-shape hash. The narrower fingerprint
    let a stale or drifted preview match and resolve the WRONG evidence: two
    different subscription/period cases on the same account/currency with
    the same required amount could classify to the identical disposition and
    funding-fingerprint hash, and the old fingerprint could not tell them
    apart, or notice that funding had moved from one payment/opening
    position to a different one carrying the same coarse shape.
    """

    if review_item.subscription_id is None or review_item.period_start is None:
        raise SystemExit(
            "review item has no subscription/period evidence -- this repair "
            "path only handles the funding-consequence owner's own "
            "pre-mutation ambiguous classifications, not draft-reconciliation "
            "review items raised by other paths"
        )
    classification = classify_prospective_prepaid_funding(
        db,
        account_id=review_item.account_id,
        currency=review_item.currency,
        amount=review_item.required_amount,
    )
    subscription = db.get(Subscription, review_item.subscription_id)
    if subscription is None:
        raise SystemExit(
            f"subscription {review_item.subscription_id} referenced by this "
            "review item no longer exists"
        )
    charge = resolve_prepaid_monthly_charge_detail(db, subscription, datetime.now(UTC))
    if charge is None:
        raise SystemExit(
            "current charge could not be resolved for this subscription -- "
            "preview again once a contract price is available"
        )
    import hashlib

    fingerprint = hashlib.sha256(
        (
            f"repair:{review_item.id}:"
            f"{review_item.subscription_id}:"
            f"{review_item.period_start.isoformat()}:"
            f"{review_item.period_end.isoformat() if review_item.period_end else ''}:"
            f"{classification.disposition.value}:"
            f"{classification.funding.fingerprint}:"
            f"{charge.subtotal}:{charge.tax_total}:{charge.total}:"
            f"{','.join(str(value) for value in sorted(classification.funding.source_payment_ids))}:"
            f"{classification.opening.baseline_id}:"
            f"{classification.opening.opening_position_id}"
        ).encode()
    ).hexdigest()
    return {
        "review_item_id": str(review_item.id),
        "account_id": str(review_item.account_id),
        "subscription_id": str(review_item.subscription_id),
        "period_start": review_item.period_start.isoformat(),
        "period_end": (
            review_item.period_end.isoformat() if review_item.period_end else None
        ),
        "currency": review_item.currency,
        "required_amount": str(review_item.required_amount),
        "current_disposition": classification.disposition.value,
        "current_recommended_action": classification.recommended_action.value,
        "fingerprint": fingerprint,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--review-item-id", type=_uuid, required=True)
    parser.add_argument("--payment-id", type=_uuid, help="Required with --apply.")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--fingerprint", help="Required with --apply.")
    parser.add_argument("--idempotency-key")
    parser.add_argument("--actor")
    parser.add_argument("--actor-system-user-id", type=_uuid)
    parser.add_argument("--reason")
    args = parser.parse_args()

    if args.apply:
        required = [
            ("--fingerprint", args.fingerprint),
            ("--payment-id", args.payment_id),
            ("--idempotency-key", args.idempotency_key),
            ("--actor", args.actor),
            ("--actor-system-user-id", args.actor_system_user_id),
            ("--reason", args.reason),
        ]
        missing = [name for name, value in required if not value]
        if missing:
            parser.error("--apply requires " + ", ".join(missing))

        with db_session_adapter.owner_command_session() as db:
            if not _resolve_repair_permission_granted(
                db, actor_system_user_id=args.actor_system_user_id
            ):
                db_session_adapter.release_read_transaction(db)
                print(f"Permission denied: {REPAIR_SCOPE} is required.")
                return 1
            review_item = _resolve_review_item(db, args.review_item_id)
            current = preview_repair(db, review_item)
            if current["fingerprint"] != args.fingerprint:
                db_session_adapter.release_read_transaction(db)
                print(
                    "Stale fingerprint: current funding classification has "
                    "changed since this fingerprint was previewed. Preview "
                    "again before applying."
                )
                return 1
            review_item_id = review_item.id
            review_item_account_id = review_item.account_id
            # `preview_repair` above already refused (`SystemExit`) a review
            # item with no subscription evidence -- asserted here only to
            # keep the type checker honest about that cross-function
            # narrowing.
            assert review_item.subscription_id is not None
            review_item_subscription_id = review_item.subscription_id
            command = EvaluatePrepaidServiceAfterSettlementCommand(
                context=CommandContext.system(
                    actor=f"{args.actor}:{args.actor_system_user_id}",
                    scope=REPAIR_SCOPE,
                    reason=args.reason,
                    command_id=uuid4(),
                    idempotency_key=args.idempotency_key,
                ),
                account_id=review_item_account_id,
                payment_id=args.payment_id,
                evidence_ref=(
                    f"repair_prepaid_funding_consequences:{review_item_id}:"
                    f"{args.idempotency_key}"
                ),
                # No `event_id`: a repair is a fresh, current-state
                # evaluation, not a replay of the original (possibly
                # historical) event -- it deliberately does not touch the
                # `PrepaidFundingTriggerExecution` receipt for the original
                # event. `skip_receipt_for_repair=True` makes that omission
                # an explicit, named decision rather than a silent
                # fallthrough (2026-09, round 7) -- any OTHER caller that
                # omits `event_id` without this flag is refused.
                #
                # `only_subscription_id`: the fingerprint above was computed
                # for exactly this review item's one subscription/period.
                # Without this, `apply_due_prepaid_service_after_funding_change`
                # would rescan and apply against EVERY due subscription on
                # the account -- a broader scope than what was previewed and
                # fingerprint-gated.
                only_subscription_id=review_item_subscription_id,
                skip_receipt_for_repair=True,
            )
            # `execute_owner_command` (inside `execute_prepaid_service_after_
            # settlement`) requires a transaction-free session at entry. The
            # permission check and the preview SELECTs above left this
            # session mid-transaction -- release that read-only transaction
            # before entering the owner-command boundary, exactly like
            # `reconcile_prepaid_drafts.py`'s `--repair-paid-invoice` path
            # does for the identical reason.
            db_session_adapter.release_read_transaction(db)
            result = execute_prepaid_service_after_settlement(db, command)
            # Prove a real, matching consequence actually happened for THIS
            # exact subscription/period before marking the review item
            # resolved (2026-09, round 7) -- an unconditional resolve
            # previously trusted the call not raising as proof enough.
            if (
                matching_settled_decision(
                    result, subscription_id=review_item_subscription_id
                )
                is None
            ):
                # `execute_owner_command` already committed the settlement
                # itself on success (this is NOT a rollback of that money --
                # there is nothing left to roll back by this point). What's
                # refused here is narrower: this repair simply does not mark
                # the review item resolved, since it cannot prove a matching
                # settled decision exists for this exact subscription.
                print(
                    "Repair executed but produced no matching settled outcome "
                    f"for subscription {review_item_subscription_id} -- the "
                    "review item was NOT resolved. Re-preview and investigate "
                    f"before retrying. Raw result: {result}"
                )
                return 1
            resolve_prepaid_draft_reconciliation_exception_for_owner(db, review_item_id)
            db.commit()
            print(
                "Repair driven through the real-time funding-consequence "
                f"owner for account {review_item_account_id}: {result}"
            )
        return 0

    with db_session_adapter.read_session() as db:
        try:
            review_item = _resolve_review_item(db, args.review_item_id)
            preview = preview_repair(db, review_item)
        finally:
            db_session_adapter.release_read_transaction(db)
    print(
        "Preview (writes nothing). Current funding classification: "
        f"{preview['current_disposition']} "
        f"({preview['current_recommended_action']}). "
        f"Re-run with --apply --fingerprint {preview['fingerprint']} "
        "--payment-id <current settling payment> --idempotency-key <key> "
        "--actor <name> --actor-system-user-id <id> --reason <reason> "
        "to apply -- this fingerprint expires the moment current funding "
        "changes."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
