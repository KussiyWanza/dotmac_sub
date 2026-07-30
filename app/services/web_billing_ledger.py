"""Service helpers for billing ledger web routes."""

from __future__ import annotations

import csv
import io
import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from types import SimpleNamespace
from urllib.parse import urlencode
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.orm import joinedload

from app.models.billing import (
    Invoice,
    InvoiceStatus,
    LedgerEntry,
    LedgerEntryType,
    LedgerSource,
)
from app.models.subscriber import Reseller, Subscriber
from app.schemas.status_presentation import StatusTone
from app.services import display_format
from app.services import web_billing_customers as web_billing_customers_service
from app.services.common import validate_enum
from app.services.ui_contracts import Kpi, StateValue

logger = logging.getLogger(__name__)


def _add_grouped_amount(
    amounts: dict[str, Decimal], *, currency: object | None, amount: object
) -> None:
    code = display_format.currency_code(currency)
    amounts[code] = amounts.get(code, Decimal("0")) + Decimal(str(amount or 0))


_CATEGORY_SOURCES: dict[str, tuple[LedgerSource, ...]] = {
    "service": (LedgerSource.invoice,),
    "payment": (LedgerSource.payment,),
    "credit_note": (LedgerSource.credit_note,),
    "adjustment": (LedgerSource.adjustment,),
    "refund": (LedgerSource.refund,),
    "other": (LedgerSource.other,),
}

# Legacy cutover: the migrated ledger carries invoice debits only through this
# instant. Native invoice issuance does NOT post a debit to ledger_entries (the
# invoice row itself is the AR record), so without merging post-cutover invoices
# the ledger view looks frozen at March 2026. Invoices issued on/before the
# cutover are already represented by migrated ledger rows — including them would
# double-count, so only issued_at strictly after this is merged.
_LEDGER_CUTOVER = datetime(2026, 3, 15, 23, 59, 59, tzinfo=UTC)


@dataclass(frozen=True)
class LedgerDateRange:
    start: datetime | None
    end: datetime | None
    start_date: date | None
    end_date: date | None


def _parse_date_range(
    start_date: str | None,
    end_date: str | None,
) -> LedgerDateRange:
    start_value = (start_date or "").strip()
    end_value = (end_date or "").strip()
    try:
        start_d = date.fromisoformat(start_value) if start_value else None
        end_d = date.fromisoformat(end_value) if end_value else None
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail="start_date and end_date must be ISO dates"
        ) from exc

    if start_d and end_d and start_d > end_d:
        raise HTTPException(
            status_code=400,
            detail="start_date must be before or equal to end_date",
        )

    return LedgerDateRange(
        start=datetime.combine(start_d, time.min, tzinfo=UTC) if start_d else None,
        end=(
            datetime.combine(end_d + timedelta(days=1), time.min, tzinfo=UTC)
            if end_d
            else None
        ),
        start_date=start_d,
        end_date=end_d,
    )


def _invoice_as_ledger_row(invoice: Invoice) -> SimpleNamespace:
    """Adapt an Invoice into a display row matching the ledger template/CSV.

    Display-only: the amount shown is the invoice total (the charge); payments
    against it are already in ledger_entries as credits. Account balances/AR are
    NOT derived from this view — they come from invoices.balance_due.
    """
    label = invoice.memo or (
        f"Invoice {invoice.invoice_number}" if invoice.invoice_number else "Invoice"
    )
    return SimpleNamespace(
        id=invoice.id,
        account_id=invoice.account_id,
        account=invoice.account,
        entry_type=SimpleNamespace(value="debit"),
        source=SimpleNamespace(value="invoice"),
        amount=invoice.total,
        currency=display_format.currency_code(invoice.currency),
        memo=label,
        effective_date=invoice.issued_at,
        created_at=invoice.created_at,
        is_active=True,
    )


def _display_date(entry) -> datetime:
    return getattr(entry, "effective_date", None) or entry.created_at


def _ledger_cohort_url(
    *,
    entry_type: str | None,
    customer_ref: str | None,
    start_date: str | None,
    end_date: str | None,
    category: str | None,
    partner_id: str | None,
) -> str:
    """Drill-down to the ledger filtered to exactly the cohort a KPI counts.

    The owner supplies this so a summary tile and the rows it summarises can
    never diverge (KPI-parity rule): the active filters travel with the link
    and only ``entry_type`` narrows it to credits or debits.
    """
    params = {
        "entry_type": entry_type,
        "customer_ref": customer_ref,
        "start_date": start_date,
        "end_date": end_date,
        "category": category,
        "partner_id": partner_id,
    }
    query = urlencode({key: value for key, value in params.items() if value})
    return "/admin/billing/ledger" + (f"?{query}" if query else "")


def build_ledger_entries_data(
    db,
    *,
    customer_ref: str | None,
    entry_type: str | None,
    start_date: str | None = None,
    end_date: str | None = None,
    category: str | None = None,
    partner_id: str | None = None,
    limit: int = 200,
) -> dict[str, object]:
    date_range = _parse_date_range(start_date, end_date)
    default_currency = display_format.default_currency(db)

    account_ids = []
    if customer_ref:
        account_ids = [
            UUID(item["id"])
            for item in web_billing_customers_service.accounts_for_customer(
                db, customer_ref
            )
        ]

    entries = []
    credit_count = 0
    debit_count = 0
    credit_amounts: dict[str, Decimal] = {}
    debit_amounts: dict[str, Decimal] = {}
    selected_partner_id = (partner_id or "").strip() or None
    # Only offer partners that actually own ledger activity. Listing every active
    # reseller surfaces empty/test partners (e.g. ones with zero subscribers),
    # and selecting one returns a blank ledger that reads as a broken filter.
    has_ledger_activity = (
        db.query(LedgerEntry.id)
        .join(Subscriber, Subscriber.id == LedgerEntry.account_id)
        .filter(Subscriber.reseller_id == Reseller.id)
        .filter(LedgerEntry.is_active.is_(True))
        .exists()
    )
    partner_options = [
        {"id": str(item.id), "name": item.name}
        for item in db.query(Reseller)
        .filter(Reseller.is_active.is_(True))
        .filter(has_ledger_activity)
        .order_by(Reseller.name.asc())
        .all()
    ]
    want_type = (
        validate_enum(entry_type, LedgerEntryType, "entry_type") if entry_type else None
    )
    selected_category = (category or "").strip().lower()

    if account_ids or not customer_ref:
        # Build one uncapped base cohort for aggregates. The displayed rows may
        # be capped for usability, but headline money must never be derived from
        # that page. ``entry_type`` is deliberately applied only to rows: the
        # credit/debit cards each summarize and link to their own exact side of
        # the otherwise-identical filter set.
        ledger_query = db.query(LedgerEntry).filter(LedgerEntry.is_active.is_(True))
        if account_ids:
            ledger_query = ledger_query.filter(LedgerEntry.account_id.in_(account_ids))
        if selected_partner_id:
            ledger_query = ledger_query.filter(
                LedgerEntry.account.has(
                    Subscriber.reseller_id == UUID(selected_partner_id)
                )
            )
        if selected_category in _CATEGORY_SOURCES:
            ledger_query = ledger_query.filter(
                LedgerEntry.source.in_(_CATEGORY_SOURCES[selected_category])
            )
        ledger_date = func.coalesce(LedgerEntry.effective_date, LedgerEntry.created_at)
        if date_range.start is not None:
            ledger_query = ledger_query.filter(ledger_date >= date_range.start)
        if date_range.end is not None:
            ledger_query = ledger_query.filter(ledger_date < date_range.end)

        for row in (
            ledger_query.with_entities(
                LedgerEntry.entry_type,
                LedgerEntry.currency,
                func.count(LedgerEntry.id).label("entry_count"),
                func.coalesce(func.sum(LedgerEntry.amount), Decimal("0")).label(
                    "amount_total"
                ),
            )
            .group_by(LedgerEntry.entry_type, LedgerEntry.currency)
            .all()
        ):
            target = (
                credit_amounts
                if row.entry_type == LedgerEntryType.credit
                else debit_amounts
            )
            _add_grouped_amount(
                target,
                currency=row.currency,
                amount=row.amount_total,
            )
            if row.entry_type == LedgerEntryType.credit:
                credit_count += int(row.entry_count or 0)
            else:
                debit_count += int(row.entry_count or 0)

        row_query = ledger_query.options(joinedload(LedgerEntry.account))
        if want_type is not None:
            row_query = row_query.filter(LedgerEntry.entry_type == want_type)
        ledger_rows = row_query.order_by(ledger_date.desc()).limit(limit).all()

        # Merge post-cutover invoices as synthetic debit rows so the ledger view
        # reflects ongoing billing (native invoices don't post to ledger_entries).
        # Invoices are debits categorised as "service", so only include them when
        # the active filters don't exclude that combination.
        invoice_rows: list[SimpleNamespace] = []
        if selected_category in ("", "service"):
            inv_q = (
                db.query(Invoice)
                .filter(Invoice.is_active.is_(True))
                .filter(Invoice.is_proforma.is_(False))
                .filter(
                    Invoice.status.notin_([InvoiceStatus.void, InvoiceStatus.draft])
                )
                .filter(Invoice.issued_at.isnot(None))
                .filter(Invoice.issued_at > _LEDGER_CUTOVER)
            )
            if account_ids:
                inv_q = inv_q.filter(Invoice.account_id.in_(account_ids))
            if selected_partner_id:
                inv_q = inv_q.filter(
                    Invoice.account.has(
                        Subscriber.reseller_id == UUID(selected_partner_id)
                    )
                )
            if date_range.start is not None:
                inv_q = inv_q.filter(Invoice.issued_at >= date_range.start)
            if date_range.end is not None:
                inv_q = inv_q.filter(Invoice.issued_at < date_range.end)
            for row in (
                inv_q.with_entities(
                    Invoice.currency,
                    func.count(Invoice.id).label("invoice_count"),
                    func.coalesce(func.sum(Invoice.total), Decimal("0")).label(
                        "amount_total"
                    ),
                )
                .group_by(Invoice.currency)
                .all()
            ):
                _add_grouped_amount(
                    debit_amounts,
                    currency=row.currency,
                    amount=row.amount_total,
                )
                debit_count += int(row.invoice_count or 0)
            if want_type in (None, LedgerEntryType.debit):
                invoice_rows = [
                    _invoice_as_ledger_row(invoice)
                    for invoice in inv_q.options(joinedload(Invoice.account))
                    .order_by(Invoice.issued_at.desc())
                    .limit(limit)
                    .all()
                ]

        entries = sorted(
            [*ledger_rows, *invoice_rows],
            key=_display_date,
            reverse=True,
        )[:limit]

    net_amounts = dict(credit_amounts)
    for currency, amount in debit_amounts.items():
        net_amounts[currency] = net_amounts.get(currency, Decimal("0")) - amount
    credit_total = sum(float(amount) for amount in credit_amounts.values())
    debit_total = sum(float(amount) for amount in debit_amounts.values())
    ledger_totals = {
        "credit_count": credit_count,
        "credit_total": credit_total,
        "credit_amounts": credit_amounts,
        "credit_display": display_format.format_currency_groups(
            credit_amounts, empty_currency=default_currency
        ),
        "debit_count": debit_count,
        "debit_total": debit_total,
        "debit_amounts": debit_amounts,
        "debit_display": display_format.format_currency_groups(
            debit_amounts, empty_currency=default_currency
        ),
        "net_total": credit_total - debit_total,
        "net_amounts": net_amounts,
        "net_display": display_format.format_currency_groups(
            net_amounts, empty_currency=default_currency
        ),
    }

    # Headline tiles as KPI contracts: each drills into the exact filtered
    # cohort it counts. "Credits"/"Debits" narrow by entry_type; "Net" keeps the
    # active filter set (it is the balance across both sides, not one type).
    ledger_kpis = {
        "credits": Kpi(
            label="Total Credits",
            value=StateValue.present(ledger_totals["credit_display"]),
            cohort_url=_ledger_cohort_url(
                entry_type=LedgerEntryType.credit.value,
                customer_ref=customer_ref,
                start_date=start_date,
                end_date=end_date,
                category=category,
                partner_id=partner_id,
            ),
            tone=StatusTone.positive,
        ),
        "debits": Kpi(
            label="Total Debits",
            value=StateValue.present(ledger_totals["debit_display"]),
            cohort_url=_ledger_cohort_url(
                entry_type=LedgerEntryType.debit.value,
                customer_ref=customer_ref,
                start_date=start_date,
                end_date=end_date,
                category=category,
                partner_id=partner_id,
            ),
            tone=StatusTone.warning,
        ),
        "net": Kpi(
            label="Net Balance",
            value=StateValue.present(ledger_totals["net_display"]),
            cohort_url=_ledger_cohort_url(
                entry_type=None,
                customer_ref=customer_ref,
                start_date=start_date,
                end_date=end_date,
                category=category,
                partner_id=partner_id,
            ),
            tone=StatusTone.info,
        ),
    }

    return {
        "entries": entries,
        "ledger_totals": ledger_totals,
        "ledger_kpis": ledger_kpis,
        "entry_type": entry_type,
        "customer_ref": customer_ref,
        "start_date": date_range.start_date.isoformat()
        if date_range.start_date
        else "",
        "end_date": date_range.end_date.isoformat() if date_range.end_date else "",
        "category": category,
        "selected_partner_id": selected_partner_id,
        "partner_options": partner_options,
    }


def _entry_customer_name(entry: LedgerEntry) -> str:
    account = getattr(entry, "account", None)
    if account is None:
        return ""
    return str(getattr(account, "name", "") or "").strip()


def render_ledger_csv(entries: list[LedgerEntry]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "entry_id",
            "customer_name",
            "entry_type",
            "source",
            "debit_amount",
            "credit_amount",
            "currency",
            "description",
            "date",
        ]
    )
    for entry in entries:
        entry_type = getattr(getattr(entry, "entry_type", None), "value", "") or ""
        amount = Decimal(str(getattr(entry, "amount", 0) or 0))
        # Prefer the real transaction date; created_at is the import instant for
        # migrated rows and would mislabel every one as 2026-03-15.
        entry_date = getattr(entry, "effective_date", None) or entry.created_at
        writer.writerow(
            [
                str(entry.id),
                _entry_customer_name(entry),
                entry_type,
                getattr(getattr(entry, "source", None), "value", "") or "",
                f"{amount:.2f}" if entry_type == "debit" else "",
                f"{amount:.2f}" if entry_type == "credit" else "",
                display_format.currency_code(entry.currency),
                entry.memo or "",
                entry_date.isoformat() if entry_date else "",
            ]
        )
    return buffer.getvalue()
