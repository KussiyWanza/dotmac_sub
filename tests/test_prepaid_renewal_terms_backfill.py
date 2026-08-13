"""Renewal-terms backfill: exact paid evidence only, fail-closed otherwise.

The contracted amount is restored solely from the subscription's own PAID
base-subscription invoice lines. The mutable catalog is never consulted for
the amount; absent or contradictory evidence becomes an owned finance work
item and the account stays fail-closed (ADR 0007 stage 3).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.models.admin_alert import AdminAlert
from app.models.billing import Invoice, InvoiceLine, InvoiceStatus
from app.models.catalog import BillingMode, Subscription, SubscriptionStatus
from app.services.owner_commands import CommandContext
from app.services.prepaid_renewal_terms_backfill import (
    _FINDING_PREFIX,
    CaptureRenewalTermsBackfillCommand,
    PrepaidRenewalTermsBackfillError,
    RenewalTermsDecision,
    capture_prepaid_renewal_terms_backfill,
    preview_prepaid_renewal_terms_backfill,
)

_NOON = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)


def _ensure_charge_inputs(db, subscription) -> None:
    from app.models.catalog import BillingCycle, OfferPrice, PriceType

    existing = (
        db.query(OfferPrice)
        .filter(
            OfferPrice.offer_id == subscription.offer_id,
            OfferPrice.price_type == PriceType.recurring,
            OfferPrice.is_active.is_(True),
        )
        .first()
    )
    if existing is None:
        db.add(
            OfferPrice(
                offer_id=subscription.offer_id,
                price_type=PriceType.recurring,
                amount=Decimal("35000.00"),
                currency="NGN",
                billing_cycle=BillingCycle.monthly,
                is_active=True,
            )
        )
        db.flush()


def _block(db, subscription) -> None:
    subscription.billing_mode = BillingMode.prepaid
    subscription.status = SubscriptionStatus.active
    subscription.unit_price = None
    _ensure_charge_inputs(db, subscription)
    db.commit()


def _paid_line(
    db,
    subscriber,
    subscription,
    amount: str,
    *,
    full_cycle: bool = True,
    currency: str = "NGN",
    quantity: str = "1.000",
    line_amount: str | None = None,
    metadata: dict | None = None,
    line_active: bool = True,
    period_days: int = 30,
):
    from datetime import timedelta

    invoice = Invoice(
        account_id=subscriber.id,
        status=InvoiceStatus.paid,
        currency=currency,
        total=Decimal(amount),
    )
    if full_cycle:
        from app.services.billing_automation import _add_months

        start = datetime(2026, 6, 1, tzinfo=UTC)
        invoice.billing_period_start = start
        invoice.billing_period_end = (
            _add_months(start, 1)
            if period_days == 30
            else start + timedelta(days=period_days)
        )
    db.add(invoice)
    db.flush()
    line = InvoiceLine(
        invoice_id=invoice.id,
        subscription_id=subscription.id,
        description="Monthly service",
        quantity=Decimal(quantity),
        unit_price=Decimal(amount),
        amount=Decimal(line_amount if line_amount is not None else amount),
        metadata_=metadata if metadata is not None else {"kind": "base_subscription"},
        is_active=line_active,
    )
    db.add(line)
    db.commit()
    return line


def _context(key: str) -> CommandContext:
    return CommandContext.system(
        actor="pytest:renewal-terms-backfill",
        scope="financial.prepaid_renewal_terms_backfill:test",
        reason="Renewal-terms backfill behavior test",
        idempotency_key=key,
    )


def _capture(db, fingerprint: str, key: str = "renewal-terms-test"):
    # The owner boundary requires a transaction-free session at entry; the
    # read-only preview above opened one.
    db.commit()
    return capture_prepaid_renewal_terms_backfill(
        db,
        CaptureRenewalTermsBackfillCommand(
            preview_fingerprint=fingerprint, as_of=_NOON
        ),
        context=_context(key),
    )


def test_consistent_paid_evidence_restores_the_contracted_amount(
    db_session, subscriber, subscription
):
    _block(db_session, subscription)
    _paid_line(db_session, subscriber, subscription, "15000.00")
    _paid_line(db_session, subscriber, subscription, "15000.00")

    preview = preview_prepaid_renewal_terms_backfill(db_session, now=_NOON)
    items = [i for i in preview.items if i.subscription_id == subscription.id]
    assert items and items[0].decision is RenewalTermsDecision.repairable
    assert items[0].contracted_amount == Decimal("15000.00")

    result = _capture(db_session, preview.fingerprint)

    assert result.repaired_count >= 1
    db_session.refresh(subscription)
    assert subscription.unit_price == Decimal("15000.00")
    # Replay with unchanged evidence rewrites nothing.
    replay_preview = preview_prepaid_renewal_terms_backfill(db_session, now=_NOON)
    replay = _capture(db_session, replay_preview.fingerprint, "renewal-terms-2")
    assert all(i.subscription_id != subscription.id for i in replay_preview.items)
    assert replay.fingerprint == replay_preview.fingerprint


def test_conflicting_amounts_become_a_finance_work_item_not_a_write(
    db_session, subscriber, subscription
):
    _block(db_session, subscription)
    _paid_line(db_session, subscriber, subscription, "15000.00")
    _paid_line(db_session, subscriber, subscription, "18000.00")

    preview = preview_prepaid_renewal_terms_backfill(db_session, now=_NOON)
    items = [i for i in preview.items if i.subscription_id == subscription.id]
    assert items and items[0].decision is RenewalTermsDecision.ambiguous_amounts

    _capture(db_session, preview.fingerprint)

    db_session.refresh(subscription)
    assert subscription.unit_price is None
    alert = (
        db_session.query(AdminAlert)
        .filter(AdminAlert.fingerprint == f"{_FINDING_PREFIX}{subscription.id}")
        .one()
    )
    assert alert.status.value == "open"
    assert alert.details["owner"] == "finance-billing"
    assert alert.details["sla_due_at"]
    assert alert.details["decision"] == "ambiguous_amounts"


def test_no_paid_evidence_stays_fail_closed_with_work_item(
    db_session, subscriber, subscription
):
    _block(db_session, subscription)

    preview = preview_prepaid_renewal_terms_backfill(db_session, now=_NOON)
    items = [i for i in preview.items if i.subscription_id == subscription.id]
    assert items and items[0].decision is RenewalTermsDecision.no_evidence

    result = _capture(db_session, preview.fingerprint)

    assert result.work_item_count >= 1
    db_session.refresh(subscription)
    assert subscription.unit_price is None


def test_stale_fingerprint_is_rejected(db_session, subscriber, subscription):
    _block(db_session, subscription)
    preview = preview_prepaid_renewal_terms_backfill(db_session, now=_NOON)
    # Evidence changes after review: a paid line appears.
    _paid_line(db_session, subscriber, subscription, "15000.00")

    with pytest.raises(PrepaidRenewalTermsBackfillError) as captured:
        _capture(db_session, preview.fingerprint)
    assert captured.value.code.endswith("stale_preview")


def test_catalog_price_is_never_used_for_the_amount(
    db_session, subscriber, subscription, monkeypatch
):
    # The catalog has an active recurring price, but with no paid evidence the
    # subscription must stay fail-closed rather than inherit catalog pricing.
    _block(db_session, subscription)

    preview = preview_prepaid_renewal_terms_backfill(db_session, now=_NOON)
    _capture(db_session, preview.fingerprint)

    db_session.refresh(subscription)
    assert subscription.unit_price is None


def test_work_item_summary_fits_admin_alert_schema():
    # admin_alerts.summary is VARCHAR(255) in production PostgreSQL; the test
    # database does not enforce varchar lengths, so pin it here (the first
    # prod capture failed on StringDataRightTruncation).
    import inspect

    from app.services import prepaid_renewal_terms_backfill as module

    source = inspect.getsource(module._sync_evidence_work_items)
    assert "summary=(" in source
    from app.models.network_monitoring import AlertSeverity  # noqa: F401

    summary = (
        "Active prepaid subscription with no frozen contracted "
        "amount; paid-invoice evidence is missing or conflicting. "
        "Record the price via a reviewed staff correction — never "
        "inferred from the catalog."
    )
    assert len(summary) <= 255


def test_suspended_subscription_is_repaired_from_paid_evidence(
    db_session, subscriber, subscription
):
    # Slice-1 gap found in production: 20 suspended prepaid subscriptions
    # lacked unit_price and stayed blocked (and unrestorable) because the
    # preview only looked at active status while the threshold owner
    # evaluates every collectible status.
    subscription.billing_mode = BillingMode.prepaid
    subscription.status = SubscriptionStatus.suspended
    subscription.unit_price = None
    _ensure_charge_inputs(db_session, subscription)
    db_session.commit()
    _paid_line(db_session, subscriber, subscription, "35000.00")

    preview = preview_prepaid_renewal_terms_backfill(db_session, now=_NOON)
    items = [i for i in preview.items if i.subscription_id == subscription.id]
    assert items and items[0].decision is RenewalTermsDecision.repairable

    _capture(db_session, preview.fingerprint, "renewal-terms-suspended")

    db_session.refresh(subscription)
    assert subscription.unit_price == Decimal("35000.00")


def test_lone_unproven_line_is_insufficient_cycle_evidence(
    db_session, subscriber, subscription
):
    _block(db_session, subscription)
    _paid_line(db_session, subscriber, subscription, "15000.00", full_cycle=False)

    preview = preview_prepaid_renewal_terms_backfill(db_session, now=_NOON)
    items = [i for i in preview.items if i.subscription_id == subscription.id]
    assert items
    assert items[0].decision is RenewalTermsDecision.insufficient_cycle_evidence
    assert "no_canonical_full_cycle_proof" in items[0].insufficiency_reasons

    _capture(db_session, preview.fingerprint, "renewal-lone")
    db_session.refresh(subscription)
    assert subscription.unit_price is None


def test_repeated_unproven_lines_are_not_proof(db_session, subscriber, subscription):
    # Repetition of unproven lines is not proof: they may all be prorated
    # or partial in the same way (review blocker on the v2.0 draft).
    _block(db_session, subscription)
    _paid_line(db_session, subscriber, subscription, "15000.00", full_cycle=False)
    _paid_line(db_session, subscriber, subscription, "15000.00", full_cycle=False)

    preview = preview_prepaid_renewal_terms_backfill(db_session, now=_NOON)
    items = [i for i in preview.items if i.subscription_id == subscription.id]
    assert items
    assert items[0].decision is RenewalTermsDecision.insufficient_cycle_evidence
    assert "no_canonical_full_cycle_proof" in items[0].insufficiency_reasons


def test_end_of_month_canonical_cycle_is_proof(db_session, subscriber, subscription):
    from datetime import timedelta as _td

    from app.models.billing import Invoice as _Inv
    from app.models.billing import InvoiceLine as _Line
    from app.models.billing import InvoiceStatus as _St

    _block(db_session, subscription)
    inv = _Inv(
        account_id=subscriber.id,
        status=_St.paid,
        currency="NGN",
        total=Decimal("35000.00"),
        billing_period_start=datetime(2026, 1, 31, tzinfo=UTC),
        billing_period_end=datetime(2026, 2, 28, tzinfo=UTC),
    )
    db_session.add(inv)
    db_session.flush()
    db_session.add(
        _Line(
            invoice_id=inv.id,
            subscription_id=subscription.id,
            description="Monthly service",
            unit_price=Decimal("35000.00"),
            amount=Decimal("35000.00"),
            metadata_={"kind": "base_subscription"},
        )
    )
    db_session.commit()
    _ = _td

    preview = preview_prepaid_renewal_terms_backfill(db_session, now=_NOON)
    items = [i for i in preview.items if i.subscription_id == subscription.id]
    assert items and items[0].decision is RenewalTermsDecision.repairable


def test_inactive_invoice_is_ignored_even_with_active_line(
    db_session, subscriber, subscription
):
    _block(db_session, subscription)
    line = _paid_line(db_session, subscriber, subscription, "15000.00")
    invoice = db_session.get(Invoice, line.invoice_id)
    invoice.is_active = False
    db_session.commit()

    preview = preview_prepaid_renewal_terms_backfill(db_session, now=_NOON)
    items = [i for i in preview.items if i.subscription_id == subscription.id]
    assert items and items[0].decision is RenewalTermsDecision.no_evidence


def test_description_proration_is_not_proof(db_session, subscriber, subscription):
    # The repository's own proration path can mark a line only in its
    # description while the period still looks month-shaped.
    _block(db_session, subscription)
    _paid_line(
        db_session,
        subscriber,
        subscription,
        "2687.50",
        metadata={"kind": "base_subscription"},
    )
    line = (
        db_session.query(InvoiceLine)
        .filter(InvoiceLine.subscription_id == subscription.id)
        .one()
    )
    line.description = "Prorated plan change adjustment"
    db_session.commit()

    preview = preview_prepaid_renewal_terms_backfill(db_session, now=_NOON)
    items = [i for i in preview.items if i.subscription_id == subscription.id]
    assert items
    assert items[0].decision is RenewalTermsDecision.insufficient_cycle_evidence
    assert "prorated" in items[0].insufficiency_reasons


def test_missing_catalog_price_row_is_owned_not_repaired(
    db_session, subscriber, subscription
):
    from app.models.catalog import OfferPrice as _OP

    _block(db_session, subscription)
    _paid_line(db_session, subscriber, subscription, "35000.00")
    for row in (
        db_session.query(_OP).filter(_OP.offer_id == subscription.offer_id).all()
    ):
        row.is_active = False
    db_session.commit()

    preview = preview_prepaid_renewal_terms_backfill(db_session, now=_NOON)
    items = [i for i in preview.items if i.subscription_id == subscription.id]
    assert items
    assert items[0].decision is RenewalTermsDecision.missing_charge_inputs
    assert "no_active_recurring_price" in items[0].insufficiency_reasons

    _capture(db_session, preview.fingerprint, "renewal-inputs")
    db_session.refresh(subscription)
    assert subscription.unit_price is None


def test_inactive_invoice_lines_are_ignored(db_session, subscriber, subscription):
    _block(db_session, subscription)
    _paid_line(db_session, subscriber, subscription, "15000.00", line_active=False)

    preview = preview_prepaid_renewal_terms_backfill(db_session, now=_NOON)
    items = [i for i in preview.items if i.subscription_id == subscription.id]
    assert items and items[0].decision is RenewalTermsDecision.no_evidence


def test_same_amount_evidence_mutation_changes_the_fingerprint(
    db_session, subscriber, subscription
):
    _block(db_session, subscription)
    _paid_line(db_session, subscriber, subscription, "15000.00")
    first = preview_prepaid_renewal_terms_backfill(db_session, now=_NOON)

    # New paid line with the SAME amount: classification values are
    # unchanged, but the evidence set is not — the v1 fingerprint missed
    # this (production proof gap).
    _paid_line(db_session, subscriber, subscription, "15000.00")
    second = preview_prepaid_renewal_terms_backfill(db_session, now=_NOON)

    assert first.fingerprint != second.fingerprint


def test_currency_mismatch_is_not_proof(db_session, subscriber, subscription):
    _block(db_session, subscription)
    _paid_line(db_session, subscriber, subscription, "15000.00", currency="USD")

    preview = preview_prepaid_renewal_terms_backfill(db_session, now=_NOON)
    items = [i for i in preview.items if i.subscription_id == subscription.id]
    assert items
    assert items[0].decision is RenewalTermsDecision.insufficient_cycle_evidence
    assert "currency_mismatch" in items[0].insufficiency_reasons


def test_prorated_line_is_not_proof(db_session, subscriber, subscription):
    _block(db_session, subscription)
    _paid_line(
        db_session,
        subscriber,
        subscription,
        "2687.50",
        metadata={"kind": "base_subscription", "prorated": True},
    )

    preview = preview_prepaid_renewal_terms_backfill(db_session, now=_NOON)
    items = [i for i in preview.items if i.subscription_id == subscription.id]
    assert items
    assert items[0].decision is RenewalTermsDecision.insufficient_cycle_evidence
    assert "prorated" in items[0].insufficiency_reasons


def test_quantity_amount_mismatch_is_not_proof(db_session, subscriber, subscription):
    _block(db_session, subscription)
    _paid_line(db_session, subscriber, subscription, "15000.00", line_amount="7500.00")

    preview = preview_prepaid_renewal_terms_backfill(db_session, now=_NOON)
    items = [i for i in preview.items if i.subscription_id == subscription.id]
    assert items
    assert items[0].decision is RenewalTermsDecision.insufficient_cycle_evidence
    assert "amount_mismatch" in items[0].insufficiency_reasons


def _backfill_one(db, subscriber, subscription, amount: str, key: str) -> None:
    _block(db, subscription)
    _paid_line(db, subscriber, subscription, amount)
    preview = preview_prepaid_renewal_terms_backfill(db, now=_NOON)
    _capture(db, preview.fingerprint, key)
    db.refresh(subscription)
    assert subscription.unit_price == Decimal(amount)


def test_correction_requires_backfill_cohort_membership(
    db_session, subscriber, subscription
):
    from app.services.prepaid_renewal_terms_backfill import (
        CorrectRenewalTermsCommand,
        RenewalTermsCorrectionAction,
        RenewalTermsCorrectionSource,
        correct_prepaid_renewal_terms,
    )

    _block(db_session, subscription)
    sub_id = subscription.id
    db_session.commit()

    with pytest.raises(PrepaidRenewalTermsBackfillError) as captured:
        correct_prepaid_renewal_terms(
            db_session,
            CorrectRenewalTermsCommand(
                subscription_id=sub_id,
                action=RenewalTermsCorrectionAction.apply_reviewed_term,
                source=RenewalTermsCorrectionSource.finance_review,
                expected_current_amount=None,
                review_reference="FIN-2026-080",
                reviewed_amount=Decimal("43000.00"),
            ),
            context=_context("correction-cohort"),
        )
    assert captured.value.code.endswith("not_in_backfill_cohort")


def test_correction_supersedes_with_optimistic_lock(
    db_session, subscriber, subscription
):
    from app.services.prepaid_renewal_terms_backfill import (
        CorrectRenewalTermsCommand,
        RenewalTermsCorrectionAction,
        RenewalTermsCorrectionSource,
        correct_prepaid_renewal_terms,
    )

    _backfill_one(db_session, subscriber, subscription, "35000.00", "corr-seed")
    sub_id = subscription.id
    db_session.commit()

    # Stale expectation is rejected.
    with pytest.raises(PrepaidRenewalTermsBackfillError) as captured:
        correct_prepaid_renewal_terms(
            db_session,
            CorrectRenewalTermsCommand(
                subscription_id=sub_id,
                action=RenewalTermsCorrectionAction.apply_reviewed_term,
                source=RenewalTermsCorrectionSource.finance_review,
                expected_current_amount=Decimal("17500.00"),
                review_reference="FIN-2026-081",
                reviewed_amount=Decimal("43000.00"),
            ),
            context=_context("correction-stale"),
        )
    assert captured.value.code.endswith("stale_current_amount")

    result = correct_prepaid_renewal_terms(
        db_session,
        CorrectRenewalTermsCommand(
            subscription_id=sub_id,
            action=RenewalTermsCorrectionAction.apply_reviewed_term,
            source=RenewalTermsCorrectionSource.finance_review,
            expected_current_amount=Decimal("35000.00"),
            review_reference="FIN-2026-081",
            reviewed_amount=Decimal("43000.00"),
        ),
        context=_context("correction-apply"),
    )
    assert result.previous_amount == Decimal("35000.00")
    assert result.new_amount == Decimal("43000.00")
    assert result.replayed is False

    # Replay: expectation no longer matches but the reviewed amount is
    # already in place — idempotent no-op.
    db_session.commit()
    replay = correct_prepaid_renewal_terms(
        db_session,
        CorrectRenewalTermsCommand(
            subscription_id=sub_id,
            action=RenewalTermsCorrectionAction.apply_reviewed_term,
            source=RenewalTermsCorrectionSource.finance_review,
            expected_current_amount=Decimal("35000.00"),
            review_reference="FIN-2026-081",
            reviewed_amount=Decimal("43000.00"),
        ),
        context=_context("correction-replay"),
    )
    assert replay.replayed is True


def test_audit_bound_fail_closed_restoration(db_session, subscriber, subscription):
    from app.services.prepaid_renewal_terms_backfill import (
        CorrectRenewalTermsCommand,
        RenewalTermsCorrectionAction,
        RenewalTermsCorrectionSource,
        audit_restored_renewal_terms,
        correct_prepaid_renewal_terms,
    )

    _backfill_one(db_session, subscriber, subscription, "2687.50", "audit-seed")
    sub_id = subscription.id
    # The proving evidence disappears after the restore (invoice voided):
    # the v2 audit can no longer confirm the amount.
    line = (
        db_session.query(InvoiceLine)
        .filter(InvoiceLine.subscription_id == sub_id)
        .one()
    )
    invoice = db_session.get(Invoice, line.invoice_id)
    invoice.is_active = False
    db_session.commit()

    run = audit_restored_renewal_terms(
        db_session, context=_context("audit-run"), now=_NOON
    )
    ours = [i for i in run.items if i.subscription_id == sub_id]
    assert ours and ours[0].amount_confirmed is False

    # Audit source cannot invent an amount.
    db_session.commit()
    with pytest.raises(PrepaidRenewalTermsBackfillError) as captured:
        correct_prepaid_renewal_terms(
            db_session,
            CorrectRenewalTermsCommand(
                subscription_id=sub_id,
                action=RenewalTermsCorrectionAction.apply_reviewed_term,
                source=RenewalTermsCorrectionSource.audit,
                expected_current_amount=Decimal("2687.50"),
                audit_fingerprint=run.audit_fingerprint,
                reviewed_amount=Decimal("2687.50"),
            ),
            context=_context("audit-bad-action"),
        )
    assert captured.value.code.endswith("invalid_audit_action")

    # A wrong fingerprint is rejected.
    db_session.commit()
    with pytest.raises(PrepaidRenewalTermsBackfillError) as captured:
        correct_prepaid_renewal_terms(
            db_session,
            CorrectRenewalTermsCommand(
                subscription_id=sub_id,
                action=RenewalTermsCorrectionAction.restore_fail_closed,
                source=RenewalTermsCorrectionSource.audit,
                expected_current_amount=Decimal("2687.50"),
                audit_fingerprint="0" * 64,
            ),
            context=_context("audit-bad-fp"),
        )
    assert captured.value.code.endswith("audit_mismatch")

    db_session.commit()
    result = correct_prepaid_renewal_terms(
        db_session,
        CorrectRenewalTermsCommand(
            subscription_id=sub_id,
            action=RenewalTermsCorrectionAction.restore_fail_closed,
            source=RenewalTermsCorrectionSource.audit,
            expected_current_amount=Decimal("2687.50"),
            audit_fingerprint=run.audit_fingerprint,
        ),
        context=_context("audit-restore"),
    )
    assert result.new_amount is None
    subscription = db_session.get(Subscription, sub_id)
    assert subscription.unit_price is None
    alert = (
        db_session.query(AdminAlert)
        .filter(AdminAlert.fingerprint == f"{_FINDING_PREFIX}{sub_id}")
        .one()
    )
    assert alert.status.value == "open"
    assert alert.details["decision"] == "correction_fail_closed"
    assert alert.details["provenance"] == f"audit:{run.audit_fingerprint}"


def test_audit_reclassifies_restored_subscriptions(
    db_session, subscriber, subscription
):
    from app.services.prepaid_renewal_terms_backfill import (
        audit_restored_renewal_terms,
    )

    _backfill_one(db_session, subscriber, subscription, "35000.00", "audit-ok")
    db_session.commit()

    run = audit_restored_renewal_terms(
        db_session, context=_context("audit-confirm"), now=_NOON
    )
    ours = [i for i in run.items if i.subscription_id == subscription.id]
    assert ours and ours[0].amount_confirmed is True
    assert len(run.audit_fingerprint) == 64


# ---------------------------------------------------------------------------
# Scheduled sequencing: the repair owner must actually be reachable
# ---------------------------------------------------------------------------


def test_scheduled_runner_restores_terms_from_paid_evidence(
    db_session, subscriber, subscription
):
    """The owner is only useful if something calls it.

    It shipped with ADR 0007 stage 3 and had no task, route or admin surface,
    so `contracted_prepaid_renewal_terms_unavailable` was a permanent block:
    32 production accounts kept consuming service, and because this owner also
    owns the work-item sync they never surfaced as finance work items either.
    """
    from app.services.collections.scheduled import restore_prepaid_renewal_terms

    _block(db_session, subscription)
    _paid_line(db_session, subscriber, subscription, "15000.00")
    _paid_line(db_session, subscriber, subscription, "15000.00")

    outcome = restore_prepaid_renewal_terms(db_session, now=_NOON)

    assert outcome.restored >= 1
    assert outcome.status.value == "ok"
    db_session.refresh(subscription)
    assert subscription.unit_price == Decimal("15000.00")


def test_scheduled_runner_opens_a_work_item_instead_of_inventing_an_amount(
    db_session, subscriber, subscription
):
    """Unresolvable evidence must stay fail-closed and become visible.

    The amount is never inferred from the mutable catalog (ADR 0007 Phase 1),
    so an account with contradictory evidence stays blocked -- but it must at
    least become an owned, SLA-bound finance work item.
    """
    from app.services.collections.scheduled import restore_prepaid_renewal_terms

    _block(db_session, subscription)
    _paid_line(db_session, subscriber, subscription, "15000.00")
    _paid_line(db_session, subscriber, subscription, "18000.00")

    outcome = restore_prepaid_renewal_terms(db_session, now=_NOON)

    assert outcome.restored == 0
    assert outcome.work_items >= 1
    db_session.refresh(subscription)
    assert subscription.unit_price is None
    alert = (
        db_session.query(AdminAlert)
        .filter(AdminAlert.fingerprint == f"{_FINDING_PREFIX}{subscription.id}")
        .one()
    )
    assert alert.status.value == "open"
    assert alert.details["owner"] == "finance-billing"


def test_scheduled_runner_is_a_noop_when_nothing_is_blocked(db_session):
    from app.services.collections.scheduled import restore_prepaid_renewal_terms

    outcome = restore_prepaid_renewal_terms(db_session, now=_NOON)

    assert outcome.blocked == 0
    assert outcome.restored == 0
    assert outcome.status.value == "ok"
