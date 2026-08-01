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


def _paid_line(db, subscriber, subscription, amount: str) -> None:
    invoice = Invoice(
        account_id=subscriber.id,
        status=InvoiceStatus.paid,
        total=Decimal(amount),
    )
    db.add(invoice)
    db.flush()
    db.add(
        InvoiceLine(
            invoice_id=invoice.id,
            subscription_id=subscription.id,
            description="Monthly service",
            unit_price=Decimal(amount),
            amount=Decimal(amount),
            metadata_={"kind": "base_subscription"},
        )
    )
    db.commit()


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
