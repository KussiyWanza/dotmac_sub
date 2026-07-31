from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.models.billing import (
    Invoice,
    InvoiceLine,
    InvoiceStatus,
    ServiceEntitlement,
    ServiceEntitlementStatus,
)
from app.models.catalog import BillingMode, SubscriptionStatus
from app.models.event_store import EventStore
from app.models.prepaid_coverage import (
    PrepaidCoverageReconciliationItem,
    PrepaidCoverageReconciliationRun,
)
from app.models.subscriber import SubscriberStatus
from app.services.collections.scheduled import repair_prepaid_coverage_evidence
from app.services.owner_commands import CommandContext
from app.services.prepaid_coverage_reconciliation import (
    CoverageReconciliationDecision,
    CoverageReconciliationReason,
    PrepaidCoverageReconciliationError,
    ReconcilePrepaidCoverageCommand,
    preview_prepaid_coverage_reconciliation,
    reconcile_prepaid_service_coverage,
    resolve_prepaid_coverage_enforcement_blockers,
)

NOW = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)


def _prepare(db, account, subscription) -> None:
    account.billing_mode = BillingMode.prepaid
    account.status = SubscriberStatus.active
    account.is_active = True
    account.billing_enabled = True
    subscription.billing_mode = BillingMode.prepaid
    subscription.status = SubscriptionStatus.active
    subscription.next_billing_at = NOW + timedelta(days=30)
    db.commit()


def _paid_invoice(db, account, subscription) -> tuple[Invoice, InvoiceLine]:
    invoice = Invoice(
        account_id=account.id,
        status=InvoiceStatus.paid,
        currency="NGN",
        subtotal=Decimal("35000.00"),
        total=Decimal("35000.00"),
        balance_due=Decimal("0.00"),
        billing_period_start=NOW - timedelta(days=1),
        billing_period_end=NOW + timedelta(days=30),
        issued_at=NOW - timedelta(days=1),
        paid_at=NOW - timedelta(days=1),
    )
    db.add(invoice)
    db.flush()
    line = InvoiceLine(
        invoice_id=invoice.id,
        subscription_id=subscription.id,
        description="Base service",
        quantity=Decimal("1.000"),
        unit_price=Decimal("35000.00"),
        amount=Decimal("35000.00"),
        metadata_={"kind": "base_subscription"},
    )
    db.add(line)
    db.commit()
    return invoice, line


def _command(preview, *, key: str = "pytest-prepaid-coverage"):
    return ReconcilePrepaidCoverageCommand(
        context=CommandContext.system(
            actor="pytest",
            scope="prepaid_service_coverage",
            reason="Reviewed exact paid invoice coverage evidence",
            idempotency_key=key,
        ),
        as_of=preview.as_of,
        preview_fingerprint=preview.fingerprint,
        subscription_ids=preview.subscription_ids,
    )


def test_paid_invoice_requires_projection_before_it_covers_service(
    db_session, subscriber_account, subscription
):
    _prepare(db_session, subscriber_account, subscription)
    _invoice, line = _paid_invoice(db_session, subscriber_account, subscription)

    preview = preview_prepaid_coverage_reconciliation(
        db_session,
        as_of=NOW,
        subscription_ids=(subscription.id,),
    )

    assert preview.repairable_count == 1
    assert preview.quarantined_count == 0
    assert preview.items[0].decision == (
        CoverageReconciliationDecision.entitlement_created
    )
    assert preview.items[0].reason == (
        CoverageReconciliationReason.exact_paid_invoice_line
    )
    assert preview.items[0].source_id == line.id
    blockers = resolve_prepaid_coverage_enforcement_blockers(
        db_session,
        [subscription],
        as_of=NOW,
    )
    assert len(blockers) == 1
    assert blockers[0].subscription_id == subscription.id
    assert blockers[0].reason == CoverageReconciliationReason.exact_paid_invoice_line


def test_reconciliation_creates_exact_entitlement_and_immutable_evidence(
    db_session, subscriber_account, subscription
):
    _prepare(db_session, subscriber_account, subscription)
    invoice, line = _paid_invoice(db_session, subscriber_account, subscription)
    preview = preview_prepaid_coverage_reconciliation(
        db_session,
        as_of=NOW,
        subscription_ids=(subscription.id,),
    )
    db_session.commit()

    result = reconcile_prepaid_service_coverage(db_session, _command(preview))

    assert result.entitlement_created_count == 1
    assert result.quarantined_count == 0
    entitlement = db_session.query(ServiceEntitlement).one()
    assert entitlement.source_invoice_id == invoice.id
    assert entitlement.source_invoice_line_id == line.id
    assert entitlement.subscription_id == subscription.id
    assert entitlement.starts_at.replace(tzinfo=UTC) == NOW - timedelta(days=1)
    assert entitlement.ends_at.replace(tzinfo=UTC) == NOW + timedelta(days=30)
    assert entitlement.metadata_["reconciled_by"] == (
        "financial.prepaid_service_coverage_reconciliation"
    )
    run = db_session.query(PrepaidCoverageReconciliationRun).one()
    item = db_session.query(PrepaidCoverageReconciliationItem).one()
    assert run.preview_fingerprint == preview.fingerprint
    assert item.entitlement_id == entitlement.id
    assert item.source_invoice_line_id == line.id
    assert item.source_entitlement_id is None
    assert item.source_service_extension_entry_id is None
    assert item.source_account_adjustment_id is None
    assert item.evidence_fingerprint == preview.items[0].evidence_fingerprint
    event = (
        db_session.query(EventStore)
        .filter_by(event_type="prepaid_coverage.reconciled")
        .one()
    )
    assert event.actor == "pytest"
    assert event.payload["run_id"] == str(run.id)


def test_reconciliation_idempotently_replays_one_run(
    db_session, subscriber_account, subscription
):
    _prepare(db_session, subscriber_account, subscription)
    _paid_invoice(db_session, subscriber_account, subscription)
    preview = preview_prepaid_coverage_reconciliation(
        db_session,
        as_of=NOW,
        subscription_ids=(subscription.id,),
    )
    db_session.commit()
    command = _command(preview)

    first = reconcile_prepaid_service_coverage(db_session, command)
    replay = reconcile_prepaid_service_coverage(db_session, command)

    assert replay.replayed is True
    assert replay.run_id == first.run_id
    assert db_session.query(ServiceEntitlement).count() == 1
    assert db_session.query(PrepaidCoverageReconciliationRun).count() == 1


def test_future_anchor_without_structural_evidence_is_quarantined(
    db_session, subscriber_account, subscription
):
    _prepare(db_session, subscriber_account, subscription)

    preview = preview_prepaid_coverage_reconciliation(
        db_session,
        as_of=NOW,
        subscription_ids=(subscription.id,),
    )

    assert preview.repairable_count == 0
    assert preview.quarantined_count == 1
    assert preview.items[0].reason == (
        CoverageReconciliationReason.future_anchor_without_exact_evidence
    )


def test_duplicate_current_entitlements_fail_closed(
    db_session, subscriber_account, subscription
):
    _prepare(db_session, subscriber_account, subscription)
    for offset in (0, 1):
        db_session.add(
            ServiceEntitlement(
                account_id=subscriber_account.id,
                subscription_id=subscription.id,
                starts_at=NOW - timedelta(days=2 + offset),
                ends_at=NOW + timedelta(days=20 + offset),
                amount_funded=Decimal("35000.00"),
                currency="NGN",
                status=ServiceEntitlementStatus.active,
            )
        )
    db_session.commit()

    preview = preview_prepaid_coverage_reconciliation(
        db_session,
        as_of=NOW,
        subscription_ids=(subscription.id,),
    )

    assert preview.quarantined_count == 1
    assert preview.items[0].reason == (
        CoverageReconciliationReason.duplicate_current_entitlements
    )


def test_confirmation_rejects_stale_preview(
    db_session, subscriber_account, subscription
):
    _prepare(db_session, subscriber_account, subscription)
    _paid_invoice(db_session, subscriber_account, subscription)
    preview = preview_prepaid_coverage_reconciliation(
        db_session,
        as_of=NOW,
        subscription_ids=(subscription.id,),
    )
    db_session.add(
        ServiceEntitlement(
            account_id=subscriber_account.id,
            subscription_id=subscription.id,
            starts_at=NOW - timedelta(days=1),
            ends_at=NOW + timedelta(days=30),
            amount_funded=Decimal("35000.00"),
            currency="NGN",
            status=ServiceEntitlementStatus.active,
        )
    )
    db_session.commit()

    with pytest.raises(PrepaidCoverageReconciliationError) as exc:
        reconcile_prepaid_service_coverage(db_session, _command(preview))

    assert exc.value.code.endswith("stale_preview")
    assert db_session.query(PrepaidCoverageReconciliationRun).count() == 0


def test_scheduled_run_repairs_exact_evidence_without_an_operator(
    db_session, subscriber_account, subscription
):
    _prepare(db_session, subscriber_account, subscription)
    invoice, line = _paid_invoice(db_session, subscriber_account, subscription)

    outcome = repair_prepaid_coverage_evidence(db_session, now=NOW)

    assert outcome.repairable == 1
    assert outcome.entitlements_created == 1
    assert outcome.status.value == "ok"
    entitlement = db_session.query(ServiceEntitlement).one()
    assert entitlement.source_invoice_id == invoice.id
    assert entitlement.source_invoice_line_id == line.id
    run = db_session.query(PrepaidCoverageReconciliationRun).one()
    assert run.actor == "task:prepaid-coverage-repair"

    # The repaired subscription is now covered, so the threshold owner never
    # consults the enforcement blockers for it again.
    from app.services.prepaid_service_coverage import (
        resolve_prepaid_service_coverage,
    )

    coverage = resolve_prepaid_service_coverage(db_session, [subscription], as_of=NOW)
    assert coverage[subscription.id].covered is True


def test_scheduled_run_is_a_noop_when_nothing_is_repairable(
    db_session, subscriber_account, subscription
):
    _prepare(db_session, subscriber_account, subscription)
    _paid_invoice(db_session, subscriber_account, subscription)

    first = repair_prepaid_coverage_evidence(db_session, now=NOW)
    second = repair_prepaid_coverage_evidence(db_session, now=NOW)

    assert first.entitlements_created == 1
    assert second.repairable == 0
    assert second.entitlements_created == 0
    assert db_session.query(ServiceEntitlement).count() == 1
    assert db_session.query(PrepaidCoverageReconciliationRun).count() == 1


def test_scheduled_run_ignores_nonblocking_quarantine(
    db_session, subscriber_account, subscription
):
    # A future billing anchor without evidence is quarantined by the preview
    # but does not block enforcement, so the runner must not confirm it into
    # run evidence on every sweep.
    _prepare(db_session, subscriber_account, subscription)

    outcome = repair_prepaid_coverage_evidence(db_session, now=NOW)

    assert outcome.repairable == 0
    assert outcome.quarantined == 1
    assert outcome.quarantined_blocking == 0
    assert outcome.entitlements_created == 0
    assert db_session.query(ServiceEntitlement).count() == 0
    assert db_session.query(PrepaidCoverageReconciliationRun).count() == 0


def test_scheduled_run_persists_blocking_quarantine_through_the_owner(
    db_session, subscriber_account, subscription, caplog
):
    _prepare(db_session, subscriber_account, subscription)
    # Malformed paid-invoice period: exact money evidence that cannot be
    # projected, which blocks adverse enforcement until a human resolves it.
    invoice, _line = _paid_invoice(db_session, subscriber_account, subscription)
    invoice.billing_period_end = invoice.billing_period_start
    db_session.commit()

    with caplog.at_level("ERROR"):
        first = repair_prepaid_coverage_evidence(db_session, now=NOW)
    second = repair_prepaid_coverage_evidence(db_session, now=NOW)

    assert first.quarantined_blocking == 1
    assert first.entitlements_created == 0
    assert any(
        "prepaid_coverage_quarantined_evidence_requires_review" in record.message
        for record in caplog.records
    )
    # The owner recorded immutable evidence for the quarantined item, and the
    # unchanged evidence replays the same run instead of multiplying records.
    run = db_session.query(PrepaidCoverageReconciliationRun).one()
    item = db_session.query(PrepaidCoverageReconciliationItem).one()
    assert run.quarantined_count == 1
    assert item.decision == "quarantined"
    assert item.reason_code == "malformed_paid_invoice_period"
    assert second.quarantined_blocking == 1
    assert db_session.query(PrepaidCoverageReconciliationRun).count() == 1
    assert db_session.query(ServiceEntitlement).count() == 0
