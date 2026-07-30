"""Service helpers for admin billing payment routes."""

from __future__ import annotations

import csv
import hashlib
import io
import logging
import re
import secrets
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import cast
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from app.models.billing import (
    Invoice,
    InvoiceStatus,
    Payment,
    PaymentMethod,
    PaymentMethodType,
    PaymentSettlementOrigin,
    PaymentStatus,
)
from app.models.domain_settings import SettingDomain
from app.models.subscriber import Reseller, Subscriber, SubscriberStatus
from app.schemas.billing import (
    BillingAccountPaymentRefundRequest,
    BillingAccountPaymentReversalRequest,
    PaymentAllocationConfirm,
    PaymentAllocationPreviewRequest,
    PaymentCreate,
    PaymentCreationConfirm,
    PaymentCreationPreviewRequest,
    PaymentMethodCreate,
    PaymentRefundPreviewRequest,
    PaymentRefundRequest,
    PaymentReversalPreviewRequest,
    PaymentReversalRequest,
    PaymentSettlementReconciliationRequest,
    PaymentUpdate,
)
from app.services import audit as audit_service
from app.services import billing as billing_service
from app.services import display_format, settings_spec
from app.services import subscriber as subscriber_service
from app.services import web_billing_customers as web_billing_customers_service
from app.services.audit_helpers import build_changes_metadata, log_audit_event
from app.services.list_query import (
    ListDefinition,
    ListFieldDefinition,
    ListQuery,
)
from app.services.status_presentation import payment_status_presentation

logger = logging.getLogger(__name__)
_IDEMPOTENCY_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{8,120}$")


IMPORT_HANDLERS: dict[str, dict[str, tuple[str, ...]]] = {
    "base_csv": {
        "account_number": ("account_number", "account_no", "acct_no"),
        "account_id": ("account_id", "subscriber_id"),
        "amount": ("amount", "credit", "payment"),
        "currency": ("currency",),
        "reference": ("reference", "narration", "description", "remarks"),
        "date": ("date", "transaction_date", "value_date", "posted_at"),
    },
    "zenith_bank": {
        "account_number": (
            "account_number",
            "beneficiary_account",
            "acct_no",
            "account_no",
        ),
        "account_id": ("account_id",),
        "amount": ("amount", "credit_amount", "credit", "value"),
        "currency": ("currency",),
        "reference": ("reference", "narration", "description", "details"),
        "date": ("date", "value_date", "transaction_date"),
    },
    "gtbank": {
        "account_number": ("account_number", "customer_account", "beneficiary_account"),
        "account_id": ("account_id",),
        "amount": ("amount", "credit", "paid_amount"),
        "currency": ("currency",),
        "reference": ("reference", "narration", "remarks"),
        "date": ("date", "transaction_date", "value_date"),
    },
    "access_bank": {
        "account_number": ("account_number", "acct_no", "account"),
        "account_id": ("account_id",),
        "amount": ("amount", "credit", "amount_paid"),
        "currency": ("currency",),
        "reference": ("reference", "narration", "remark"),
        "date": ("date", "value_date", "transaction_date"),
    },
    "fixed_width_basic": {
        "account_number": ("account_number",),
        "account_id": ("account_id",),
        "amount": ("amount",),
        "currency": ("currency",),
        "reference": ("reference",),
        "date": ("date",),
    },
}


def normalize_import_rows(rows: list[dict], handler: str | None) -> list[dict]:
    """Normalize raw import rows into canonical payment import shape."""
    handler_key = (handler or "base_csv").strip() or "base_csv"
    spec = IMPORT_HANDLERS.get(handler_key, IMPORT_HANDLERS["base_csv"])
    normalized: list[dict] = []
    for row in rows:
        src = {str(k).strip().lower(): v for k, v in row.items()}
        item: dict[str, object] = {}
        for target_field, aliases in spec.items():
            value = None
            for name in aliases:
                if name in src and str(src[name]).strip() != "":
                    value = src[name]
                    break
            if value is not None:
                item[target_field] = value
        normalized.append(item)
    return normalized


from app.validators.forms import parse_decimal, parse_uuid


def payment_primary_invoice_id(payment: Payment | None) -> str | None:
    """Return the invoice_id from the earliest allocation on a payment."""
    if not payment or not payment.allocations:
        return None
    allocation = min(
        payment.allocations,
        key=lambda entry: entry.created_at or datetime.min.replace(tzinfo=UTC),
    )
    return str(allocation.invoice_id)


def _payment_method_type_options() -> list[str]:
    """Return allowed payment method type values from settings."""
    spec = settings_spec.get_spec(SettingDomain.billing, "default_payment_method_type")
    if not spec or not spec.allowed:
        return []
    return sorted(spec.allowed)


def resolve_payment_method_id(
    db: Session, account_id: UUID, selection: str | None
) -> UUID | None:
    """Resolve a payment-method selector to a PaymentMethod UUID.

    *selection* may be:
    - ``None`` / empty -> ``None``
    - ``"id:<uuid>"`` -> use that UUID directly
    - ``"type:<method_type>"`` -> find-or-create by type for the account
    - a bare UUID string -> use that UUID directly
    """
    if not selection:
        return None
    if selection.startswith("id:"):
        return UUID(selection.split(":", 1)[1])
    if selection.startswith("type:"):
        method_type = selection.split(":", 1)[1]
        allowed = _payment_method_type_options()
        if allowed and method_type not in allowed:
            raise ValueError("payment_method_type is invalid")
        method_type_enum = PaymentMethodType(method_type)
        stmt = (
            select(PaymentMethod)
            .where(PaymentMethod.account_id == account_id)
            .where(PaymentMethod.method_type == method_type_enum)
            .where(PaymentMethod.is_active.is_(True))
            .order_by(PaymentMethod.created_at.desc())
            .limit(1)
        )
        method = db.scalars(stmt).first()
        if method:
            return method.id
        label = method_type.replace("_", " ").title()
        payload = PaymentMethodCreate(
            account_id=account_id,
            method_type=method_type_enum,
            label=label,
        )
        method = cast(
            PaymentMethod, billing_service.payment_methods.create(db, payload)
        )
        return method.id
    return UUID(selection)


def build_payment_detail_data(
    db: Session,
    *,
    payment_id: str,
) -> dict[str, object] | None:
    """Load payment and derived data for the detail page.

    Returns ``None`` when the payment cannot be found.
    """
    payment = billing_service.payments.get(db=db, payment_id=payment_id)
    if not payment:
        return None
    allocation_available = billing_service.payment_allocations.available_amount(
        db, payment_id
    )
    allocation_invoices: list[Invoice] = []
    if payment.account_id is not None and allocation_available > Decimal("0.00"):
        allocated_invoice_ids = {
            allocation.invoice_id for allocation in payment.allocations
        }
        allocation_invoices = (
            db.query(Invoice)
            .filter(Invoice.account_id == payment.account_id)
            .filter(Invoice.currency == payment.currency)
            .filter(Invoice.is_active.is_(True))
            .filter(
                Invoice.status.in_(
                    [
                        InvoiceStatus.issued,
                        InvoiceStatus.partially_paid,
                        InvoiceStatus.overdue,
                    ]
                )
            )
            .filter(Invoice.balance_due > Decimal("0.00"))
            .filter(Invoice.id.notin_(allocated_invoice_ids))
            .order_by(Invoice.due_at.asc().nulls_last(), Invoice.created_at.asc())
            .all()
        )
    refund_capability = (
        billing_service.consolidated_payment_refunds.capability(db, payment_id)
        if payment.billing_account_id is not None
        else billing_service.refunds.capability(db, payment_id)
    )
    reversal_capability = (
        billing_service.consolidated_payment_reversals.capability(db, payment_id)
        if payment.billing_account_id is not None
        else billing_service.reversals.capability(db, payment_id)
    )
    return {
        "payment": payment,
        "payment_status_presentation": payment_status_presentation(payment.status),
        "primary_invoice_id": payment_primary_invoice_id(payment),
        "edit_capability": billing_service.payments.edit_capability(db, payment_id),
        "refund_capability": refund_capability,
        "reversal_capability": reversal_capability,
        "allocation_invoices": allocation_invoices,
        "allocation_available": allocation_available,
        "active_page": "payments",
        "active_menu": "billing",
    }


def build_payment_edit_data(
    db: Session,
    *,
    payment_id: str,
) -> dict[str, object] | None:
    """Load payment and dependencies for the edit form.

    Returns ``None`` when the payment cannot be found.
    """
    from app.services import web_billing_payment_forms as forms_svc

    payment = billing_service.payments.get(db=db, payment_id=payment_id)
    if not payment:
        return None
    edit_capability = billing_service.payments.edit_capability(db, payment_id)
    if not edit_capability.allowed:
        raise HTTPException(status_code=409, detail=edit_capability.reason)
    selected_account = payment.account
    primary_inv_id = payment_primary_invoice_id(payment)
    if not selected_account and payment.account_id:
        try:
            selected_account = subscriber_service.accounts.get(
                db=db, account_id=str(payment.account_id)
            )
        except Exception:
            selected_account = None
    deps = forms_svc.load_edit_dependencies(
        db,
        payment=payment,
        selected_account=selected_account,
    )
    return {
        "payment": payment,
        "selected_account": selected_account,
        "primary_invoice_id": primary_inv_id,
        "deps": deps,
    }


def build_create_payload(
    *,
    account_id,
    collection_account_id: str | None,
    payment_method_id,
    amount,
    currency: str,
    status: str | None,
    memo: str | None,
    invoice_id: str | None,
) -> PaymentCreate:
    """Build payment-create payload including optional allocation."""
    allocations = None
    if invoice_id:
        from app.schemas.billing import PaymentAllocationApply

        allocations = [
            PaymentAllocationApply(
                invoice_id=UUID(invoice_id),
                amount=amount,
            )
        ]
    return PaymentCreate(
        account_id=account_id,
        collection_account_id=UUID(collection_account_id)
        if collection_account_id
        else None,
        payment_method_id=payment_method_id,
        amount=amount,
        currency=currency.strip().upper(),
        status=PaymentStatus(status) if status else PaymentStatus.pending,
        memo=memo.strip() if memo else None,
        allocations=allocations,
    )


def _owner_payment_idempotency_key(token: str | None) -> str:
    raw = (token or "").strip()
    if not raw:
        return f"admin-payment-{secrets.token_urlsafe(24)}"
    if not _IDEMPOTENCY_TOKEN_RE.fullmatch(raw):
        raise ValueError("Invalid payment submission token")
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return f"admin-payment-{digest}"


def build_update_payload(
    *,
    payment_method_id,
    amount,
    currency: str,
    status: str | None,
    memo: str | None,
) -> PaymentUpdate:
    """Build payment-update payload.

    Deliberately omits account_id: the payment scope is immutable and
    billing_service.payments.update() rejects any payload that sets it (it
    checks ``"account_id" in model_dump(exclude_unset=True)``). The edit form
    always submits the locked account, so including it here made every payment
    edit fail with a 400. The account is resolved/validated by the caller.
    """
    owner_evidence_statuses = {"refunded", "partially_refunded", "reversed"}
    if status in owner_evidence_statuses:
        return PaymentUpdate(memo=memo.strip() if memo else None)
    data: dict[str, object] = {
        "payment_method_id": payment_method_id,
        "amount": amount,
        "currency": currency.strip().upper(),
        "memo": memo.strip() if memo else None,
    }
    # Evidence-backed terminal/partial outcomes are owner projections. The
    # generic edit adapter must neither set nor resend those statuses.
    data["status"] = PaymentStatus(status) if status else PaymentStatus.pending
    return PaymentUpdate(**data)


def update_invoice_allocation_if_changed(
    db: Session,
    *,
    payment_obj,
    current_invoice_id: str | None,
    requested_invoice_id: str | None,
) -> bool:
    """Ask the payment owner to move this payment to a different invoice.

    This used to hard-delete the allocations and write a new one for the full
    payment amount, with no ledger entry, no cap against the target's balance,
    and no recalculation of either invoice — so the released invoice kept
    reading ``paid`` with no money behind it while the ledger still credited it.
    Allocation, ledger credit and both invoices' totals must move together, and
    only ``Payments.reallocate`` does that.
    """
    invoice_changed = bool(
        requested_invoice_id and requested_invoice_id != current_invoice_id
    )
    if not invoice_changed:
        return False
    billing_service.payments.reallocate(
        db, str(payment_obj.id), str(requested_invoice_id)
    )
    db.refresh(payment_obj)
    return True


def parse_import_date(date_str: str | None) -> datetime | None:
    """Parse import date in ISO/common formats."""
    if not date_str:
        return None
    try:
        return datetime.fromisoformat(date_str).replace(tzinfo=UTC)
    except ValueError:
        pass
    for fmt in ["%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y"]:
        try:
            return datetime.strptime(date_str, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def import_payments(
    db: Session,
    payments_data: list[dict],
    default_currency: str,
    *,
    payment_source: str | None = None,
    payment_method_type: str | None = None,
    pair_inactive_customers: bool = True,
) -> tuple[int, list[str]]:
    """Import payments payload list; return imported count and error messages."""
    imported_count = 0
    errors: list[str] = []
    source_label = (payment_source or "").strip()
    selected_method_type = None
    if payment_method_type:
        try:
            selected_method_type = PaymentMethodType(str(payment_method_type))
        except ValueError:
            return 0, [f"Invalid payment_method_type: {payment_method_type}"]
    for idx, row in enumerate(payments_data):
        try:
            account_id = None
            account_number = row.get("account_number")
            account_id_str = row.get("account_id")
            if account_number:
                account_stmt = select(Subscriber).where(
                    Subscriber.account_number == str(account_number)
                )
                if not pair_inactive_customers:
                    account_stmt = account_stmt.where(Subscriber.is_active.is_(True))
                account = db.scalars(account_stmt).first()
                if not account:
                    errors.append(f"Row {idx + 1}: Account not found: {account_number}")
                    continue
                account_id = account.id
            elif account_id_str:
                try:
                    account_uuid = UUID(account_id_str)
                except ValueError:
                    errors.append(f"Row {idx + 1}: Invalid account_id format")
                    continue
                account = db.get(Subscriber, account_uuid)
                if not account:
                    errors.append(f"Row {idx + 1}: Account not found: {account_id_str}")
                    continue
                if not pair_inactive_customers and not bool(account.is_active):
                    errors.append(
                        f"Row {idx + 1}: Account is inactive: {account_id_str}"
                    )
                    continue
                account_id = account.id
            else:
                errors.append(f"Row {idx + 1}: Missing account identifier")
                continue

            try:
                amount = Decimal(str(row.get("amount", "0")))
                if amount <= 0:
                    errors.append(f"Row {idx + 1}: Amount must be positive")
                    continue
            except (ValueError, InvalidOperation):
                errors.append(f"Row {idx + 1}: Invalid amount")
                continue

            paid_at = parse_import_date(row.get("date"))
            currency = row.get("currency", default_currency) or default_currency
            reference = row.get("reference", "")
            memo_bits = []
            if source_label:
                memo_bits.append(f"[{source_label}]")
            memo_bits.append("Imported payment")
            if reference:
                memo_bits.append(f"Ref: {reference}")
            memo_text = " ".join(memo_bits)
            payment_method_id = None
            if selected_method_type is not None:
                method_stmt = (
                    select(PaymentMethod)
                    .where(PaymentMethod.account_id == account_id)
                    .where(PaymentMethod.method_type == selected_method_type)
                    .where(PaymentMethod.is_active.is_(True))
                    .order_by(PaymentMethod.created_at.desc())
                    .limit(1)
                )
                method = db.scalars(method_stmt).first()
                if not method:
                    method = cast(
                        PaymentMethod,
                        billing_service.payment_methods.create(
                            db,
                            PaymentMethodCreate(
                                account_id=account_id,
                                method_type=selected_method_type,
                                label=selected_method_type.value.replace(
                                    "_", " "
                                ).title(),
                            ),
                        ),
                    )
                payment_method_id = method.id
            payload = PaymentCreate(
                account_id=account_id,
                payment_method_id=payment_method_id,
                amount=amount,
                currency=currency.upper(),
                status=PaymentStatus.succeeded,
                memo=memo_text,
            )
            payment = billing_service.payments.create(db, payload)
            if paid_at:
                payment.paid_at = paid_at
                db.commit()
            imported_count += 1
        except Exception as exc:
            errors.append(f"Row {idx + 1}: {str(exc)}")
            continue
    return imported_count, errors


# UI page contract for the admin payments list. The projection-boundary owner:
# it declares which fields are searchable/filterable/sortable, the default order
# and the allowed page sizes. The route validates and normalises request state
# through this contract; build_payments_list_data remains the read owner that
# issues the SQL. Only created_at is sortable because that is the ordering the
# read owner supports today.
PAYMENTS_LIST_DEFINITION = ListDefinition(
    key="payments",
    fields=(
        ListFieldDefinition("reference", "Reference", searchable=True),
        ListFieldDefinition("customer_ref", "Customer", filterable=True),
        ListFieldDefinition("partner_id", "Partner", filterable=True),
        ListFieldDefinition("status", "Status", filterable=True),
        ListFieldDefinition("method", "Method", filterable=True),
        ListFieldDefinition("date_range", "Date range", filterable=True),
        ListFieldDefinition("unallocated_only", "Unallocated only", filterable=True),
        ListFieldDefinition("created_at", "Created", sortable=True),
    ),
    default_sort="created_at",
    default_sort_dir="desc",
    default_per_page=25,
)


def build_payments_list_query(
    *,
    customer_ref: str | None = None,
    partner_id: str | None = None,
    status: str | None = None,
    method: str | None = None,
    search: str | None = None,
    date_range: str | None = None,
    unallocated_only: bool = False,
    sort_by: str | None = None,
    sort_dir: str | None = None,
    page: int = 1,
    per_page: int | None = None,
) -> ListQuery:
    """Normalise loose payments-list request params through the page contract.

    Drops blank filters and rejects unsupported sort fields / page sizes, so the
    read owner receives an already-validated query. ``unallocated_only`` is
    carried as a filter value so it round-trips through the contract.
    """
    return PAYMENTS_LIST_DEFINITION.build_query(
        search=search,
        filters={
            "customer_ref": customer_ref,
            "partner_id": partner_id,
            "status": status,
            "method": method,
            "date_range": date_range,
            "unallocated_only": "true" if unallocated_only else None,
        },
        sort_by=sort_by,
        sort_dir=sort_dir,
        page=page,
        per_page=per_page,
    )


def build_payments_list_data(
    db: Session,
    *,
    page: int,
    per_page: int,
    customer_ref: str | None,
    partner_id: str | None = None,
    status: str | None = None,
    method: str | None = None,
    search: str | None = None,
    date_range: str | None = None,
    unallocated_only: bool = False,
    sort_dir: str = "desc",
) -> dict[str, object]:
    """Build list/stat data for payments page.

    Read owner for the payments list. ``sort_dir`` orders the (contract-only
    sortable) ``created_at`` column; the route supplies it from the validated
    ListQuery. Other filters keep their existing semantics.
    """

    default_currency = display_format.default_currency(db)

    def _empty_status_total() -> dict[str, object]:
        return {
            "count": 0,
            "amount": 0.0,
            "amounts": {},
            "display": display_format.format_currency_groups(
                {}, empty_currency=default_currency
            ),
        }

    def _build_status_totals(filtered_subquery) -> dict[str, dict[str, object]]:  # type: ignore[no-untyped-def]
        summary: dict[str, dict[str, object]] = {
            key: _empty_status_total()
            for key in (
                "succeeded",
                "pending",
                "failed",
                "refunded",
                "partially_refunded",
                "canceled",
            )
        }
        rows = db.execute(
            select(
                filtered_subquery.c.status,
                filtered_subquery.c.currency,
                func.count().label("count"),
                func.coalesce(func.sum(filtered_subquery.c.amount), 0).label("amount"),
            ).group_by(filtered_subquery.c.status, filtered_subquery.c.currency)
        ).all()
        for status_value, currency_value, count_value, amount_value in rows:
            key = (
                status_value.value
                if hasattr(status_value, "value")
                else str(status_value)
            )
            if key not in summary:
                summary[key] = _empty_status_total()
            currency = display_format.currency_code(currency_value)
            amount = Decimal(str(amount_value or 0))
            amounts = cast(dict[str, Decimal], summary[key]["amounts"])
            amounts[currency] = amounts.get(currency, Decimal("0")) + amount
            summary[key]["count"] = int(summary[key]["count"]) + int(count_value or 0)
            summary[key]["amount"] = float(
                Decimal(str(summary[key]["amount"])) + amount
            )
        for item in summary.values():
            item["display"] = display_format.format_currency_groups(
                cast(dict[str, Decimal], item["amounts"]),
                empty_currency=default_currency,
            )
        all_amounts: dict[str, Decimal] = {}
        for item in summary.values():
            for currency, amount in cast(dict[str, Decimal], item["amounts"]).items():
                all_amounts[currency] = all_amounts.get(currency, Decimal("0")) + amount
        summary["all"] = {
            "count": sum(int(item["count"]) for item in summary.values()),
            "amount": sum(float(item["amount"]) for item in summary.values()),
            "amounts": all_amounts,
            "display": display_format.format_currency_groups(
                all_amounts,
                empty_currency=default_currency,
            ),
        }
        return summary

    def _method_enum(value: str) -> PaymentMethodType | None:
        mapping = {
            "card": PaymentMethodType.card,
            "cash": PaymentMethodType.cash,
            "check": PaymentMethodType.check,
            "transfer": PaymentMethodType.transfer,
            "bank_transfer": PaymentMethodType.transfer,
            "bank_account": PaymentMethodType.bank_account,
            "other": PaymentMethodType.other,
            "mobile": PaymentMethodType.other,
        }
        return mapping.get(value)

    def _apply_payment_filters(stmt):  # type: ignore[no-untyped-def]
        scoped = stmt.where(Payment.is_active.is_(True))
        if account_ids:
            scoped = scoped.where(Payment.account_id.in_(account_ids))
        if selected_partner_id:
            scoped = scoped.where(
                Payment.account.has(Subscriber.reseller_id == selected_partner_id)
            )
        if status:
            try:
                status_enum = PaymentStatus(status)
                scoped = scoped.where(Payment.status == status_enum)
            except ValueError:
                pass
        if method:
            method_enum = _method_enum(method)
            if method_enum:
                scoped = scoped.where(
                    Payment.payment_method.has(PaymentMethod.method_type == method_enum)
                )
        if search:
            term = f"%{search.strip()}%"
            scoped = scoped.where(
                (Payment.memo.ilike(term))
                | (Payment.external_id.ilike(term))
                | (Payment.receipt_number.ilike(term))
            )
        if unallocated_only:
            scoped = scoped.where(~Payment.allocations.any())
        if date_range in {"today", "week", "month", "quarter"}:
            now = datetime.now(UTC)
            if date_range == "today":
                start = datetime(now.year, now.month, now.day, tzinfo=UTC)
            elif date_range == "week":
                start = datetime(now.year, now.month, now.day, tzinfo=UTC) - timedelta(
                    days=now.weekday()
                )
            elif date_range == "month":
                start = now - timedelta(days=30)
            else:
                start = now - timedelta(days=90)
            scoped = scoped.where(Payment.created_at >= start)
        return scoped

    def _enrich_payment_row(payment: Payment) -> None:
        method_type = None
        if payment.payment_method and payment.payment_method.method_type:
            raw_method = payment.payment_method.method_type
            method_type = (
                raw_method.value if hasattr(raw_method, "value") else str(raw_method)
            )
        elif payment.payment_channel and payment.payment_channel.channel_type:
            raw_channel = payment.payment_channel.channel_type
            method_type = (
                raw_channel.value if hasattr(raw_channel, "value") else str(raw_channel)
            )
        else:
            method_type = "other"

        method_labels = {
            "cash": "Cash",
            "card": "Card",
            "transfer": "Bank",
            "bank_account": "Bank",
            "check": "Check",
            "other": "Online",
        }
        prefix = method_labels.get(method_type, "Payment")
        receipt = payment.receipt_number or str(payment.id)[:8]
        payment.display_number = f"{prefix} {receipt}"  # type: ignore[attr-defined]
        payment.display_method = method_labels.get(method_type, "Other")  # type: ignore[attr-defined]
        payment.narration = payment.memo or payment.external_id or "-"  # type: ignore[attr-defined]

    offset = (page - 1) * per_page
    account_ids = []
    selected_partner_id = None
    if partner_id:
        try:
            selected_partner_id = UUID(partner_id)
        except ValueError:
            selected_partner_id = None
    customer_filtered = bool(customer_ref)
    if customer_ref:
        account_ids = [
            UUID(item["id"])
            for item in web_billing_customers_service.accounts_for_customer(
                db, customer_ref
            )
        ]

    payments: list[Payment] = []
    total = 0
    status_totals = {
        key: _empty_status_total()
        for key in (
            "succeeded",
            "pending",
            "failed",
            "refunded",
            "partially_refunded",
            "canceled",
            "all",
        )
    }
    if account_ids or not customer_filtered:
        filtered_subquery = _apply_payment_filters(
            select(Payment.id, Payment.status, Payment.amount, Payment.currency)
        ).subquery()
        total = db.scalar(select(func.count()).select_from(filtered_subquery)) or 0
        status_totals = _build_status_totals(filtered_subquery)

        created_order = (
            Payment.created_at.asc() if sort_dir == "asc" else Payment.created_at.desc()
        )
        base_stmt = _apply_payment_filters(
            select(Payment).options(joinedload(Payment.account))
        ).order_by(created_order)
        payments = list(db.scalars(base_stmt.offset(offset).limit(per_page)).all())
        for payment in payments:
            _enrich_payment_row(payment)

    total_pages = (total + per_page - 1) // per_page if total > 0 else 1

    accounts = subscriber_service.accounts.list(
        db=db,
        subscriber_id=None,
        reseller_id=None,
        order_by="created_at",
        order_dir="desc",
        limit=2000,
        offset=0,
    )
    total_balance = sum((getattr(account, "balance", 0) or 0) for account in accounts)
    active_count = sum(
        1 for account in accounts if account.status == SubscriberStatus.active
    )
    suspended_count = sum(
        1 for account in accounts if account.status == SubscriberStatus.suspended
    )
    partner_options = [
        {"id": str(item.id), "name": item.name}
        for item in db.query(Reseller)
        .filter(Reseller.is_active.is_(True))
        .order_by(Reseller.name.asc())
        .all()
    ]

    return {
        "payments": payments,
        "payment_status_presentations": {
            str(payment.id): payment_status_presentation(payment.status)
            for payment in payments
        },
        "payment_status_options": [
            {
                "value": payment_status.value,
                "label": payment_status_presentation(payment_status).label,
            }
            for payment_status in PaymentStatus
        ],
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": total_pages,
        "total_balance": total_balance,
        "active_count": active_count,
        "suspended_count": suspended_count,
        "customer_ref": customer_ref,
        "selected_partner_id": str(selected_partner_id)
        if selected_partner_id
        else None,
        "partner_options": partner_options,
        "status": status,
        "method": method,
        "search": search,
        "date_range": date_range,
        "unallocated_only": unallocated_only,
        "status_totals": status_totals,
    }


def render_payments_csv(payments: list[Payment]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "payment_id",
            "display_number",
            "account_id",
            "amount",
            "currency",
            "status",
            "method",
            "narration",
            "paid_at",
            "created_at",
        ]
    )
    for payment in payments:
        method_value = getattr(payment, "display_method", None) or "Other"
        narration = getattr(payment, "narration", None) or payment.memo or ""
        writer.writerow(
            [
                str(payment.id),
                getattr(payment, "display_number", None) or "",
                str(payment.account_id) if payment.account_id else "",
                f"{Decimal(str(payment.amount or 0)):.2f}",
                display_format.currency_code(payment.currency),
                payment.status.value
                if hasattr(payment.status, "value")
                else str(payment.status or ""),
                method_value,
                narration,
                payment.paid_at.isoformat() if payment.paid_at else "",
                payment.created_at.isoformat() if payment.created_at else "",
            ]
        )
    return buffer.getvalue()


def resolve_default_currency(db: Session) -> str:
    return display_format.default_currency(db)


def build_import_result_payload(
    *, imported_count: int, errors: list[str]
) -> dict[str, object]:
    shown_errors = errors[:10] if errors else []
    return {
        "imported": imported_count,
        "errors": shown_errors,
        "shown_errors": len(shown_errors),
        "total_errors": len(errors),
        "omitted_errors": max(0, len(errors) - len(shown_errors)),
    }


def process_payment_import_payload(
    db: Session,
    body: dict,
) -> dict[str, object]:
    """Validate and import payment rows from a web JSON payload."""
    payments_data = body.get("payments", [])
    handler = body.get("handler")

    if not payments_data:
        return {"message": "No payments to import", "status_code": 400}

    default_currency = resolve_default_currency(db)
    normalized_rows = normalize_import_rows(payments_data, handler)
    payment_source = body.get("payment_source")
    payment_method_type = body.get("payment_method_type")
    file_name = body.get("file_name")
    pair_inactive_customers = bool(body.get("pair_inactive_customers", True))
    row_count = len(normalized_rows)
    total_amount = Decimal("0")
    currency_totals: dict[str, Decimal] = {}
    for row in normalized_rows:
        try:
            amount = Decimal(str(row.get("amount", 0) or 0))
        except (TypeError, ValueError, InvalidOperation):
            continue
        currency = display_format.currency_code(row.get("currency") or default_currency)
        total_amount += amount
        currency_totals[currency] = currency_totals.get(currency, Decimal("0")) + amount

    imported_count, errors = import_payments(
        db,
        normalized_rows,
        default_currency,
        payment_source=payment_source,
        payment_method_type=payment_method_type,
        pair_inactive_customers=pair_inactive_customers,
    )
    return {
        "payload": build_import_result_payload(
            imported_count=imported_count,
            errors=errors,
        ),
        "audit_metadata": {
            "imported": imported_count,
            "errors": len(errors),
            "payment_source": payment_source,
            "payment_method_type": payment_method_type,
            "file_name": file_name,
            "row_count": row_count,
            "total_amount": float(total_amount),
            "currency_totals": {
                currency: float(amount) for currency, amount in currency_totals.items()
            },
            "pair_inactive_customers": pair_inactive_customers,
            "handler": handler,
        },
        "status_code": 200,
    }


def list_payment_import_history(
    db: Session, *, limit: int = 20
) -> list[dict[str, object]]:
    return list_payment_import_history_filtered(
        db,
        limit=limit,
        handler=None,
        status=None,
        date_range=None,
    )


def list_payment_import_history_filtered(
    db: Session,
    *,
    limit: int = 20,
    handler: str | None = None,
    status: str | None = None,
    date_range: str | None = None,
) -> list[dict[str, object]]:
    now = datetime.now(UTC)
    start = None
    if date_range in {"today", "week", "month", "quarter"}:
        if date_range == "today":
            start = datetime(now.year, now.month, now.day, tzinfo=UTC)
        elif date_range == "week":
            start = datetime(now.year, now.month, now.day, tzinfo=UTC) - timedelta(
                days=now.weekday()
            )
        elif date_range == "month":
            start = datetime(now.year, now.month, 1, tzinfo=UTC)
        else:
            quarter_start_month = ((now.month - 1) // 3) * 3 + 1
            start = datetime(now.year, quarter_start_month, 1, tzinfo=UTC)

    events = audit_service.audit_events.list(
        db=db,
        actor_id=None,
        actor_type=None,
        action="import",
        entity_type="payment",
        entity_id=None,
        request_id=None,
        is_success=None,
        status_code=None,
        is_active=None,
        order_by="occurred_at",
        order_dir="desc",
        limit=limit,
        offset=0,
    )
    rows: list[dict[str, object]] = []
    default_currency = resolve_default_currency(db)
    for event in events:
        if start is not None and event.occurred_at and event.occurred_at < start:
            continue
        metadata = getattr(event, "metadata_", None) or {}
        row_handler = str(metadata.get("handler") or "base_csv")
        if handler and row_handler != handler:
            continue
        imported = int(metadata.get("imported", 0) or 0)
        errors = int(metadata.get("errors", 0) or 0)
        if imported > 0 and errors == 0:
            row_status = "success"
        elif imported > 0 and errors > 0:
            row_status = "partial"
        elif imported == 0 and errors > 0:
            row_status = "failed"
        else:
            row_status = "none"
        if status in {"success", "partial", "failed"} and row_status != status:
            continue
        row_count = int(metadata.get("row_count", imported + errors) or 0)
        total_amount = Decimal(str(metadata.get("total_amount", 0) or 0))
        raw_currency_totals = metadata.get("currency_totals")
        currency_totals: dict[str, Decimal] = {}
        if isinstance(raw_currency_totals, dict):
            for currency, amount in raw_currency_totals.items():
                currency_totals[display_format.currency_code(currency)] = Decimal(
                    str(amount or 0)
                )
        if not currency_totals:
            currency_totals[
                display_format.currency_code(
                    metadata.get("currency") or default_currency
                )
            ] = total_amount
        rows.append(
            {
                "occurred_at": event.occurred_at,
                "file_name": metadata.get("file_name") or "-",
                "handler": row_handler,
                "payment_source": metadata.get("payment_source") or "-",
                "payment_method_type": metadata.get("payment_method_type") or "-",
                "status": row_status,
                "row_count": row_count,
                "matched_count": imported,
                "unmatched_count": errors,
                "total_amount": float(total_amount),
                "currency_totals": currency_totals,
                "total_display": display_format.format_currency_groups(
                    currency_totals,
                    empty_currency=default_currency,
                ),
            }
        )
    return rows


def render_payment_import_history_csv(rows: list[dict[str, object]]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "occurred_at",
            "file_name",
            "handler",
            "status",
            "payment_source",
            "payment_method_type",
            "row_count",
            "matched_count",
            "unmatched_count",
            "total_amount",
        ]
    )
    for row in rows:
        writer.writerow(
            [
                row.get("occurred_at").isoformat() if row.get("occurred_at") else "",
                row.get("file_name", ""),
                row.get("handler", ""),
                row.get("status", ""),
                row.get("payment_source", ""),
                row.get("payment_method_type", ""),
                row.get("row_count", 0),
                row.get("matched_count", 0),
                row.get("unmatched_count", 0),
                f"{Decimal(str(row.get('total_amount', 0) or 0)):.2f}",
            ]
        )
    return buffer.getvalue()


def import_template_csv() -> str:
    return """account_number,amount,currency,reference,date
ACC-001,15000,NGN,TRF-001,2024-01-15
ACC-002,25000,NGN,TRF-002,2024-01-16
"""


def _prepare_payment_create(
    db: Session,
    *,
    account_id: str | None,
    amount: str,
    currency: str,
    status: str | None,
    invoice_id: str | None,
    collection_account_id: str | None,
    payment_method_id: str | None,
    memo: str | None,
) -> dict[str, object]:
    """Resolve adapter inputs into the canonical payment owner request."""
    from app.services import web_billing_payment_forms as forms_svc

    resolved_invoice = forms_svc.resolve_invoice(db, invoice_id)
    balance_value: str | None = None
    balance_display: str | None = None
    if resolved_invoice:
        balance_value, balance_display = forms_svc.invoice_balance_info(
            resolved_invoice
        )
    resolved_account_id = account_id or (
        str(resolved_invoice.account_id) if resolved_invoice else None
    )
    if not resolved_account_id:
        raise ValueError("account_id is required")
    parsed_account_id = parse_uuid(resolved_account_id, "account_id")
    effective_currency = (
        resolved_invoice.currency
        if resolved_invoice and resolved_invoice.currency
        else currency
    )
    parsed_amount = parse_decimal(amount, "amount")
    payload = build_create_payload(
        account_id=parsed_account_id,
        collection_account_id=collection_account_id,
        payment_method_id=resolve_payment_method_id(
            db, parsed_account_id, payment_method_id
        ),
        amount=parsed_amount,
        currency=effective_currency,
        status=status,
        memo=memo,
        invoice_id=invoice_id,
    )
    return {
        "payload": payload,
        "resolved_invoice": resolved_invoice,
        "balance_value": balance_value,
        "balance_display": balance_display,
    }


def preview_payment_create(
    db: Session,
    *,
    account_id: str | None,
    amount: str,
    currency: str,
    status: str | None,
    invoice_id: str | None,
    collection_account_id: str | None,
    payment_method_id: str | None,
    memo: str | None,
) -> dict[str, object]:
    prepared = _prepare_payment_create(
        db,
        account_id=account_id,
        amount=amount,
        currency=currency,
        status=status,
        invoice_id=invoice_id,
        collection_account_id=collection_account_id,
        payment_method_id=payment_method_id,
        memo=memo,
    )
    payload = cast(PaymentCreate, prepared["payload"])
    preview = billing_service.payments.preview_creation(
        db,
        PaymentCreationPreviewRequest(
            **payload.model_dump(),
            auto_allocate=True,
        ),
    )
    return {
        **prepared,
        "preview": preview,
    }


def process_payment_create(
    db: Session,
    *,
    account_id: str | None,
    amount: str,
    currency: str,
    status: str | None,
    invoice_id: str | None,
    collection_account_id: str | None,
    payment_method_id: str | None,
    memo: str | None,
    idempotency_token: str | None = None,
    preview_fingerprint: str | None = None,
) -> dict[str, object]:
    """Confirm a reviewed payment preview through the canonical owner."""
    prepared = _prepare_payment_create(
        db,
        account_id=account_id,
        amount=amount,
        currency=currency,
        status=status,
        invoice_id=invoice_id,
        collection_account_id=collection_account_id,
        payment_method_id=payment_method_id,
        memo=memo,
    )
    payload = cast(PaymentCreate, prepared["payload"])
    owner_key = _owner_payment_idempotency_key(idempotency_token)
    result = None
    if preview_fingerprint is None:
        replay = billing_service.payments.replay_creation_request(
            db,
            payload,
            auto_allocate=True,
            idempotency_key=owner_key,
        )
        if replay is not None:
            result = replay
        else:
            preview = billing_service.payments.preview_creation(
                db,
                PaymentCreationPreviewRequest(
                    **payload.model_dump(),
                    auto_allocate=True,
                ),
            )
            preview_fingerprint = preview.fingerprint
    if preview_fingerprint is not None and result is None:
        result = billing_service.payments.confirm_creation(
            db,
            PaymentCreationConfirm(
                **payload.model_dump(),
                auto_allocate=True,
                preview_fingerprint=preview_fingerprint,
                idempotency_key=owner_key,
            ),
        )
    assert result is not None
    return {
        "payment": result.payment,
        "resolved_invoice": prepared["resolved_invoice"],
        "balance_value": prepared["balance_value"],
        "balance_display": prepared["balance_display"],
        "idempotent_replay": result.idempotent_replay,
        "audit_metadata": result.audit_metadata(),
    }


def _actor_id(request) -> str | None:
    from app.web.admin import get_current_user

    current_user = get_current_user(request)
    return str(current_user.get("subscriber_id")) if current_user else None


def process_payment_create_with_audit(
    db: Session,
    request,
    *,
    account_id: str | None,
    amount: str,
    currency: str,
    status: str | None,
    invoice_id: str | None,
    collection_account_id: str | None,
    payment_method_id: str | None,
    memo: str | None,
    idempotency_token: str | None = None,
    preview_fingerprint: str | None = None,
) -> dict[str, object]:
    result = process_payment_create(
        db,
        account_id=account_id,
        amount=amount,
        currency=currency,
        status=status,
        invoice_id=invoice_id,
        collection_account_id=collection_account_id,
        payment_method_id=payment_method_id,
        memo=memo,
        idempotency_token=idempotency_token,
        preview_fingerprint=preview_fingerprint,
    )
    payment = cast(Payment, result["payment"])
    if not result.get("idempotent_replay"):
        log_audit_event(
            db=db,
            request=request,
            action="create",
            entity_type="payment",
            entity_id=str(payment.id),
            actor_id=_actor_id(request),
            metadata=cast(dict[str, object], result.get("audit_metadata") or {}),
        )
    return result


def process_payment_update(
    db: Session,
    *,
    payment_id: str,
    account_id: str | None,
    amount: str,
    currency: str,
    status: str | None,
    invoice_id: str | None,
    payment_method_id: str | None,
    memo: str | None,
) -> dict[str, object]:
    """Validate inputs, update a payment, and return before/after snapshots."""
    from app.services import web_billing_payment_forms as forms_svc

    before = billing_service.payments.get(db=db, payment_id=payment_id)
    current_invoice_id = payment_primary_invoice_id(before)
    resolved_invoice = forms_svc.resolve_invoice(db, invoice_id)
    resolved_account_id = account_id or (
        str(resolved_invoice.account_id) if resolved_invoice else None
    )
    if not resolved_account_id:
        resolved_account_id = str(before.account_id) if before else None
    if not resolved_account_id:
        raise ValueError("account_id is required")
    parsed_account_id = parse_uuid(resolved_account_id, "account_id")
    effective_currency = (
        resolved_invoice.currency
        if resolved_invoice and resolved_invoice.currency
        else currency
    )
    requested_invoice_id = str(resolved_invoice.id) if resolved_invoice else None
    invoice_changed = (
        requested_invoice_id and requested_invoice_id != current_invoice_id
    )
    payload = build_update_payload(
        payment_method_id=resolve_payment_method_id(
            db, parsed_account_id, payment_method_id
        ),
        amount=parse_decimal(amount, "amount"),
        currency=effective_currency,
        status=status,
        memo=memo,
    )
    billing_service.payments.update(db, payment_id, payload)
    if invoice_changed and requested_invoice_id:
        payment_obj = billing_service.payments.get(db=db, payment_id=payment_id)
        update_invoice_allocation_if_changed(
            db,
            payment_obj=payment_obj,
            current_invoice_id=current_invoice_id,
            requested_invoice_id=requested_invoice_id,
        )
    after = billing_service.payments.get(db=db, payment_id=payment_id)
    return {
        "before": before,
        "after": after,
        "audit_metadata": build_changes_metadata(before, after),
    }


def process_payment_update_with_audit(
    db: Session,
    request,
    *,
    payment_id: str,
    account_id: str | None,
    amount: str,
    currency: str,
    status: str | None,
    invoice_id: str | None,
    payment_method_id: str | None,
    memo: str | None,
) -> dict[str, object]:
    result = process_payment_update(
        db,
        payment_id=payment_id,
        account_id=account_id,
        amount=amount,
        currency=currency,
        status=status,
        invoice_id=invoice_id,
        payment_method_id=payment_method_id,
        memo=memo,
    )
    log_audit_event(
        db=db,
        request=request,
        action="update",
        entity_type="payment",
        entity_id=payment_id,
        actor_id=_actor_id(request),
        metadata=cast(dict[str, object] | None, result.get("audit_metadata")),
    )
    return result


def preview_payment_refund(
    db: Session,
    *,
    payment_id: str,
    amount: str | None,
    reason: str | None,
) -> dict[str, object]:
    """Build the owner-authored financial and access consequence preview."""
    try:
        parsed_amount = Decimal(amount) if amount and amount.strip() else None
    except InvalidOperation as exc:
        raise HTTPException(status_code=400, detail="Invalid refund amount") from exc
    normalized_reason = reason.strip() if reason and reason.strip() else None
    request = PaymentRefundPreviewRequest(
        amount=parsed_amount,
        reason=normalized_reason,
    )
    payment = billing_service.payments.get(db, payment_id)
    preview = (
        billing_service.consolidated_payment_refunds.preview(db, payment_id, request)
        if payment.billing_account_id is not None
        else billing_service.refunds.preview(db, payment_id, request)
    )
    return {
        "preview": preview,
        "reason": normalized_reason,
        "idempotency_key": secrets.token_urlsafe(24),
    }


def preview_payment_allocation(
    db: Session,
    *,
    payment_id: str,
    invoice_id: str,
    amount: str,
) -> dict[str, object]:
    """Build the exact account-credit-to-receivable transfer preview."""
    try:
        parsed_amount = Decimal(amount)
    except InvalidOperation as exc:
        raise HTTPException(
            status_code=400, detail="Invalid allocation amount"
        ) from exc
    request = PaymentAllocationPreviewRequest(
        payment_id=UUID(payment_id),
        invoice_id=UUID(invoice_id),
        amount=parsed_amount,
    )
    preview = billing_service.payment_allocations.preview(db, request)
    return {
        "preview": preview,
        "idempotency_key": secrets.token_urlsafe(24),
    }


def process_payment_allocation_with_audit(
    db: Session,
    request,
    *,
    payment_id: str,
    invoice_id: str,
    amount: str,
    preview_fingerprint: str,
    idempotency_key: str,
):
    """Confirm a reviewed allocation transfer through its owner."""
    result = billing_service.payment_allocations.confirm(
        db,
        PaymentAllocationConfirm(
            payment_id=UUID(payment_id),
            invoice_id=UUID(invoice_id),
            amount=Decimal(amount),
            preview_fingerprint=preview_fingerprint,
            idempotency_key=idempotency_key,
        ),
    )
    if not result.idempotent_replay:
        log_audit_event(
            db=db,
            request=request,
            action="allocate",
            entity_type="payment",
            entity_id=payment_id,
            actor_id=_actor_id(request),
            metadata=result.audit_metadata(),
        )
    return result


def preview_payment_settlement(db: Session, *, payment_id: str) -> dict[str, object]:
    """Build the owner-authored pending-to-settled consequence preview."""
    preview = billing_service.payments.preview_settlement(db, payment_id)
    return {
        "preview": preview,
        "idempotency_key": secrets.token_urlsafe(24),
    }


def process_payment_settlement_with_audit(
    db: Session,
    request,
    *,
    payment_id: str,
    preview_fingerprint: str,
    idempotency_key: str,
):
    """Confirm reviewed settlement through the canonical payment owner."""
    result = billing_service.payments.settle(
        db,
        payment_id,
        preview_fingerprint=preview_fingerprint,
        idempotency_key=idempotency_key,
        origin=PaymentSettlementOrigin.manual,
    )
    if not result.idempotent_replay:
        log_audit_event(
            db=db,
            request=request,
            action="settle",
            entity_type="payment",
            entity_id=payment_id,
            actor_id=_actor_id(request),
            metadata=result.audit_metadata(),
        )
    return result


def reconcile_payment_settlement_evidence_with_audit(
    db: Session,
    request,
    *,
    payment_id: str,
    payload: PaymentSettlementReconciliationRequest,
):
    """Reconcile reviewed historical evidence and record the admin action."""
    result = billing_service.payments.reconcile_settlement_evidence(
        db, payment_id, payload
    )
    log_audit_event(
        db=db,
        request=request,
        actor_id=_actor_id(request),
        action="reconcile_settlement_evidence",
        entity_type="payment",
        entity_id=payment_id,
        metadata={
            "settlement_id": str(result.id),
            "reason": payload.reason,
            "unallocated_ledger_entry_id": (
                str(payload.unallocated_ledger_entry_id)
                if payload.unallocated_ledger_entry_id
                else None
            ),
        },
    )
    return result


def process_payment_refund_with_audit(
    db: Session,
    request,
    *,
    payment_id: str,
    amount: str,
    reason: str | None,
    preview_fingerprint: str,
    idempotency_key: str,
):
    """Confirm a previewed completed refund through the canonical owner."""
    payment = billing_service.payments.get(db, payment_id)
    payload_values = {
        "amount": Decimal(amount),
        "reason": reason.strip() if reason and reason.strip() else None,
        "preview_fingerprint": preview_fingerprint,
        "idempotency_key": idempotency_key,
    }
    result = (
        billing_service.consolidated_payment_refunds.confirm(
            db,
            payment_id,
            BillingAccountPaymentRefundRequest(**payload_values),
            actor_id=_actor_id(request),
        )
        if payment.billing_account_id is not None
        else billing_service.refunds.process_with_evidence(
            db, payment_id, PaymentRefundRequest(**payload_values)
        )
    )
    log_audit_event(
        db=db,
        request=request,
        action="refund",
        entity_type="payment",
        entity_id=payment_id,
        actor_id=_actor_id(request),
        metadata=result.audit_metadata(),
    )
    return result


def preview_payment_reversal(
    db: Session,
    *,
    payment_id: str,
    reason: str,
) -> dict[str, object]:
    """Build the owner-authored chargeback/bank-reversal preview."""
    request = PaymentReversalPreviewRequest(reason=reason.strip())
    payment = billing_service.payments.get(db, payment_id)
    preview = (
        billing_service.consolidated_payment_reversals.preview(db, payment_id, request)
        if payment.billing_account_id is not None
        else billing_service.reversals.preview(db, payment_id, request)
    )
    return {
        "preview": preview,
        "reason": request.reason,
        "idempotency_key": secrets.token_urlsafe(24),
    }


def process_payment_reversal_with_audit(
    db: Session,
    request,
    *,
    payment_id: str,
    reason: str,
    preview_fingerprint: str,
    idempotency_key: str,
):
    """Confirm a previewed payment reversal through its canonical owner."""
    payment = billing_service.payments.get(db, payment_id)
    payload_values = {
        "reason": reason.strip(),
        "preview_fingerprint": preview_fingerprint,
        "idempotency_key": idempotency_key,
    }
    result = (
        billing_service.consolidated_payment_reversals.confirm(
            db,
            payment_id,
            BillingAccountPaymentReversalRequest(**payload_values),
            actor_id=_actor_id(request),
        )
        if payment.billing_account_id is not None
        else billing_service.reversals.process_with_evidence(
            db, payment_id, PaymentReversalRequest(**payload_values)
        )
    )
    log_audit_event(
        db=db,
        request=request,
        action="reverse",
        entity_type="payment",
        entity_id=payment_id,
        actor_id=_actor_id(request),
        metadata=result.audit_metadata(),
    )
    return result


def process_payment_import_payload_with_audit(
    db: Session,
    request,
    body: dict,
) -> dict[str, object]:
    result = process_payment_import_payload(db, body)
    if int(result.get("status_code", 200)) == 200:
        log_audit_event(
            db=db,
            request=request,
            action="import",
            entity_type="payment",
            entity_id="bulk",
            actor_id=_actor_id(request),
            metadata=cast(dict[str, object], result.get("audit_metadata") or {}),
        )
    return result
