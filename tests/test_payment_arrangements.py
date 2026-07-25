"""Tests for the payment arrangements service (installment plans).

Moved out of tests/test_contracts_crypto_services.py and extended to cover
creation validation, lifecycle (approve/record/overdue/default), automatic
progression from billing payments, and customer cancel restrictions.
"""

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from app.models.billing import Invoice, InvoiceStatus
from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.payment_arrangement import (
    ArrangementStatus,
    InstallmentStatus,
    PaymentArrangement,
    PaymentArrangementInstallment,
    PaymentFrequency,
)
from app.models.subscription_engine import SettingValueType
from app.services import web_billing_arrangements
from app.services.customer_portal_flow_billing import cancel_customer_arrangement
from app.services.payment_arrangements import (
    _calculate_end_date,
    _calculate_next_due_date,
    apply_payment_to_arrangement,
    get_account_outstanding_balance,
    get_next_actionable_installment,
    payment_arrangements,
)
from app.services.settings_spec import get_spec

# ============================================================================
# helpers
# ============================================================================


def _overdue_invoice(db_session, subscriber, amount="1000.00"):
    inv = Invoice(
        account_id=subscriber.id,
        status=InvoiceStatus.overdue,
        currency="NGN",
        subtotal=Decimal(amount),
        tax_total=Decimal("0.00"),
        total=Decimal(amount),
        balance_due=Decimal(amount),
        issued_at=datetime(2026, 4, 1, tzinfo=UTC),
        due_at=datetime(2026, 5, 1, tzinfo=UTC),
        is_active=True,
    )
    db_session.add(inv)
    db_session.commit()
    db_session.refresh(inv)
    return inv


def _create_arrangement_directly(
    db_session,
    subscriber,
    total=Decimal("400.00"),
    num_installments=2,
    frequency=PaymentFrequency.monthly,
    start=None,
    notes=None,
):
    """Create a PaymentArrangement + installments directly via the ORM,
    bypassing the service create method (and its validation)."""
    start_date = start or date(2025, 6, 1)
    installment_amount = (total / Decimal(num_installments)).quantize(Decimal("0.01"))
    rounding_diff = total - (installment_amount * num_installments)
    end_date = _calculate_end_date(start_date, frequency, num_installments)

    arrangement = PaymentArrangement(
        subscriber_id=subscriber.id,
        total_amount=total,
        installment_amount=installment_amount,
        frequency=frequency,
        installments_total=num_installments,
        installments_paid=0,
        start_date=start_date,
        end_date=end_date,
        next_due_date=start_date,
        status=ArrangementStatus.pending,
        notes=notes,
    )
    db_session.add(arrangement)
    db_session.flush()

    current_date = start_date
    for i in range(num_installments):
        amount = installment_amount
        if i == num_installments - 1:
            amount += rounding_diff
        inst = PaymentArrangementInstallment(
            arrangement_id=arrangement.id,
            installment_number=i + 1,
            amount=amount,
            due_date=current_date,
            status=InstallmentStatus.pending,
        )
        db_session.add(inst)
        current_date = _calculate_next_due_date(current_date, frequency)

    db_session.commit()
    db_session.refresh(arrangement)
    return arrangement


def _installments_for(db_session, arrangement):
    return (
        db_session.query(PaymentArrangementInstallment)
        .filter(PaymentArrangementInstallment.arrangement_id == arrangement.id)
        .order_by(PaymentArrangementInstallment.installment_number)
        .all()
    )


# ============================================================================
# helper-function tests
# ============================================================================


def test_arrangement_policy_setting_specs_registered():
    expected = {
        "arrangement_min_installments": (2, 2, 60),
        "arrangement_max_installments": (24, 2, 60),
        "arrangement_default_overdue_installments": (2, 1, 5),
    }

    for key, (default, min_value, max_value) in expected.items():
        spec = get_spec(SettingDomain.billing, key)

        assert spec is not None
        assert spec.value_type == SettingValueType.integer
        assert spec.default == default
        assert spec.min_value == min_value
        assert spec.max_value == max_value


class TestPaymentArrangementHelpers:
    """Tests for payment arrangement helper functions."""

    def test_calculate_end_date_weekly(self):
        start = date(2025, 1, 1)
        end = _calculate_end_date(start, PaymentFrequency.weekly, 4)
        assert end == date(2025, 1, 22)

    def test_calculate_end_date_biweekly(self):
        start = date(2025, 1, 1)
        end = _calculate_end_date(start, PaymentFrequency.biweekly, 3)
        assert end == date(2025, 1, 29)

    def test_calculate_end_date_monthly(self):
        start = date(2025, 1, 15)
        end = _calculate_end_date(start, PaymentFrequency.monthly, 3)
        assert end == date(2025, 3, 15)

    def test_calculate_end_date_monthly_year_boundary(self):
        start = date(2025, 11, 1)
        end = _calculate_end_date(start, PaymentFrequency.monthly, 4)
        assert end == date(2026, 2, 1)

    def test_calculate_end_date_monthly_clamps_short_month_and_recovers_anchor(self):
        start = date(2025, 1, 31)
        end = _calculate_end_date(start, PaymentFrequency.monthly, 3)
        assert end == date(2025, 3, 31)

    def test_calculate_next_due_date_weekly(self):
        d = date(2025, 3, 1)
        assert _calculate_next_due_date(d, PaymentFrequency.weekly) == date(2025, 3, 8)

    def test_calculate_next_due_date_biweekly(self):
        d = date(2025, 3, 1)
        assert _calculate_next_due_date(d, PaymentFrequency.biweekly) == date(
            2025, 3, 15
        )

    def test_calculate_next_due_date_monthly(self):
        d = date(2025, 3, 15)
        assert _calculate_next_due_date(d, PaymentFrequency.monthly) == date(
            2025, 4, 15
        )

    def test_calculate_next_due_date_monthly_december(self):
        d = date(2025, 12, 1)
        assert _calculate_next_due_date(d, PaymentFrequency.monthly) == date(2026, 1, 1)

    def test_calculate_next_due_date_monthly_clamps_short_month(self):
        d = date(2025, 1, 31)
        assert _calculate_next_due_date(
            d, PaymentFrequency.monthly, anchor_day=31
        ) == date(2025, 2, 28)

    def test_calculate_next_due_date_monthly_preserves_anchor_after_clamp(self):
        d = date(2025, 2, 28)
        assert _calculate_next_due_date(
            d, PaymentFrequency.monthly, anchor_day=31
        ) == date(2025, 3, 31)


# ============================================================================
# CRUD + creation validation tests
# ============================================================================


class TestPaymentArrangements:
    """Tests for payment arrangement CRUD operations."""

    def test_create_arrangement_uses_subscriber_fields(self, db_session, subscriber):
        _overdue_invoice(db_session, subscriber, amount="600.00")
        arrangement = payment_arrangements.create(
            db_session,
            subscriber_id=str(subscriber.id),
            total_amount=Decimal("600.00"),
            installments=3,
            frequency=PaymentFrequency.monthly.value,
            start_date=date(2025, 1, 31),
            requested_by_subscriber_id=str(subscriber.id),
            notes="Need extra time",
        )

        assert arrangement.subscriber_id == subscriber.id
        assert arrangement.requested_by_subscriber_id == subscriber.id
        assert arrangement.end_date == date(2025, 3, 31)
        assert len(arrangement.installments) == 3

    def test_create_arrangement_directly(self, db_session, subscriber):
        arrangement = _create_arrangement_directly(
            db_session,
            subscriber,
            total=Decimal("600.00"),
            num_installments=3,
        )
        assert arrangement is not None
        assert arrangement.total_amount == Decimal("600.00")
        assert arrangement.installment_amount == Decimal("200.00")
        assert arrangement.installments_total == 3
        assert arrangement.installments_paid == 0
        assert arrangement.status == ArrangementStatus.pending
        assert arrangement.frequency == PaymentFrequency.monthly

    def test_create_arrangement_rounding(self, db_session, subscriber):
        arrangement = _create_arrangement_directly(
            db_session,
            subscriber,
            total=Decimal("100.00"),
            num_installments=3,
        )
        assert arrangement.installment_amount == Decimal("33.33")
        installments = _installments_for(db_session, arrangement)
        assert len(installments) == 3
        total = sum(i.amount for i in installments)
        assert total == Decimal("100.00")
        assert installments[2].amount == Decimal("33.34")

    def test_create_rejected_without_outstanding_balance(self, db_session, subscriber):
        with pytest.raises(HTTPException) as exc_info:
            payment_arrangements.create(
                db_session,
                subscriber_id=str(subscriber.id),
                total_amount=Decimal("100.00"),
                installments=2,
                frequency=PaymentFrequency.monthly.value,
                start_date=date(2026, 7, 1),
            )
        assert exc_info.value.status_code == 400
        assert "outstanding" in exc_info.value.detail

    def test_create_rejected_over_outstanding_balance(self, db_session, subscriber):
        _overdue_invoice(db_session, subscriber, amount="100.00")
        with pytest.raises(HTTPException) as exc_info:
            payment_arrangements.create(
                db_session,
                subscriber_id=str(subscriber.id),
                total_amount=Decimal("600.00"),
                installments=3,
                frequency=PaymentFrequency.monthly.value,
                start_date=date(2026, 7, 1),
            )
        assert exc_info.value.status_code == 400
        assert "outstanding" in exc_info.value.detail

    def test_create_rejected_over_invoice_balance(self, db_session, subscriber):
        invoice = _overdue_invoice(db_session, subscriber, amount="300.00")
        # Plenty of account-level balance, but the targeted invoice is smaller
        _overdue_invoice(db_session, subscriber, amount="1000.00")
        with pytest.raises(HTTPException) as exc_info:
            payment_arrangements.create(
                db_session,
                subscriber_id=str(subscriber.id),
                total_amount=Decimal("400.00"),
                installments=2,
                frequency=PaymentFrequency.monthly.value,
                start_date=date(2026, 7, 1),
                invoice_id=str(invoice.id),
            )
        assert exc_info.value.status_code == 400
        assert "invoice" in exc_info.value.detail.lower()

    def test_create_respects_configured_max_installments(self, db_session, subscriber):
        db_session.add(
            DomainSetting(
                domain=SettingDomain.billing,
                key="arrangement_max_installments",
                value_type=SettingValueType.integer,
                value_text="3",
                is_active=True,
            )
        )
        db_session.commit()
        _overdue_invoice(db_session, subscriber, amount="1000.00")

        with pytest.raises(HTTPException) as exc_info:
            payment_arrangements.create(
                db_session,
                subscriber_id=str(subscriber.id),
                total_amount=Decimal("100.00"),
                installments=4,
                frequency=PaymentFrequency.monthly.value,
                start_date=date(2026, 7, 1),
            )

        assert exc_info.value.status_code == 400
        assert "Maximum 3 installments allowed" in exc_info.value.detail

    def test_create_within_invoice_balance_succeeds(self, db_session, subscriber):
        invoice = _overdue_invoice(db_session, subscriber, amount="300.00")
        arrangement = payment_arrangements.create(
            db_session,
            subscriber_id=str(subscriber.id),
            total_amount=Decimal("300.00"),
            installments=3,
            frequency=PaymentFrequency.monthly.value,
            start_date=date(2026, 7, 1),
            invoice_id=str(invoice.id),
        )
        assert arrangement.invoice_id == invoice.id

    def test_create_rejected_when_pending_arrangement_exists(
        self, db_session, subscriber
    ):
        _overdue_invoice(db_session, subscriber, amount="1000.00")
        _create_arrangement_directly(db_session, subscriber)
        with pytest.raises(HTTPException) as exc_info:
            payment_arrangements.create(
                db_session,
                subscriber_id=str(subscriber.id),
                total_amount=Decimal("100.00"),
                installments=2,
                frequency=PaymentFrequency.monthly.value,
                start_date=date(2026, 7, 1),
            )
        assert exc_info.value.status_code == 400
        assert "pending" in exc_info.value.detail

    def test_create_rejected_when_active_arrangement_exists(
        self, db_session, subscriber
    ):
        _overdue_invoice(db_session, subscriber, amount="1000.00")
        existing = _create_arrangement_directly(db_session, subscriber)
        payment_arrangements.approve(db_session, str(existing.id))
        with pytest.raises(HTTPException) as exc_info:
            payment_arrangements.create(
                db_session,
                subscriber_id=str(subscriber.id),
                total_amount=Decimal("100.00"),
                installments=2,
                frequency=PaymentFrequency.monthly.value,
                start_date=date(2026, 7, 1),
            )
        assert exc_info.value.status_code == 400
        assert "active" in exc_info.value.detail

    def test_create_allowed_after_cancel(self, db_session, subscriber):
        _overdue_invoice(db_session, subscriber, amount="1000.00")
        existing = _create_arrangement_directly(db_session, subscriber)
        payment_arrangements.cancel(db_session, str(existing.id))
        arrangement = payment_arrangements.create(
            db_session,
            subscriber_id=str(subscriber.id),
            total_amount=Decimal("100.00"),
            installments=2,
            frequency=PaymentFrequency.monthly.value,
            start_date=date(2026, 7, 1),
        )
        assert arrangement.status == ArrangementStatus.pending

    def test_get_account_outstanding_balance(self, db_session, subscriber):
        assert get_account_outstanding_balance(
            db_session, str(subscriber.id)
        ) == Decimal("0")
        _overdue_invoice(db_session, subscriber, amount="150.00")
        _overdue_invoice(db_session, subscriber, amount="50.00")
        assert get_account_outstanding_balance(
            db_session, str(subscriber.id)
        ) == Decimal("200.00")

    def test_get_account_outstanding_balance_excludes_reconciliation_hold(
        self, db_session, subscriber
    ):
        _overdue_invoice(db_session, subscriber, amount="150.00")
        held = _overdue_invoice(db_session, subscriber, amount="50.00")
        held.metadata_ = {"reconciliation_hold": True}
        db_session.commit()

        assert get_account_outstanding_balance(
            db_session, str(subscriber.id)
        ) == Decimal("150.00")

    def test_get_account_outstanding_balance_excludes_truthy_reconciliation_hold(
        self, db_session, subscriber
    ):
        _overdue_invoice(db_session, subscriber, amount="150.00")
        for hold_value in ("true", "1", "yes", "on"):
            held = _overdue_invoice(db_session, subscriber, amount="50.00")
            held.metadata_ = {"reconciliation_hold": hold_value}
        db_session.commit()

        assert get_account_outstanding_balance(
            db_session, str(subscriber.id)
        ) == Decimal("150.00")

    def test_get_account_outstanding_balance_counts_false_or_missing_hold(
        self, db_session, subscriber
    ):
        _overdue_invoice(db_session, subscriber, amount="150.00")
        false_hold = _overdue_invoice(db_session, subscriber, amount="50.00")
        false_hold.metadata_ = {"reconciliation_hold": False}
        false_string_hold = _overdue_invoice(db_session, subscriber, amount="25.00")
        false_string_hold.metadata_ = {"reconciliation_hold": "false"}
        db_session.commit()

        assert get_account_outstanding_balance(
            db_session, str(subscriber.id)
        ) == Decimal("225.00")

    def test_get_arrangement(self, db_session, subscriber):
        arrangement = _create_arrangement_directly(db_session, subscriber)
        fetched = payment_arrangements.get(db_session, str(arrangement.id))
        assert fetched.id == arrangement.id

    def test_get_arrangement_not_found(self, db_session):
        with pytest.raises(HTTPException) as exc_info:
            payment_arrangements.get(db_session, str(uuid.uuid4()))
        assert exc_info.value.status_code == 404


# ============================================================================
# lifecycle tests: approve / record / overdue / default
# ============================================================================


class TestArrangementLifecycle:
    def test_approve_arrangement_past_start_marks_first_due(
        self, db_session, subscriber
    ):
        arrangement = _create_arrangement_directly(db_session, subscriber)
        approved = payment_arrangements.approve(db_session, str(arrangement.id))
        assert approved.status == ArrangementStatus.active
        assert approved.approved_at is not None

        first = _installments_for(db_session, arrangement)[0]
        assert first.status == InstallmentStatus.due

    def test_approve_future_start_keeps_first_pending(self, db_session, subscriber):
        start = date.today() + timedelta(days=30)
        arrangement = _create_arrangement_directly(db_session, subscriber, start=start)
        payment_arrangements.approve(db_session, str(arrangement.id))

        first = _installments_for(db_session, arrangement)[0]
        assert first.status == InstallmentStatus.pending

        # The scheduled check must not promote it before its due date
        result = payment_arrangements.check_overdue_installments(db_session)
        assert result["installments_marked_due"] == 0
        db_session.refresh(first)
        assert first.status == InstallmentStatus.pending

        # Once the date arrives, it is promoted to due
        first.due_date = date.today()
        db_session.commit()
        result = payment_arrangements.check_overdue_installments(db_session)
        assert result["installments_marked_due"] == 1
        db_session.refresh(first)
        assert first.status == InstallmentStatus.due

    def test_approve_records_admin_approver(self, db_session, subscriber):
        arrangement = _create_arrangement_directly(db_session, subscriber)
        admin_id = str(uuid.uuid4())
        approved = payment_arrangements.approve(
            db_session, str(arrangement.id), approved_by_user_id=admin_id
        )
        assert approved.approved_by_user_id == admin_id

    def test_approve_non_pending_raises(self, db_session, subscriber):
        arrangement = _create_arrangement_directly(db_session, subscriber)
        payment_arrangements.approve(db_session, str(arrangement.id))
        with pytest.raises(HTTPException) as exc_info:
            payment_arrangements.approve(db_session, str(arrangement.id))
        assert exc_info.value.status_code == 400

    def test_approve_not_found(self, db_session):
        with pytest.raises(HTTPException) as exc_info:
            payment_arrangements.approve(db_session, str(uuid.uuid4()))
        assert exc_info.value.status_code == 404

    def test_record_installment_payment(self, db_session, subscriber):
        arrangement = _create_arrangement_directly(db_session, subscriber)
        payment_arrangements.approve(db_session, str(arrangement.id))

        installments = _installments_for(db_session, arrangement)
        first = installments[0]
        paid = payment_arrangements.record_installment_payment(
            db_session, str(first.id), notes="Paid at office"
        )
        assert paid.status == InstallmentStatus.paid
        assert paid.paid_at is not None
        assert "Paid at office" in paid.notes

        # Second installment (due 2025-07-01, in the past) becomes due
        db_session.refresh(installments[1])
        assert installments[1].status == InstallmentStatus.due

        db_session.refresh(arrangement)
        assert arrangement.installments_paid == 1
        assert arrangement.next_due_date == installments[1].due_date

    def test_record_payment_future_next_installment_stays_pending(
        self, db_session, subscriber
    ):
        arrangement = _create_arrangement_directly(
            db_session, subscriber, start=date.today()
        )
        payment_arrangements.approve(db_session, str(arrangement.id))

        installments = _installments_for(db_session, arrangement)
        payment_arrangements.record_installment_payment(
            db_session, str(installments[0].id)
        )
        db_session.refresh(installments[1])
        assert installments[1].status == InstallmentStatus.pending
        db_session.refresh(arrangement)
        assert arrangement.next_due_date == installments[1].due_date

    def test_record_all_installments_completes_arrangement(
        self, db_session, subscriber
    ):
        arrangement = _create_arrangement_directly(db_session, subscriber)
        payment_arrangements.approve(db_session, str(arrangement.id))

        installments = _installments_for(db_session, arrangement)
        payment_arrangements.record_installment_payment(
            db_session, str(installments[0].id)
        )
        payment_arrangements.record_installment_payment(
            db_session, str(installments[1].id)
        )

        db_session.refresh(arrangement)
        assert arrangement.status == ArrangementStatus.completed
        assert arrangement.installments_paid == 2
        assert arrangement.next_due_date is None

    def test_record_already_paid_raises(self, db_session, subscriber):
        arrangement = _create_arrangement_directly(db_session, subscriber)
        payment_arrangements.approve(db_session, str(arrangement.id))

        first = _installments_for(db_session, arrangement)[0]
        payment_arrangements.record_installment_payment(db_session, str(first.id))
        with pytest.raises(HTTPException) as exc_info:
            payment_arrangements.record_installment_payment(db_session, str(first.id))
        assert exc_info.value.status_code == 400
        assert "already paid" in exc_info.value.detail

    def test_record_payment_on_pending_arrangement_raises(self, db_session, subscriber):
        arrangement = _create_arrangement_directly(db_session, subscriber)
        first = _installments_for(db_session, arrangement)[0]
        with pytest.raises(HTTPException) as exc_info:
            payment_arrangements.record_installment_payment(db_session, str(first.id))
        assert exc_info.value.status_code == 400
        assert "pending" in exc_info.value.detail

    def test_record_installment_not_found(self, db_session):
        with pytest.raises(HTTPException) as exc_info:
            payment_arrangements.record_installment_payment(
                db_session, str(uuid.uuid4())
            )
        assert exc_info.value.status_code == 404

    def test_check_overdue_marks_overdue_and_defaults(self, db_session, subscriber):
        # Two installments, both past due
        arrangement = _create_arrangement_directly(
            db_session,
            subscriber,
            start=date.today() - timedelta(days=90),
            frequency=PaymentFrequency.weekly,
        )
        payment_arrangements.approve(db_session, str(arrangement.id))

        with patch("app.services.events.emit_event") as emit_mock:
            # First run: #1 (due) becomes overdue, #2 (pending) promoted to due
            result = payment_arrangements.check_overdue_installments(db_session)
            assert result["installments_marked_overdue"] == 1
            assert result["installments_marked_due"] == 1
            assert result["arrangements_defaulted"] == 0

            # Second run: #2 becomes overdue too -> arrangement defaults
            result = payment_arrangements.check_overdue_installments(db_session)
            assert result["installments_marked_overdue"] == 1
            assert result["arrangements_defaulted"] == 1

        db_session.refresh(arrangement)
        assert arrangement.status == ArrangementStatus.defaulted
        statuses = [i.status for i in _installments_for(db_session, arrangement)]
        assert statuses == [InstallmentStatus.overdue, InstallmentStatus.overdue]

        emit_mock.assert_called_once()
        _, event_type = emit_mock.call_args[0][0], emit_mock.call_args[0][1]
        assert event_type.value == "arrangement.defaulted"
        assert emit_mock.call_args[0][2]["arrangement_id"] == str(arrangement.id)

    def test_check_overdue_respects_configured_default_threshold(
        self, db_session, subscriber
    ):
        db_session.add(
            DomainSetting(
                domain=SettingDomain.billing,
                key="arrangement_default_overdue_installments",
                value_type=SettingValueType.integer,
                value_text="3",
                is_active=True,
            )
        )
        arrangement = _create_arrangement_directly(
            db_session,
            subscriber,
            start=date.today() - timedelta(days=90),
            frequency=PaymentFrequency.weekly,
        )
        payment_arrangements.approve(db_session, str(arrangement.id))

        payment_arrangements.check_overdue_installments(db_session)
        result = payment_arrangements.check_overdue_installments(db_session)

        db_session.refresh(arrangement)
        assert result["installments_marked_overdue"] == 1
        assert result["arrangements_defaulted"] == 0
        assert arrangement.status == ArrangementStatus.active

    def test_check_overdue_ignores_unapproved_arrangements(
        self, db_session, subscriber
    ):
        # Pending (never approved) arrangement with past dates must not be touched
        arrangement = _create_arrangement_directly(
            db_session, subscriber, start=date.today() - timedelta(days=60)
        )
        result = payment_arrangements.check_overdue_installments(db_session)
        assert result == {
            "installments_marked_overdue": 0,
            "installments_marked_due": 0,
            "arrangements_defaulted": 0,
        }
        statuses = [i.status for i in _installments_for(db_session, arrangement)]
        assert all(s == InstallmentStatus.pending for s in statuses)

    def test_cancel_arrangement(self, db_session, subscriber):
        arrangement = _create_arrangement_directly(db_session, subscriber, notes=None)
        canceled = payment_arrangements.cancel(
            db_session, str(arrangement.id), notes="Customer request"
        )
        assert canceled.status == ArrangementStatus.canceled
        assert "Customer request" in canceled.notes

    def test_cancel_with_existing_notes(self, db_session, subscriber):
        arrangement = _create_arrangement_directly(
            db_session, subscriber, notes="Original note"
        )
        canceled = payment_arrangements.cancel(
            db_session, str(arrangement.id), notes="Cancellation reason"
        )
        assert "Original note" in canceled.notes
        assert "Cancellation reason" in canceled.notes

    def test_cancel_completed_raises(self, db_session, subscriber):
        arrangement = _create_arrangement_directly(db_session, subscriber)
        arrangement.status = ArrangementStatus.completed
        db_session.commit()
        with pytest.raises(HTTPException) as exc_info:
            payment_arrangements.cancel(db_session, str(arrangement.id))
        assert exc_info.value.status_code == 400

    def test_cancel_already_canceled_raises(self, db_session, subscriber):
        arrangement = _create_arrangement_directly(db_session, subscriber)
        arrangement.status = ArrangementStatus.canceled
        db_session.commit()
        with pytest.raises(HTTPException) as exc_info:
            payment_arrangements.cancel(db_session, str(arrangement.id))
        assert exc_info.value.status_code == 400

    def test_cancel_not_found(self, db_session):
        with pytest.raises(HTTPException) as exc_info:
            payment_arrangements.cancel(db_session, str(uuid.uuid4()))
        assert exc_info.value.status_code == 404


# ============================================================================
# automatic progression from billing payments
# ============================================================================


class TestApplyPaymentToArrangement:
    def test_no_active_arrangement_returns_none(self, db_session, subscriber):
        assert (
            apply_payment_to_arrangement(
                db_session, str(subscriber.id), Decimal("100.00")
            )
            is None
        )
        # Pending (unapproved) arrangements are not auto-advanced either
        _create_arrangement_directly(db_session, subscriber)
        assert (
            apply_payment_to_arrangement(
                db_session, str(subscriber.id), Decimal("100.00")
            )
            is None
        )

    def test_exact_amount_pays_one_installment(self, db_session, subscriber):
        arrangement = _create_arrangement_directly(
            db_session, subscriber, total=Decimal("400.00"), num_installments=2
        )
        payment_arrangements.approve(db_session, str(arrangement.id))

        result = apply_payment_to_arrangement(
            db_session, str(subscriber.id), Decimal("200.00")
        )
        assert result["installments_paid"] == 1
        assert result["arrangement_completed"] is False

        installments = _installments_for(db_session, arrangement)
        assert installments[0].status == InstallmentStatus.paid
        assert installments[1].status != InstallmentStatus.paid

    def test_large_payment_pays_multiple_and_completes(self, db_session, subscriber):
        arrangement = _create_arrangement_directly(
            db_session, subscriber, total=Decimal("400.00"), num_installments=2
        )
        payment_arrangements.approve(db_session, str(arrangement.id))

        result = apply_payment_to_arrangement(
            db_session, str(subscriber.id), Decimal("400.00")
        )
        assert result["installments_paid"] == 2
        assert result["arrangement_completed"] is True

        db_session.refresh(arrangement)
        assert arrangement.status == ArrangementStatus.completed
        statuses = [i.status for i in _installments_for(db_session, arrangement)]
        assert statuses == [InstallmentStatus.paid, InstallmentStatus.paid]

    def test_partial_amount_pays_nothing(self, db_session, subscriber):
        arrangement = _create_arrangement_directly(
            db_session, subscriber, total=Decimal("400.00"), num_installments=2
        )
        payment_arrangements.approve(db_session, str(arrangement.id))

        result = apply_payment_to_arrangement(
            db_session, str(subscriber.id), Decimal("150.00")
        )
        assert result["installments_paid"] == 0

        first = _installments_for(db_session, arrangement)[0]
        assert first.status == InstallmentStatus.due

    def test_links_payment_id_on_installment(self, db_session, subscriber):
        arrangement = _create_arrangement_directly(db_session, subscriber)
        payment_arrangements.approve(db_session, str(arrangement.id))

        from app.models.billing import Payment, PaymentStatus

        payment = Payment(
            account_id=subscriber.id,
            amount=Decimal("200.00"),
            currency="NGN",
            status=PaymentStatus.succeeded,
        )
        db_session.add(payment)
        db_session.commit()

        apply_payment_to_arrangement(
            db_session,
            str(subscriber.id),
            Decimal("200.00"),
            payment_id=str(payment.id),
        )
        first = _installments_for(db_session, arrangement)[0]
        assert first.payment_id == payment.id

    def test_event_handler_advances_arrangement(self, db_session, subscriber):
        from app.services.events.handlers.arrangements import ArrangementHandler
        from app.services.events.types import Event, EventType

        arrangement = _create_arrangement_directly(db_session, subscriber)
        payment_arrangements.approve(db_session, str(arrangement.id))

        handler = ArrangementHandler()
        handler.handle(
            db_session,
            Event(
                event_type=EventType.payment_received,
                payload={"amount": "200.00", "status": "succeeded"},
                account_id=subscriber.id,
            ),
        )
        first = _installments_for(db_session, arrangement)[0]
        assert first.status == InstallmentStatus.paid

    def test_event_handler_ignores_non_succeeded_payments(self, db_session, subscriber):
        from app.services.events.handlers.arrangements import ArrangementHandler
        from app.services.events.types import Event, EventType

        arrangement = _create_arrangement_directly(db_session, subscriber)
        payment_arrangements.approve(db_session, str(arrangement.id))

        handler = ArrangementHandler()
        handler.handle(
            db_session,
            Event(
                event_type=EventType.payment_received,
                payload={"amount": "200.00", "status": "pending"},
                account_id=subscriber.id,
            ),
        )
        first = _installments_for(db_session, arrangement)[0]
        assert first.status == InstallmentStatus.due


# ============================================================================
# admin web service: record-payment action, pagination, detail context
# ============================================================================


class TestAdminArrangementWeb:
    def test_list_data_counts_without_fetching_all(self, db_session, subscriber):
        for _ in range(3):
            arrangement = _create_arrangement_directly(db_session, subscriber)
            arrangement.status = ArrangementStatus.canceled
            db_session.commit()
        state = web_billing_arrangements.list_data(
            db_session, status=None, page=1, per_page=2
        )
        assert state["total"] == 3
        assert state["total_pages"] == 2
        assert len(state["arrangements"]) == 2

        state = web_billing_arrangements.list_data(
            db_session, status="canceled", page=1, per_page=10
        )
        assert state["total"] == 3

    def test_detail_data_exposes_only_owner_projected_actions(
        self, db_session, subscriber
    ):
        arrangement = _create_arrangement_directly(db_session, subscriber)
        state = web_billing_arrangements.detail_data(
            db_session,
            arrangement_id=str(arrangement.id),
            can_write=True,
        )
        assert [form.key for form in state["arrangement_actions"]] == [
            web_billing_arrangements.APPROVE_ACTION_KEY,
            web_billing_arrangements.CANCEL_ACTION_KEY,
        ]

        payment_arrangements.approve(db_session, str(arrangement.id))
        state = web_billing_arrangements.detail_data(
            db_session,
            arrangement_id=str(arrangement.id),
            can_write=True,
        )
        assert [form.key for form in state["arrangement_actions"]] == [
            web_billing_arrangements.CANCEL_ACTION_KEY,
            web_billing_arrangements.RECORD_PAYMENT_ACTION_KEY,
        ]

        state = web_billing_arrangements.detail_data(
            db_session,
            arrangement_id=str(arrangement.id),
            can_write=False,
        )
        assert state["arrangement_actions"] == ()

    def test_next_actionable_prefers_overdue(self, db_session, subscriber):
        arrangement = _create_arrangement_directly(db_session, subscriber)
        payment_arrangements.approve(db_session, str(arrangement.id))
        installments = _installments_for(db_session, arrangement)
        installments[0].status = InstallmentStatus.overdue
        db_session.commit()
        actionable = get_next_actionable_installment(db_session, str(arrangement.id))
        assert actionable.id == installments[0].id


# ============================================================================
# customer cancel restrictions
# ============================================================================


class TestCustomerCancelArrangement:
    def test_customer_can_cancel_pending(self, db_session, subscriber):
        arrangement = _create_arrangement_directly(db_session, subscriber)
        result = cancel_customer_arrangement(
            db_session, {"account_id": str(subscriber.id)}, str(arrangement.id)
        )
        assert result == {"success": True}
        db_session.refresh(arrangement)
        assert arrangement.status == ArrangementStatus.canceled

    def test_customer_cannot_cancel_active(self, db_session, subscriber):
        arrangement = _create_arrangement_directly(db_session, subscriber)
        payment_arrangements.approve(db_session, str(arrangement.id))
        with pytest.raises(HTTPException) as exc_info:
            cancel_customer_arrangement(
                db_session, {"account_id": str(subscriber.id)}, str(arrangement.id)
            )
        assert exc_info.value.status_code == 400
        db_session.refresh(arrangement)
        assert arrangement.status == ArrangementStatus.active

    def test_customer_cannot_cancel_other_accounts_arrangement(
        self, db_session, subscriber
    ):
        arrangement = _create_arrangement_directly(db_session, subscriber)
        with pytest.raises(HTTPException) as exc_info:
            cancel_customer_arrangement(
                db_session, {"account_id": str(uuid.uuid4())}, str(arrangement.id)
            )
        assert exc_info.value.status_code == 404


def test_payment_on_defaulted_arrangement_does_not_complete_it(db_session, subscriber):
    """A late installment payment must not silently resurrect a defaulted
    arrangement into completed (SM-gap #46)."""
    arrangement = _create_arrangement_directly(
        db_session, subscriber, num_installments=2
    )
    payment_arrangements.approve(db_session, str(arrangement.id))
    arrangement.status = ArrangementStatus.defaulted
    db_session.flush()

    for inst in _installments_for(db_session, arrangement):
        payment_arrangements.record_installment_payment(db_session, str(inst.id))

    db_session.refresh(arrangement)
    assert arrangement.status == ArrangementStatus.defaulted


def test_waived_installment_is_not_double_counted(db_session, subscriber):
    """Recording payment on a waived installment must not count it again
    (SM-gap #46)."""
    arrangement = _create_arrangement_directly(
        db_session, subscriber, num_installments=2
    )
    payment_arrangements.approve(db_session, str(arrangement.id))
    installments = _installments_for(db_session, arrangement)
    installments[0].status = InstallmentStatus.waived
    db_session.flush()
    db_session.refresh(arrangement)
    before = arrangement.installments_paid

    payment_arrangements.record_installment_payment(db_session, str(installments[0].id))

    db_session.refresh(arrangement)
    db_session.refresh(installments[0])
    assert arrangement.installments_paid == before
    assert installments[0].status == InstallmentStatus.waived
