"""Service for managing payment arrangements."""

import json
import logging
from calendar import monthrange
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from hashlib import sha256
from typing import cast
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.models.payment_arrangement import (
    ArrangementStatus,
    InstallmentStatus,
    PaymentArrangement,
    PaymentArrangementInstallment,
    PaymentFrequency,
)
from app.services import settings_spec
from app.services.common import apply_ordering, apply_pagination, coerce_uuid
from app.services.domain_errors import DomainError
from app.services.response import ListResponseMixin

logger = logging.getLogger(__name__)

DEFAULT_MIN_INSTALLMENTS = 2
DEFAULT_MAX_INSTALLMENTS = 24
DEFAULT_OVERDUE_DEFAULT_THRESHOLD = 2
MIN_ALLOWED_INSTALLMENTS = 2
MAX_ALLOWED_INSTALLMENTS = 60
MAX_ALLOWED_OVERDUE_DEFAULT_THRESHOLD = 5


class PaymentArrangementStaffAction(StrEnum):
    """Staff actions whose impact must be previewed before execution."""

    approve = "approve"
    cancel = "cancel"
    record_payment = "record_payment"


class PaymentArrangementStaffActionError(DomainError):
    """Stable error contract for staff arrangement previews and transitions."""


@dataclass(frozen=True, slots=True)
class PaymentArrangementStaffActionPreview:
    """Owner-authored facts for one exact staff action."""

    action: PaymentArrangementStaffAction
    arrangement_id: UUID
    account_id: UUID
    current_status: ArrangementStatus
    resulting_status: ArrangementStatus
    currency: str
    total_amount: Decimal
    installments_paid: int
    installments_total: int
    installment_id: UUID | None
    installment_number: int | None
    installment_amount: Decimal | None
    collection_shield_change: str
    fingerprint: str


def _staff_action_error(
    suffix: str,
    message: str,
    **details: object,
) -> PaymentArrangementStaffActionError:
    return PaymentArrangementStaffActionError(
        code=f"financial.payment_arrangements.{suffix}",
        message=message,
        details=details,
    )


def _resolve_int_setting(
    db: Session,
    key: str,
    default: int,
    *,
    minimum: int,
    maximum: int | None = None,
) -> int:
    value = settings_spec.resolve_value(db, SettingDomain.billing, key)
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    parsed = max(minimum, parsed)
    if maximum is not None:
        parsed = min(maximum, parsed)
    return parsed


def _arrangement_installment_bounds(db: Session) -> tuple[int, int]:
    minimum = _resolve_int_setting(
        db,
        "arrangement_min_installments",
        DEFAULT_MIN_INSTALLMENTS,
        minimum=MIN_ALLOWED_INSTALLMENTS,
        maximum=MAX_ALLOWED_INSTALLMENTS,
    )
    maximum = _resolve_int_setting(
        db,
        "arrangement_max_installments",
        DEFAULT_MAX_INSTALLMENTS,
        minimum=minimum,
        maximum=MAX_ALLOWED_INSTALLMENTS,
    )
    return minimum, max(minimum, maximum)


def _arrangement_default_overdue_threshold(db: Session) -> int:
    return _resolve_int_setting(
        db,
        "arrangement_default_overdue_installments",
        DEFAULT_OVERDUE_DEFAULT_THRESHOLD,
        minimum=1,
        maximum=MAX_ALLOWED_OVERDUE_DEFAULT_THRESHOLD,
    )


def _add_month_clamped(current_date: date, anchor_day: int | None = None) -> date:
    """Advance one calendar month, clamping to the month's last valid day."""
    if current_date.month == 12:
        next_year = current_date.year + 1
        next_month = 1
    else:
        next_year = current_date.year
        next_month = current_date.month + 1

    target_day = anchor_day or current_date.day
    last_day = monthrange(next_year, next_month)[1]
    return date(next_year, next_month, min(target_day, last_day))


def _calculate_end_date(
    start_date: date, frequency: PaymentFrequency, installments: int
) -> date:
    """Calculate the end date based on frequency and number of installments."""
    if frequency == PaymentFrequency.weekly:
        return start_date + timedelta(weeks=installments - 1)
    elif frequency == PaymentFrequency.biweekly:
        return start_date + timedelta(weeks=(installments - 1) * 2)
    else:  # monthly
        end_date = start_date
        for _ in range(installments - 1):
            end_date = _add_month_clamped(end_date, anchor_day=start_date.day)
        return end_date


def _calculate_next_due_date(
    current_date: date, frequency: PaymentFrequency, anchor_day: int | None = None
) -> date:
    """Calculate the next due date based on frequency."""
    if frequency == PaymentFrequency.weekly:
        return current_date + timedelta(weeks=1)
    elif frequency == PaymentFrequency.biweekly:
        return current_date + timedelta(weeks=2)
    else:  # monthly
        return _add_month_clamped(current_date, anchor_day=anchor_day)


def get_account_outstanding_balance(db: Session, subscriber_id: str) -> Decimal:
    """Sum of balance_due across the account's overdue invoices.

    Service-level equivalent of the portal-context helper; arrangements may
    only cover what is actually owed and overdue.
    """
    from app.services.invoice_collectibility import collection_blocking_balance

    return collection_blocking_balance(db, subscriber_id)


def active_arrangement_shield_reason(db: Session, account_id: object) -> str | None:
    """Return the owner-produced collection shield for one account."""
    arrangement_id = (
        db.query(PaymentArrangement.id)
        .filter(PaymentArrangement.subscriber_id == coerce_uuid(account_id))
        .filter(PaymentArrangement.status == ArrangementStatus.active)
        .filter(PaymentArrangement.is_active.is_(True))
        .order_by(PaymentArrangement.created_at.asc())
        .limit(1)
        .scalar()
    )
    return f"active payment arrangement {arrangement_id}" if arrangement_id else None


def bulk_active_arrangement_shield_reasons(
    db: Session, account_ids: set[UUID]
) -> dict[UUID, str]:
    """Return active-arrangement shields for a dunning cohort in one query."""
    if not account_ids:
        return {}
    ids = {coerce_uuid(account_id) for account_id in account_ids}
    rows = (
        db.query(PaymentArrangement.subscriber_id, PaymentArrangement.id)
        .filter(PaymentArrangement.subscriber_id.in_(ids))
        .filter(PaymentArrangement.status == ArrangementStatus.active)
        .filter(PaymentArrangement.is_active.is_(True))
        .order_by(
            PaymentArrangement.subscriber_id.asc(),
            PaymentArrangement.created_at.asc(),
        )
        .all()
    )
    reasons: dict[UUID, str] = {}
    for account_id, arrangement_id in rows:
        reasons.setdefault(account_id, f"active payment arrangement {arrangement_id}")
    return reasons


def _staff_action_state(
    db: Session,
    arrangement_id: object,
    *,
    lock: bool,
) -> tuple[PaymentArrangement, list[PaymentArrangementInstallment]]:
    arrangement_stmt = select(PaymentArrangement).where(
        PaymentArrangement.id == coerce_uuid(arrangement_id),
        PaymentArrangement.is_active.is_(True),
    )
    installment_stmt = (
        select(PaymentArrangementInstallment)
        .where(
            PaymentArrangementInstallment.arrangement_id == coerce_uuid(arrangement_id),
            PaymentArrangementInstallment.is_active.is_(True),
        )
        .order_by(PaymentArrangementInstallment.installment_number.asc())
    )
    if lock:
        arrangement_stmt = arrangement_stmt.with_for_update()
        installment_stmt = installment_stmt.with_for_update()
    arrangement = db.scalar(arrangement_stmt)
    if arrangement is None:
        raise _staff_action_error(
            "not_found",
            "Payment arrangement not found.",
            arrangement_id=str(arrangement_id),
        )
    installments = list(db.scalars(installment_stmt).all())
    return arrangement, installments


def _next_actionable_from(
    installments: list[PaymentArrangementInstallment],
) -> PaymentArrangementInstallment | None:
    actionable_statuses = {
        InstallmentStatus.overdue,
        InstallmentStatus.due,
        InstallmentStatus.pending,
    }
    return next(
        (
            installment
            for installment in installments
            if installment.status in actionable_statuses
        ),
        None,
    )


def _staff_action_fingerprint(
    *,
    arrangement: PaymentArrangement,
    installments: list[PaymentArrangementInstallment],
    action: PaymentArrangementStaffAction,
    installment: PaymentArrangementInstallment | None,
) -> str:
    payload = {
        "action": action.value,
        "arrangement": {
            "id": str(arrangement.id),
            "status": arrangement.status.value,
            "updated_at": arrangement.updated_at.isoformat(),
            "installments_paid": arrangement.installments_paid,
            "installments_total": arrangement.installments_total,
            "next_due_date": (
                arrangement.next_due_date.isoformat()
                if arrangement.next_due_date
                else None
            ),
        },
        "installments": [
            {
                "id": str(item.id),
                "number": item.installment_number,
                "status": item.status.value,
                "amount": str(item.amount),
                "due_date": item.due_date.isoformat(),
                "updated_at": item.updated_at.isoformat(),
            }
            for item in installments
        ],
        "target_installment_id": str(installment.id) if installment else None,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return sha256(canonical.encode("utf-8")).hexdigest()


def _build_staff_action_preview(
    db: Session,
    *,
    arrangement: PaymentArrangement,
    installments: list[PaymentArrangementInstallment],
    action: PaymentArrangementStaffAction,
) -> PaymentArrangementStaffActionPreview:
    installment: PaymentArrangementInstallment | None = None
    resulting_status = arrangement.status
    shield_change = "No collection-shield change."

    if action is PaymentArrangementStaffAction.approve:
        if arrangement.status is not ArrangementStatus.pending:
            raise _staff_action_error(
                "action_not_available",
                "Only a pending payment arrangement can be approved.",
                action=action.value,
                status=arrangement.status.value,
            )
        resulting_status = ArrangementStatus.active
        shield_change = (
            "The arrangement will begin shielding this account from collection."
        )
    elif action is PaymentArrangementStaffAction.cancel:
        if arrangement.status not in {
            ArrangementStatus.pending,
            ArrangementStatus.active,
        }:
            raise _staff_action_error(
                "action_not_available",
                "Only a pending or active payment arrangement can be canceled.",
                action=action.value,
                status=arrangement.status.value,
            )
        resulting_status = ArrangementStatus.canceled
        shield_change = (
            "The active collection shield will be removed."
            if arrangement.status is ArrangementStatus.active
            else "The pending arrangement will never create a collection shield."
        )
    else:
        if arrangement.status not in {
            ArrangementStatus.active,
            ArrangementStatus.defaulted,
        }:
            raise _staff_action_error(
                "action_not_available",
                "Installment payment can be recorded only on an active or defaulted arrangement.",
                action=action.value,
                status=arrangement.status.value,
            )
        installment = _next_actionable_from(installments)
        if installment is None:
            raise _staff_action_error(
                "action_not_available",
                "No unpaid installment is available to record.",
                action=action.value,
                status=arrangement.status.value,
            )
        if (
            arrangement.status is ArrangementStatus.active
            and arrangement.installments_paid + 1 >= arrangement.installments_total
        ):
            resulting_status = ArrangementStatus.completed
            shield_change = (
                "Completing the arrangement will remove its collection shield."
            )
        elif arrangement.status is ArrangementStatus.defaulted:
            shield_change = (
                "The installment will be recorded, but the defaulted arrangement "
                "will not be reactivated."
            )

    from app.services import display_format

    currency = (
        arrangement.invoice.currency
        if arrangement.invoice and arrangement.invoice.currency
        else display_format.default_currency(db)
    )
    return PaymentArrangementStaffActionPreview(
        action=action,
        arrangement_id=arrangement.id,
        account_id=arrangement.subscriber_id,
        current_status=arrangement.status,
        resulting_status=resulting_status,
        currency=display_format.currency_code(currency),
        total_amount=Decimal(str(arrangement.total_amount)),
        installments_paid=arrangement.installments_paid,
        installments_total=arrangement.installments_total,
        installment_id=installment.id if installment else None,
        installment_number=installment.installment_number if installment else None,
        installment_amount=(
            Decimal(str(installment.amount)) if installment is not None else None
        ),
        collection_shield_change=shield_change,
        fingerprint=_staff_action_fingerprint(
            arrangement=arrangement,
            installments=installments,
            action=action,
            installment=installment,
        ),
    )


def preview_staff_action(
    db: Session,
    *,
    arrangement_id: object,
    action: PaymentArrangementStaffAction,
    lock: bool = False,
) -> PaymentArrangementStaffActionPreview:
    """Preview one action from canonical arrangement and installment state."""

    arrangement, installments = _staff_action_state(
        db,
        arrangement_id,
        lock=lock,
    )
    return _build_staff_action_preview(
        db,
        arrangement=arrangement,
        installments=installments,
        action=action,
    )


def available_staff_action_previews(
    db: Session,
    *,
    arrangement_id: object,
) -> tuple[PaymentArrangementStaffActionPreview, ...]:
    """Return every action currently permitted by the arrangement owner."""

    arrangement, installments = _staff_action_state(db, arrangement_id, lock=False)
    previews: list[PaymentArrangementStaffActionPreview] = []
    for action in PaymentArrangementStaffAction:
        try:
            previews.append(
                _build_staff_action_preview(
                    db,
                    arrangement=arrangement,
                    installments=installments,
                    action=action,
                )
            )
        except PaymentArrangementStaffActionError as exc:
            if not exc.code.endswith(".action_not_available"):
                raise
    return tuple(previews)


def stage_staff_action(
    db: Session,
    *,
    preview: PaymentArrangementStaffActionPreview,
    actor_id: str,
    note: str | None,
) -> tuple[PaymentArrangement, PaymentArrangementInstallment | None]:
    """Apply a locked, freshly recomputed staff preview without committing."""

    arrangement = db.get(PaymentArrangement, preview.arrangement_id)
    if arrangement is None:
        raise _staff_action_error(
            "not_found",
            "Payment arrangement not found.",
            arrangement_id=str(preview.arrangement_id),
        )
    installment: PaymentArrangementInstallment | None = None
    if preview.action is PaymentArrangementStaffAction.approve:
        arrangement.status = ArrangementStatus.active
        arrangement.approved_at = datetime.now(UTC)
        arrangement.approved_by_user_id = actor_id
        first_installment = next(
            (
                item
                for item in arrangement.installments
                if item.installment_number == 1 and item.is_active
            ),
            None,
        )
        if first_installment and first_installment.due_date <= date.today():
            first_installment.status = InstallmentStatus.due
    elif preview.action is PaymentArrangementStaffAction.cancel:
        arrangement.status = ArrangementStatus.canceled
    else:
        if preview.installment_id is None:
            raise _staff_action_error(
                "incomplete_evidence",
                "Installment payment preview has no target installment.",
            )
        installment = db.get(
            PaymentArrangementInstallment,
            preview.installment_id,
        )
        if installment is None:
            raise _staff_action_error(
                "incomplete_evidence",
                "The previewed installment is no longer available.",
                installment_id=str(preview.installment_id),
            )
        PaymentArrangements._mark_installment_paid(
            db,
            installment,
            notes=note,
        )
    db.flush()
    return arrangement, installment


class PaymentArrangements(ListResponseMixin):
    """Service for payment arrangement CRUD operations."""

    @staticmethod
    def create(
        db: Session,
        subscriber_id: str,
        total_amount: Decimal,
        installments: int,
        frequency: str,
        start_date: date,
        invoice_id: str | None = None,
        requested_by_subscriber_id: str | None = None,
        notes: str | None = None,
    ) -> PaymentArrangement:
        """Create a new payment arrangement with installments.

        Args:
            db: Database session
            subscriber_id: The subscriber requesting the arrangement
            total_amount: Total amount to be paid
            installments: Number of installments
            frequency: Payment frequency (weekly, biweekly, monthly)
            start_date: First payment date
            invoice_id: Optional specific invoice
            requested_by_subscriber_id: Subscriber making the request
            notes: Optional notes

        Returns:
            The created payment arrangement
        """
        # Validate account
        from app.models.subscriber import Subscriber

        subscriber = db.get(Subscriber, coerce_uuid(subscriber_id))
        if not subscriber:
            raise HTTPException(status_code=404, detail="Subscriber not found")

        # Validate frequency
        try:
            freq = PaymentFrequency(frequency)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid frequency. Must be one of: {[f.value for f in PaymentFrequency]}",
            )

        # Validate invoice if provided
        invoice = None
        if invoice_id:
            from app.models.billing import Invoice

            invoice = db.get(Invoice, coerce_uuid(invoice_id))
            if not invoice:
                raise HTTPException(status_code=404, detail="Invoice not found")
            if str(invoice.account_id) != subscriber_id:
                raise HTTPException(
                    status_code=400, detail="Invoice does not belong to this subscriber"
                )

        # Validate installments
        min_installments, max_installments = _arrangement_installment_bounds(db)
        if installments < min_installments:
            raise HTTPException(
                status_code=400,
                detail=f"Minimum {min_installments} installments required",
            )
        if installments > max_installments:
            raise HTTPException(
                status_code=400,
                detail=f"Maximum {max_installments} installments allowed",
            )
        if total_amount <= 0:
            raise HTTPException(
                status_code=400, detail="Arrangement amount must be greater than 0"
            )

        # Reject overlapping arrangements: one pending/active at a time
        existing = (
            db.query(PaymentArrangement)
            .filter(PaymentArrangement.subscriber_id == coerce_uuid(subscriber_id))
            .filter(
                PaymentArrangement.status.in_(
                    [ArrangementStatus.pending, ArrangementStatus.active]
                )
            )
            .filter(PaymentArrangement.is_active.is_(True))
            .first()
        )
        if existing:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Account already has a {existing.status.value} payment arrangement"
                ),
            )

        # Amount must relate to what is actually owed
        if invoice is not None:
            invoice_balance = Decimal(str(invoice.balance_due or 0))
            if total_amount > invoice_balance:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "Arrangement amount exceeds the invoice balance due "
                        f"({invoice_balance})"
                    ),
                )
        outstanding = get_account_outstanding_balance(db, subscriber_id)
        if total_amount > outstanding:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Arrangement amount exceeds the account's outstanding "
                    f"balance ({outstanding})"
                ),
            )

        # Calculate installment amount
        installment_amount = (total_amount / Decimal(installments)).quantize(
            Decimal("0.01")
        )

        # Adjust last installment for rounding
        total_from_installments = installment_amount * installments
        rounding_diff = total_amount - total_from_installments

        # Calculate end date
        end_date = _calculate_end_date(start_date, freq, installments)

        arrangement = PaymentArrangement(
            subscriber_id=coerce_uuid(subscriber_id),
            invoice_id=coerce_uuid(invoice_id) if invoice_id else None,
            total_amount=total_amount,
            installment_amount=installment_amount,
            frequency=freq,
            installments_total=installments,
            installments_paid=0,
            start_date=start_date,
            end_date=end_date,
            next_due_date=start_date,
            status=ArrangementStatus.pending,
            requested_by_subscriber_id=coerce_uuid(requested_by_subscriber_id)
            if requested_by_subscriber_id
            else None,
            notes=notes,
        )
        db.add(arrangement)
        db.flush()

        # Create installments
        current_date = start_date
        for i in range(installments):
            amount = installment_amount
            # Add rounding difference to last installment
            if i == installments - 1:
                amount += rounding_diff

            installment = PaymentArrangementInstallment(
                arrangement_id=arrangement.id,
                installment_number=i + 1,
                amount=amount,
                due_date=current_date,
                status=InstallmentStatus.pending,
            )
            db.add(installment)

            current_date = _calculate_next_due_date(
                current_date, freq, anchor_day=start_date.day
            )

        db.commit()
        db.refresh(arrangement)

        logger.info(
            f"Created payment arrangement {arrangement.id} for subscriber {subscriber_id}, "
            f"{installments} installments of {installment_amount}"
        )
        return arrangement

    @staticmethod
    def get(db: Session, arrangement_id: str) -> PaymentArrangement:
        """Get a payment arrangement by ID."""
        arrangement = db.get(PaymentArrangement, coerce_uuid(arrangement_id))
        if not arrangement:
            raise HTTPException(status_code=404, detail="Payment arrangement not found")
        return arrangement

    @staticmethod
    def list(
        db: Session,
        account_id: str | None,
        status: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ) -> list[PaymentArrangement]:
        """List payment arrangements with filters."""
        query = db.query(PaymentArrangement).filter(
            PaymentArrangement.is_active.is_(True)
        )

        if account_id:
            query = query.filter(
                PaymentArrangement.subscriber_id == coerce_uuid(account_id)
            )

        if status:
            try:
                query = query.filter(
                    PaymentArrangement.status == ArrangementStatus(status)
                )
            except ValueError:
                raise HTTPException(status_code=400, detail=f"Invalid status: {status}")

        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {
                "created_at": PaymentArrangement.created_at,
                "start_date": PaymentArrangement.start_date,
                "status": PaymentArrangement.status,
            },
        )
        return cast(
            list[PaymentArrangement], apply_pagination(query, limit, offset).all()
        )

    @staticmethod
    def approve(
        db: Session,
        arrangement_id: str,
        approver_id: str | None = None,
        approved_by_user_id: str | None = None,
    ) -> PaymentArrangement:
        """Approve a payment arrangement.

        Args:
            db: Database session
            arrangement_id: The arrangement ID
            approver_id: Subscriber approving the arrangement (FK to subscribers)
            approved_by_user_id: SystemUser (admin) approving the arrangement

        Returns:
            The updated arrangement
        """
        arrangement = db.get(PaymentArrangement, coerce_uuid(arrangement_id))
        if not arrangement:
            raise HTTPException(status_code=404, detail="Payment arrangement not found")

        if arrangement.status != ArrangementStatus.pending:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot approve arrangement with status {arrangement.status.value}",
            )

        now = datetime.now(UTC)
        arrangement.status = ArrangementStatus.active
        arrangement.approved_at = now
        if approver_id:
            arrangement.approved_by_subscriber_id = coerce_uuid(approver_id)
        if approved_by_user_id:
            arrangement.approved_by_user_id = str(approved_by_user_id)

        # Mark first installment as due only once its due date has arrived.
        # Future-dated installments stay pending; check_overdue_installments
        # promotes them to due when the date arrives.
        first_installment = (
            db.query(PaymentArrangementInstallment)
            .filter(PaymentArrangementInstallment.arrangement_id == arrangement.id)
            .filter(PaymentArrangementInstallment.installment_number == 1)
            .first()
        )
        if first_installment and first_installment.due_date <= date.today():
            first_installment.status = InstallmentStatus.due

        db.commit()
        db.refresh(arrangement)

        logger.info(f"Approved payment arrangement {arrangement_id}")
        return arrangement

    @staticmethod
    def _mark_installment_paid(
        db: Session,
        installment: PaymentArrangementInstallment,
        payment_id: str | None = None,
        notes: str | None = None,
    ) -> None:
        """Mark a single installment paid and advance the arrangement.

        Mutates state without committing; callers commit.
        """
        today = date.today()
        # Idempotency: a paid/waived installment is terminal — re-marking it
        # would double-count installments_paid and could wrongly complete the
        # arrangement.
        if installment.status in (InstallmentStatus.paid, InstallmentStatus.waived):
            logger.info(
                "installment %s already %s; skipping re-mark",
                installment.id,
                installment.status.value,
            )
            return

        installment.status = InstallmentStatus.paid
        installment.paid_at = datetime.now(UTC)
        if payment_id:
            installment.payment_id = coerce_uuid(payment_id)
        if notes:
            installment.notes = (
                (installment.notes + "\n" + notes) if installment.notes else notes
            )

        arrangement = installment.arrangement
        # A late installment payment must not silently resurrect a terminal
        # (defaulted/canceled/completed) arrangement — record the installment
        # but do not advance/complete it.
        if arrangement.status != ArrangementStatus.active:
            logger.warning(
                "installment %s paid on non-active arrangement %s (%s); not advancing",
                installment.id,
                arrangement.id,
                arrangement.status.value,
            )
            db.flush()
            return

        arrangement.installments_paid += 1

        # Advance the next installment; it only becomes "due" once its
        # due date has arrived (otherwise the scheduled check promotes it).
        next_installment = (
            db.query(PaymentArrangementInstallment)
            .filter(PaymentArrangementInstallment.arrangement_id == arrangement.id)
            .filter(
                PaymentArrangementInstallment.installment_number
                == installment.installment_number + 1
            )
            .first()
        )
        if next_installment:
            if (
                next_installment.status == InstallmentStatus.pending
                and next_installment.due_date <= today
            ):
                next_installment.status = InstallmentStatus.due
            arrangement.next_due_date = next_installment.due_date
        else:
            arrangement.next_due_date = None

        # Check if all installments are paid
        if arrangement.installments_paid >= arrangement.installments_total:
            arrangement.status = ArrangementStatus.completed
            logger.info(f"Payment arrangement {arrangement.id} completed")

        # Sessions may run with autoflush disabled; flush so follow-up
        # queries (e.g. the progression loop) see the new statuses.
        db.flush()

    @staticmethod
    def record_installment_payment(
        db: Session,
        installment_id: str,
        payment_id: str | None = None,
        notes: str | None = None,
    ) -> PaymentArrangementInstallment:
        """Record payment for an installment.

        Args:
            db: Database session
            installment_id: The installment ID
            payment_id: Optional payment record ID
            notes: Optional note appended to the installment

        Returns:
            The updated installment
        """
        installment = db.get(PaymentArrangementInstallment, coerce_uuid(installment_id))
        if not installment:
            raise HTTPException(status_code=404, detail="Installment not found")

        if installment.status == InstallmentStatus.paid:
            raise HTTPException(status_code=400, detail="Installment already paid")

        arrangement = installment.arrangement
        if arrangement.status not in (
            ArrangementStatus.active,
            ArrangementStatus.defaulted,
        ):
            raise HTTPException(
                status_code=400,
                detail=(
                    "Cannot record payment on arrangement with status "
                    f"{arrangement.status.value}"
                ),
            )

        PaymentArrangements._mark_installment_paid(
            db, installment, payment_id=payment_id, notes=notes
        )

        db.commit()
        db.refresh(installment)

        logger.info(
            f"Recorded payment for installment {installment_id}, "
            f"arrangement {arrangement.id} now has {arrangement.installments_paid}/{arrangement.installments_total} paid"
        )
        return installment

    @staticmethod
    def check_overdue_installments(db: Session) -> dict[str, int]:
        """Promote and expire installments on active arrangements.

        Three passes over installments of active arrangements:
        1. ``due`` installments past their due date become ``overdue``.
        2. ``pending`` installments whose due date has arrived become ``due``
           (this is what activates future-dated arrangements).
        3. Arrangements with 2+ overdue installments are marked defaulted and
           an ``arrangement.defaulted`` event is emitted.

        Returns:
            Counts: installments_marked_overdue, installments_marked_due,
            arrangements_defaulted
        """
        today = date.today()

        def _active_installments(status: InstallmentStatus):
            return (
                db.query(PaymentArrangementInstallment)
                .join(PaymentArrangementInstallment.arrangement)
                .filter(PaymentArrangementInstallment.status == status)
                .filter(PaymentArrangementInstallment.is_active.is_(True))
                .filter(PaymentArrangement.status == ArrangementStatus.active)
                .filter(PaymentArrangement.is_active.is_(True))
            )

        # 1. Mark past-due "due" installments overdue
        overdue = (
            _active_installments(InstallmentStatus.due)
            .filter(PaymentArrangementInstallment.due_date < today)
            .all()
        )
        defaulted: list[PaymentArrangement] = []
        for installment in overdue:
            installment.status = InstallmentStatus.overdue
            # Sessions may run with autoflush disabled; make the status
            # change visible to the count query below.
            db.flush()

            # Arrangement defaults once the configured count is overdue.
            arrangement = installment.arrangement
            default_threshold = _arrangement_default_overdue_threshold(db)
            overdue_count = (
                db.query(PaymentArrangementInstallment)
                .filter(PaymentArrangementInstallment.arrangement_id == arrangement.id)
                .filter(
                    PaymentArrangementInstallment.status == InstallmentStatus.overdue
                )
                .count()
            )
            if (
                overdue_count >= default_threshold
                and arrangement.status == ArrangementStatus.active
            ):
                arrangement.status = ArrangementStatus.defaulted
                defaulted.append(arrangement)
                logger.warning(f"Payment arrangement {arrangement.id} defaulted")

        # 2. Promote pending installments whose due date has arrived
        promoted = (
            _active_installments(InstallmentStatus.pending)
            .filter(PaymentArrangementInstallment.due_date <= today)
            .all()
        )
        for installment in promoted:
            installment.status = InstallmentStatus.due

        if overdue or promoted:
            db.commit()
            logger.info(
                "Arrangement check: %d marked overdue, %d marked due, %d defaulted",
                len(overdue),
                len(promoted),
                len(defaulted),
            )

        # 3. Notify on defaults (after commit so handlers see final state)
        from app.services.events import emit_event
        from app.services.events.types import EventType

        for arrangement in defaulted:
            emit_event(
                db,
                EventType.arrangement_defaulted,
                {
                    "arrangement_id": str(arrangement.id),
                    "total_amount": str(arrangement.total_amount),
                    "installments_paid": arrangement.installments_paid,
                    "installments_total": arrangement.installments_total,
                },
                account_id=arrangement.subscriber_id,
            )

        return {
            "installments_marked_overdue": len(overdue),
            "installments_marked_due": len(promoted),
            "arrangements_defaulted": len(defaulted),
        }

    @staticmethod
    def cancel(
        db: Session,
        arrangement_id: str,
        notes: str | None = None,
    ) -> PaymentArrangement:
        """Cancel a payment arrangement.

        Args:
            db: Database session
            arrangement_id: The arrangement ID
            notes: Cancellation notes

        Returns:
            The updated arrangement
        """
        arrangement = db.get(PaymentArrangement, coerce_uuid(arrangement_id))
        if not arrangement:
            raise HTTPException(status_code=404, detail="Payment arrangement not found")

        if arrangement.status in (
            ArrangementStatus.completed,
            ArrangementStatus.canceled,
        ):
            raise HTTPException(
                status_code=400,
                detail=f"Cannot cancel arrangement with status {arrangement.status.value}",
            )

        arrangement.status = ArrangementStatus.canceled
        if notes:
            arrangement.notes = (
                (arrangement.notes + "\n" + notes) if arrangement.notes else notes
            )

        db.commit()
        db.refresh(arrangement)

        logger.info(f"Canceled payment arrangement {arrangement_id}")
        return arrangement


class PaymentArrangementInstallments(ListResponseMixin):
    """Service for installment operations."""

    @staticmethod
    def list(
        db: Session,
        arrangement_id: str | None,
        status: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ) -> list[PaymentArrangementInstallment]:
        """List installments with filters."""
        query = db.query(PaymentArrangementInstallment).filter(
            PaymentArrangementInstallment.is_active.is_(True)
        )

        if arrangement_id:
            query = query.filter(
                PaymentArrangementInstallment.arrangement_id
                == coerce_uuid(arrangement_id)
            )

        if status:
            try:
                query = query.filter(
                    PaymentArrangementInstallment.status == InstallmentStatus(status)
                )
            except ValueError:
                raise HTTPException(status_code=400, detail=f"Invalid status: {status}")

        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {
                "due_date": PaymentArrangementInstallment.due_date,
                "installment_number": PaymentArrangementInstallment.installment_number,
                "status": PaymentArrangementInstallment.status,
            },
        )
        return cast(
            list[PaymentArrangementInstallment],
            apply_pagination(query, limit, offset).all(),
        )


def get_next_actionable_installment(
    db: Session, arrangement_id: str
) -> PaymentArrangementInstallment | None:
    """Return the next unpaid installment a payment should apply to.

    Overdue/due installments first, then pending, in installment order.
    """
    unpaid = (
        db.query(PaymentArrangementInstallment)
        .filter(
            PaymentArrangementInstallment.arrangement_id == coerce_uuid(arrangement_id)
        )
        .filter(
            PaymentArrangementInstallment.status.in_(
                [
                    InstallmentStatus.overdue,
                    InstallmentStatus.due,
                    InstallmentStatus.pending,
                ]
            )
        )
        .filter(PaymentArrangementInstallment.is_active.is_(True))
        .order_by(PaymentArrangementInstallment.installment_number.asc())
        .first()
    )
    return unpaid


def apply_payment_to_arrangement(
    db: Session,
    account_id: str,
    amount: Decimal,
    payment_id: str | None = None,
) -> dict | None:
    """Apply a received billing payment to the account's active arrangement.

    Pays off installments in order (overdue/due first, then pending) for as
    long as the remaining amount covers the next installment in full. Partial
    remainders are not applied to an installment.

    Args:
        db: Database session
        account_id: Subscriber/account the payment belongs to
        amount: Payment amount
        payment_id: Optional billing Payment id to link on the installments

    Returns:
        Dict with installments_paid / arrangement_completed / arrangement_id,
        or None when the account has no active arrangement.
    """
    if amount is None or amount <= 0:
        return None

    arrangement = (
        db.query(PaymentArrangement)
        .filter(PaymentArrangement.subscriber_id == coerce_uuid(account_id))
        .filter(PaymentArrangement.status == ArrangementStatus.active)
        .filter(PaymentArrangement.is_active.is_(True))
        .order_by(PaymentArrangement.created_at.asc())
        .first()
    )
    if not arrangement:
        return None

    remaining = Decimal(amount)
    paid_count = 0
    while remaining > 0:
        installment = get_next_actionable_installment(db, str(arrangement.id))
        if installment is None or remaining < installment.amount:
            break
        PaymentArrangements._mark_installment_paid(
            db,
            installment,
            payment_id=payment_id,
            notes="Auto-applied from billing payment",
        )
        remaining -= installment.amount
        paid_count += 1

    if paid_count:
        db.commit()
        db.refresh(arrangement)
        logger.info(
            "Applied payment to arrangement %s: %d installment(s) paid, status now %s",
            arrangement.id,
            paid_count,
            arrangement.status.value,
        )

    return {
        "arrangement_id": str(arrangement.id),
        "installments_paid": paid_count,
        "arrangement_completed": arrangement.status == ArrangementStatus.completed,
    }


payment_arrangements = PaymentArrangements()
installments = PaymentArrangementInstallments()
