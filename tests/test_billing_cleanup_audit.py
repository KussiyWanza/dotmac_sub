from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.models.billing import Invoice, InvoiceLine, InvoiceStatus
from app.models.catalog import (
    AccessCredential,
    AccessType,
    AddOn,
    AddOnPrice,
    BillingCycle,
    BillingMode,
    CatalogOffer,
    PriceBasis,
    PriceType,
    ServiceType,
    Subscription,
    SubscriptionAddOn,
    SubscriptionStatus,
)
from app.models.collections import DunningCase, DunningCaseStatus
from app.models.enforcement_lock import EnforcementLock, EnforcementReason
from app.models.subscriber import Subscriber, SubscriberStatus
from app.services.billing_cleanup_audit import (
    build_billing_cleanup_report,
    find_active_subscription_missing_radius,
    find_billing_addon_without_billable_parent,
    find_billing_disabled_service_lines,
    find_billing_duplicate_subscription_period_lines,
    write_billing_cleanup_report,
)


def _account(db, *, mode=BillingMode.prepaid, status=SubscriberStatus.active):
    account = Subscriber(
        first_name="Billing",
        last_name="Audit",
        email=f"{uuid.uuid4().hex}@example.com",
        status=status,
        billing_mode=mode,
        is_active=True,
    )
    db.add(account)
    db.flush()
    return account


def _offer(db, *, mode=BillingMode.prepaid):
    offer = CatalogOffer(
        # Unique per call: sellable offer names are unique (489), and this
        # helper is invoked once per subscription with the same mode.
        name=f"Audit {mode.value} {uuid.uuid4().hex[:8]}",
        service_type=ServiceType.residential,
        access_type=AccessType.fiber,
        price_basis=PriceBasis.flat,
        billing_cycle=BillingCycle.monthly,
        billing_mode=mode,
        is_active=True,
    )
    db.add(offer)
    db.flush()
    return offer


def _subscription(
    db,
    account,
    *,
    mode=BillingMode.prepaid,
    status=SubscriptionStatus.active,
    offer=None,
    next_billing_at=None,
):
    offer = offer or _offer(db, mode=mode)
    subscription = Subscription(
        subscriber_id=account.id,
        offer_id=offer.id,
        status=status,
        billing_mode=mode,
        next_billing_at=next_billing_at,
    )
    db.add(subscription)
    db.flush()
    return subscription


def _invoice(
    db,
    account,
    *,
    status=InvoiceStatus.overdue,
    amount="100.00",
    start=None,
    end=None,
    balance_due=None,
):
    total = Decimal(amount)
    balance = Decimal(balance_due if balance_due is not None else amount)
    invoice = Invoice(
        account_id=account.id,
        invoice_number=f"INV-{uuid.uuid4().hex[:10]}",
        status=status,
        currency="NGN",
        subtotal=total,
        tax_total=Decimal("0.00"),
        total=total,
        balance_due=balance,
        billing_period_start=start,
        billing_period_end=end,
        issued_at=start,
        due_at=start,
        is_active=True,
    )
    db.add(invoice)
    db.flush()
    return invoice


def _line(db, invoice, subscription, *, amount="100.00"):
    line = InvoiceLine(
        invoice_id=invoice.id,
        subscription_id=subscription.id,
        description="Audit service",
        quantity=Decimal("1.000"),
        unit_price=Decimal(amount),
        amount=Decimal(amount),
        is_active=True,
    )
    db.add(line)
    db.flush()
    return line


def test_finds_disabled_service_lines(db_session):
    account = _account(db_session, mode=BillingMode.postpaid)
    subscription = _subscription(
        db_session,
        account,
        mode=BillingMode.postpaid,
        status=SubscriptionStatus.canceled,
    )
    subscription.canceled_at = datetime(2026, 6, 1, tzinfo=UTC)
    invoice = _invoice(
        db_session,
        account,
        status=InvoiceStatus.issued,
        start=datetime(2026, 7, 1, tzinfo=UTC),
        end=datetime(2026, 8, 1, tzinfo=UTC),
    )
    line = _line(db_session, invoice, subscription)
    db_session.commit()

    rows = find_billing_disabled_service_lines(db_session)

    assert any(row["invoice_line_id"] == str(line.id) for row in rows)
    row = next(row for row in rows if row["invoice_line_id"] == str(line.id))
    assert row["finding_type"] == "disabled_service"
    assert row["proposed_disposition"] == "credit_or_void_required"


def test_finds_duplicate_subscription_period_lines(db_session):
    account = _account(db_session, mode=BillingMode.postpaid)
    subscription = _subscription(db_session, account, mode=BillingMode.postpaid)
    start = datetime(2026, 7, 1, tzinfo=UTC)
    end = datetime(2026, 8, 1, tzinfo=UTC)
    invoice = _invoice(
        db_session,
        account,
        status=InvoiceStatus.issued,
        amount="200.00",
        start=start,
        end=end,
    )
    first = _line(db_session, invoice, subscription)
    second = _line(db_session, invoice, subscription)
    db_session.commit()

    rows = find_billing_duplicate_subscription_period_lines(db_session)
    line_ids = {row["invoice_line_id"] for row in rows}

    assert str(first.id) in line_ids
    assert str(second.id) in line_ids
    assert all(row["duplicate_group_key"] for row in rows)


def test_finds_orphan_recurring_addon(db_session):
    account = _account(db_session, mode=BillingMode.postpaid)
    subscription = _subscription(
        db_session,
        account,
        mode=BillingMode.postpaid,
        status=SubscriptionStatus.canceled,
    )
    subscription.canceled_at = datetime(2026, 7, 1, tzinfo=UTC)
    addon = AddOn(name="Static IP", is_active=True)
    db_session.add(addon)
    db_session.flush()
    db_session.add(
        AddOnPrice(
            add_on_id=addon.id,
            price_type=PriceType.recurring,
            amount=Decimal("1000.00"),
            currency="NGN",
            is_active=True,
        )
    )
    sub_addon = SubscriptionAddOn(
        subscription_id=subscription.id,
        add_on_id=addon.id,
        start_at=datetime(2026, 6, 1, tzinfo=UTC),
    )
    db_session.add(sub_addon)
    db_session.commit()

    rows = find_billing_addon_without_billable_parent(db_session)

    assert any(row["subscription_add_on_id"] == str(sub_addon.id) for row in rows)


def test_finds_active_subscription_missing_radius(db_session, monkeypatch):
    account = _account(db_session, mode=BillingMode.prepaid)
    subscription = _subscription(db_session, account, mode=BillingMode.prepaid)
    subscription.login = "missing-radius"
    credential = AccessCredential(
        subscriber_id=account.id,
        username="missing-radius",
        secret_hash="plain:test123",
        is_active=True,
    )
    db_session.add(credential)
    db_session.commit()

    monkeypatch.setattr(
        "app.services.billing_cleanup_audit._external_ip_state",
        lambda db, logins: ({}, set(), []),
    )

    rows = find_active_subscription_missing_radius(db_session)

    assert any(row["subscription_id"] == str(subscription.id) for row in rows)
    row = next(row for row in rows if row["subscription_id"] == str(subscription.id))
    assert row["credential_usable"] == "true"
    assert row["recommended_action"] == "sync_radius_connectivity"


def test_billing_cleanup_report_includes_inconsistent_billing_buckets(db_session):
    now = datetime(2026, 7, 9, tzinfo=UTC)

    phantom_account = _account(db_session, mode=BillingMode.prepaid)
    _subscription(db_session, phantom_account, mode=BillingMode.prepaid)
    phantom_invoice = _invoice(
        db_session,
        phantom_account,
        status=InvoiceStatus.overdue,
        amount="125.00",
        start=now - timedelta(days=30),
        end=now,
    )

    overlap_account = _account(db_session, mode=BillingMode.prepaid)
    overlap_subscription = _subscription(
        db_session,
        overlap_account,
        mode=BillingMode.prepaid,
        next_billing_at=now - timedelta(days=10),
    )
    paid = _invoice(
        db_session,
        overlap_account,
        status=InvoiceStatus.paid,
        amount="100.00",
        balance_due="0.00",
        start=now - timedelta(days=35),
        end=now + timedelta(days=5),
    )
    _line(db_session, paid, overlap_subscription)
    bad_overlap = _invoice(
        db_session,
        overlap_account,
        status=InvoiceStatus.issued,
        amount="100.00",
        start=now - timedelta(days=5),
        end=now + timedelta(days=25),
    )
    _line(db_session, bad_overlap, overlap_subscription)

    stale_account = _account(db_session, mode=BillingMode.postpaid)
    stale_subscription = _subscription(
        db_session, stale_account, mode=BillingMode.postpaid
    )
    case = DunningCase(
        account_id=stale_account.id,
        status=DunningCaseStatus.open,
        current_step=7,
        started_at=now - timedelta(days=7),
    )
    db_session.add(case)
    db_session.flush()
    lock = EnforcementLock(
        subscription_id=stale_subscription.id,
        subscriber_id=stale_account.id,
        reason=EnforcementReason.overdue,
        source=f"dunning_case:{case.id}",
        is_active=True,
    )
    db_session.add(lock)

    drift_account = _account(db_session, mode=BillingMode.prepaid)
    postpaid_offer = _offer(db_session, mode=BillingMode.postpaid)
    drift_subscription = _subscription(
        db_session,
        drift_account,
        mode=BillingMode.postpaid,
        offer=postpaid_offer,
    )
    db_session.commit()

    report = build_billing_cleanup_report(db_session)

    prepaid_invoice_ids = {row["invoice_id"] for row in report.prepaid_collectible_ar}
    assert str(phantom_invoice.id) in prepaid_invoice_ids
    assert str(bad_overlap.id) in prepaid_invoice_ids
    assert any(
        row["bad_invoice_id"] == str(bad_overlap.id) for row in report.prepaid_overlaps
    )
    assert any(row["case_id"] == str(case.id) for row in report.stale_dunning_cases)
    assert any(row["lock_id"] == str(lock.id) for row in report.stale_overdue_locks)
    assert any(
        row["subscription_id"] == str(drift_subscription.id)
        and row["issue"] == "subscription_vs_account"
        for row in report.billing_mode_drift
    )
    assert any(
        row["subscription_id"] == str(overlap_subscription.id)
        and row["paid_through"] == paid.billing_period_end.isoformat()
        for row in report.next_billing_anchor_drift
    )


def test_write_billing_cleanup_report_writes_summary_and_bucket_csvs(
    db_session, tmp_path
):
    account = _account(db_session, mode=BillingMode.prepaid)
    _subscription(db_session, account, mode=BillingMode.prepaid)
    _invoice(db_session, account, status=InvoiceStatus.overdue, amount="50.00")
    db_session.commit()

    report = build_billing_cleanup_report(db_session)
    files = write_billing_cleanup_report(report, tmp_path)

    assert "summary" in files
    assert (tmp_path / "prepaid_collectible_ar.csv").exists()
    assert (tmp_path / "billing_disabled_service_lines.csv").exists()
    assert (tmp_path / "billing_duplicate_subscription_period_lines.csv").exists()
    assert (tmp_path / "billing_addon_without_billable_parent.csv").exists()
    assert (tmp_path / "active_subscription_missing_radius.csv").exists()
    assert (tmp_path / "stale_dunning_cases.csv").exists()
    with (tmp_path / "summary.json").open() as handle:
        summary = json.load(handle)
    assert summary["prepaid_collectible_ar"] >= 1
