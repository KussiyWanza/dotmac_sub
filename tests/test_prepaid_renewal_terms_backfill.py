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
from app.models.catalog import BillingMode, SubscriptionStatus
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


def _block(db, subscription) -> None:
    subscription.billing_mode = BillingMode.prepaid
    subscription.status = SubscriptionStatus.active
    subscription.unit_price = None
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
        invoice.billing_period_start = datetime(2026, 6, 1, tzinfo=UTC)
        invoice.billing_period_end = datetime(2026, 6, 1, tzinfo=UTC) + timedelta(
            days=period_days
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
    assert "lone_line_without_full_cycle_proof" in items[0].insufficiency_reasons

    _capture(db_session, preview.fingerprint, "renewal-lone")
    db_session.refresh(subscription)
    assert subscription.unit_price is None


def test_repeated_compatible_lines_are_repairable_without_period_proof(
    db_session, subscriber, subscription
):
    _block(db_session, subscription)
    _paid_line(db_session, subscriber, subscription, "15000.00", full_cycle=False)
    _paid_line(db_session, subscriber, subscription, "15000.00", full_cycle=False)

    preview = preview_prepaid_renewal_terms_backfill(db_session, now=_NOON)
    items = [i for i in preview.items if i.subscription_id == subscription.id]
    assert items and items[0].decision is RenewalTermsDecision.repairable


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


def test_correction_supersedes_and_replays_idempotently(
    db_session, subscriber, subscription
):
    from app.services.prepaid_renewal_terms_backfill import (
        CorrectRenewalTermsCommand,
        RenewalTermsCorrectionAction,
        correct_prepaid_renewal_terms,
    )

    _block(db_session, subscription)
    subscription.unit_price = Decimal("2687.50")
    sub_id = subscription.id
    db_session.commit()

    result = correct_prepaid_renewal_terms(
        db_session,
        CorrectRenewalTermsCommand(
            subscription_id=sub_id,
            action=RenewalTermsCorrectionAction.apply_reviewed_term,
            reviewed_amount=Decimal("43000.00"),
            review_reference="FIN-2026-081",
        ),
        context=_context("correction-1"),
    )
    assert result.previous_amount == Decimal("2687.50")
    assert result.new_amount == Decimal("43000.00")
    assert result.replayed is False
    db_session.refresh(subscription)
    assert subscription.unit_price == Decimal("43000.00")

    db_session.commit()
    replay = correct_prepaid_renewal_terms(
        db_session,
        CorrectRenewalTermsCommand(
            subscription_id=sub_id,
            action=RenewalTermsCorrectionAction.apply_reviewed_term,
            reviewed_amount=Decimal("43000.00"),
            review_reference="FIN-2026-081",
        ),
        context=_context("correction-2"),
    )
    assert replay.replayed is True


def test_correction_can_restore_fail_closed_with_work_item(
    db_session, subscriber, subscription
):
    from app.services.prepaid_renewal_terms_backfill import (
        CorrectRenewalTermsCommand,
        RenewalTermsCorrectionAction,
        correct_prepaid_renewal_terms,
    )

    _block(db_session, subscription)
    subscription.unit_price = Decimal("2687.50")
    sub_id = subscription.id
    db_session.commit()

    correct_prepaid_renewal_terms(
        db_session,
        CorrectRenewalTermsCommand(
            subscription_id=sub_id,
            action=RenewalTermsCorrectionAction.restore_fail_closed,
            review_reference="FIN-2026-082",
        ),
        context=_context("correction-3"),
    )
    db_session.refresh(subscription)
    assert subscription.unit_price is None
    alert = (
        db_session.query(AdminAlert)
        .filter(AdminAlert.fingerprint == f"{_FINDING_PREFIX}{subscription.id}")
        .one()
    )
    assert alert.status.value == "open"
    assert alert.details["decision"] == "correction_fail_closed"
    assert alert.details["review_reference"] == "FIN-2026-082"


def test_audit_reclassifies_restored_subscriptions(
    db_session, subscriber, subscription
):
    from app.services.prepaid_renewal_terms_backfill import (
        audit_restored_renewal_terms,
    )

    _block(db_session, subscription)
    _paid_line(db_session, subscriber, subscription, "35000.00")
    preview = preview_prepaid_renewal_terms_backfill(db_session, now=_NOON)
    _capture(db_session, preview.fingerprint, "renewal-audit")
    db_session.refresh(subscription)
    assert subscription.unit_price == Decimal("35000.00")

    items = audit_restored_renewal_terms(db_session)
    ours = [i for i in items if i.subscription_id == subscription.id]
    assert ours and ours[0].amount_confirmed is True
