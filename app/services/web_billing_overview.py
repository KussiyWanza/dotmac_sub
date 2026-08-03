"""Service helpers for billing overview/invoice list/aging pages."""

from __future__ import annotations

import csv
import io
import logging
from collections.abc import Iterator
from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from threading import Lock
from time import monotonic
from uuid import UUID

from sqlalchemy import func, or_
from sqlalchemy.orm import Session, joinedload, selectinload

from app.models.billing import Invoice, InvoiceStatus, PaymentAllocation
from app.models.subscriber import Reseller, Subscriber, UserType
from app.services import display_format
from app.services import web_billing_customers as web_billing_customers_service
from app.services.common import validate_enum
from app.services.inclusive_date_range import InclusiveDateRange
from app.services.list_query import (
    ListDefinition,
    ListFieldDefinition,
    ListQuery,
    PageMeta,
    SortDirection,
)
from app.services.status_presentation import invoice_status_presentation

logger = logging.getLogger(__name__)


def _empty_invoice_total(currency: str) -> dict[str, object]:
    empty_display = display_format.format_currency_groups({}, empty_currency=currency)
    return {
        "count": 0,
        "amount": 0.0,
        "due_total": 0.0,
        "received_total": 0.0,
        "amounts": {},
        "due_amounts": {},
        "received_amounts": {},
        "display": empty_display,
        "due_display": empty_display,
        "received_display": empty_display,
    }


def _finalize_invoice_total(item: dict[str, object], *, currency: str) -> None:
    item["display"] = display_format.format_currency_groups(
        item["amounts"],  # type: ignore[arg-type]
        empty_currency=currency,
    )
    item["due_display"] = display_format.format_currency_groups(
        item["due_amounts"],  # type: ignore[arg-type]
        empty_currency=currency,
    )
    item["received_display"] = display_format.format_currency_groups(
        item["received_amounts"],  # type: ignore[arg-type]
        empty_currency=currency,
    )


_BUCKET_SEQUENCE = ("current", "1_30", "31_60", "61_90", "90_plus")
_BUCKET_LABELS = {
    "current": "Current",
    "1_30": "1-30 Days",
    "31_60": "31-60 Days",
    "61_90": "61-90 Days",
    "90_plus": "90+ Days",
}
_OVERVIEW_CACHE_TTL_SECONDS = 15.0
_overview_cache_lock = Lock()
_overview_cache: dict[
    tuple[str | None, str | None, str], tuple[float, dict[str, object]]
] = {}
_UNPAID_INVOICE_STATUSES = (
    InvoiceStatus.draft,
    InvoiceStatus.issued,
    InvoiceStatus.partially_paid,
    InvoiceStatus.overdue,
)
_INVOICE_STATUS_FILTERS = frozenset(
    {status.value for status in InvoiceStatus} | {"unpaid"}
)
INVOICE_LIST_DEFINITION = ListDefinition(
    key="billing_invoices",
    fields=(
        ListFieldDefinition(
            "invoice_number", "Invoice", searchable=True, sortable=True
        ),
        ListFieldDefinition("memo", "Memo", searchable=True),
        ListFieldDefinition("account_id", "Account", filterable=True),
        ListFieldDefinition("partner_id", "Partner", filterable=True),
        ListFieldDefinition("status", "Status", filterable=True, sortable=True),
        ListFieldDefinition("proforma_only", "Proforma", filterable=True),
        ListFieldDefinition("customer_ref", "Customer", filterable=True),
        ListFieldDefinition("start_date", "Start date", filterable=True),
        ListFieldDefinition("end_date", "End date", filterable=True),
        ListFieldDefinition("total", "Amount", sortable=True),
        ListFieldDefinition("issued_at", "Issued", sortable=True),
        ListFieldDefinition("due_at", "Due", sortable=True),
        ListFieldDefinition("created_at", "Created", sortable=True),
    ),
    default_sort="created_at",
    default_sort_dir="desc",
)


def _normalize_invoice_uuid_filter(value: str | None, name: str) -> str | None:
    normalized = str(value or "").strip()
    if not normalized:
        return None
    try:
        return str(UUID(normalized))
    except ValueError as exc:
        raise ValueError(f"{name} must be a valid UUID") from exc


def _normalize_invoice_per_page(per_page: int | str | None) -> int:
    try:
        normalized = int(str(per_page or "").strip())
    except ValueError:
        return INVOICE_LIST_DEFINITION.default_per_page
    if normalized in INVOICE_LIST_DEFINITION.per_page_options:
        return normalized
    return INVOICE_LIST_DEFINITION.default_per_page


def build_invoice_list_query(
    *,
    account_id: str | None,
    partner_id: str | None,
    status: str | None,
    proforma_only: bool,
    customer_ref: str | None,
    search: str | None,
    start_date: date | None,
    end_date: date | None,
    sort_by: str | None = None,
    sort_dir: SortDirection | str | None = None,
    page: int = 1,
    per_page: int | str | None = 25,
) -> ListQuery:
    """Normalize invoice list state through its declared capabilities."""

    normalized_status = str(status or "").strip().lower() or None
    if normalized_status and normalized_status not in _INVOICE_STATUS_FILTERS:
        raise ValueError(f"Unsupported status filter: {normalized_status}")
    created_range = InclusiveDateRange.from_dates(
        start_date=start_date,
        end_date=end_date,
    )

    return INVOICE_LIST_DEFINITION.build_query(
        search=search,
        filters={
            "account_id": _normalize_invoice_uuid_filter(account_id, "account_id"),
            "partner_id": _normalize_invoice_uuid_filter(partner_id, "partner_id"),
            "status": normalized_status,
            "proforma_only": "true" if proforma_only else None,
            "customer_ref": str(customer_ref or "").strip() or None,
            "start_date": created_range.start_value,
            "end_date": created_range.end_value,
        },
        sort_by=sort_by,
        sort_dir=sort_dir,
        page=page,
        per_page=_normalize_invoice_per_page(per_page),
    )


# Statuses that represent real accounts-receivable for the aging report (draft
# is excluded — not yet billed/owed). Aging must query THESE directly: loading
# all invoices ordered oldest-first and capping (the previous approach) hid all
# current debt behind years of historical/paid invoices, so every bucket read
# zero despite live arrears.
_AR_OPEN_STATUSES = (
    InvoiceStatus.issued,
    InvoiceStatus.partially_paid,
    InvoiceStatus.overdue,
)
# Safety bound on rows loaded for the report. Open AR (debtors only) is far
# smaller than the full invoice history; if this is ever hit we log so the cap
# isn't silent and we can move totals to a SQL aggregation.
_AR_AGING_MAX_INVOICES = 20000


def _add_months(value: date, months: int) -> date:
    year = value.year + ((value.month - 1 + months) // 12)
    month = ((value.month - 1 + months) % 12) + 1
    return date(year, month, 1)


def _month_end(value: date) -> date:
    next_month = _add_months(value, 1)
    return next_month - timedelta(days=1)


def _billing_location_options(db) -> list[str]:
    rows = (
        db.query(
            func.coalesce(
                Subscriber.region, Subscriber.billing_region, Subscriber.city
            ).label("location")
        )
        .filter(
            (Subscriber.user_type.is_(None))
            | (Subscriber.user_type != UserType.system_user)
        )
        .filter(
            func.coalesce(
                Subscriber.region, Subscriber.billing_region, Subscriber.city
            ).is_not(None)
        )
        .distinct()
        .all()
    )
    return sorted(
        {
            str(row.location).strip()
            for row in rows
            if getattr(row, "location", None) and str(row.location).strip()
        }
    )


def _normalized_overview_key(
    *,
    partner_id: str | None,
    location: str | None,
    period: str,
) -> tuple[str | None, str | None, str]:
    normalized_partner = (partner_id or "").strip() or None
    normalized_location = (location or "").strip().lower() or None
    normalized_period = (period or "").strip() or "this_month"
    return (normalized_partner, normalized_location, normalized_period)


def _get_cached_overview(
    key: tuple[str | None, str | None, str],
) -> dict[str, object] | None:
    now = monotonic()
    with _overview_cache_lock:
        cached = _overview_cache.get(key)
        if cached and (now - cached[0]) < _OVERVIEW_CACHE_TTL_SECONDS:
            return deepcopy(cached[1])
        if cached:
            _overview_cache.pop(key, None)
    return None


def _store_cached_overview(
    key: tuple[str | None, str | None, str],
    value: dict[str, object],
) -> None:
    with _overview_cache_lock:
        _overview_cache[key] = (monotonic(), deepcopy(value))


def build_overview_data(
    db,
    *,
    partner_id: str | None = None,
    location: str | None = None,
    period: str = "this_month",
) -> dict[str, object]:  # type: ignore[type-arg]
    """Build billing dashboard data via centralized reporting service."""
    from app.services.billing.reporting import billing_reporting

    cache_key = _normalized_overview_key(
        partner_id=partner_id,
        location=location,
        period=period,
    )
    result = _get_cached_overview(cache_key)
    if result is None:
        result = billing_reporting.get_dashboard_stats(
            db,
            partner_id=partner_id,
            location=location,
            period=period,
        )
        _store_cached_overview(cache_key, result)
    result["selected_partner_id"] = cache_key[0]
    result["selected_location"] = (location or "").strip() or None
    default_currency = display_format.default_currency(db)
    result["default_currency"] = default_currency
    stats = result.get("stats")
    if isinstance(stats, dict):
        stats["payments_amount_display"] = display_format.format_currency_amount(
            stats.get("payments_amount", 0), default_currency
        )
        stats["total_revenue_display"] = display_format.format_currency_amount(
            stats.get("total_revenue", 0), default_currency
        )
        stats["unpaid_invoices_amount_display"] = display_format.format_currency_amount(
            stats.get("unpaid_invoices_amount", 0), default_currency
        )

    from app.models.subscriber import Reseller

    # Partner options from Reseller table (efficient, not from invoice scan)
    partner_rows = (
        db.query(Reseller)
        .filter(Reseller.is_active.is_(True))
        .order_by(Reseller.name)
        .all()
    )
    result["partner_options"] = [
        {"id": str(r.id), "name": r.name} for r in partner_rows
    ]
    invoices = result.get("invoices")
    result["invoice_status_presentations"] = (
        {
            str(invoice.id): invoice_status_presentation(invoice.status)
            for invoice in invoices
        }
        if isinstance(invoices, list)
        else {}
    )
    return result


def _invoice_customer_account_ids(db, customer_ref: str | None) -> tuple[UUID, ...]:
    if not customer_ref:
        return ()
    return tuple(
        UUID(item["id"])
        for item in web_billing_customers_service.accounts_for_customer(
            db, customer_ref
        )
    )


def _apply_invoice_list_filters(
    query,
    *,
    list_query: ListQuery,
    customer_account_ids: tuple[UUID, ...],
    include_status: bool,
):  # type: ignore[no-untyped-def]
    scoped = query.filter(Invoice.is_active.is_(True))
    customer_ref = list_query.filter_value("customer_ref")
    account_id = list_query.filter_value("account_id")
    partner_id = list_query.filter_value("partner_id")
    status = list_query.filter_value("status")
    created_range = InclusiveDateRange.from_iso_values(
        start_date=list_query.filter_value("start_date"),
        end_date=list_query.filter_value("end_date"),
    )

    if customer_ref:
        if not customer_account_ids:
            return scoped.filter(Invoice.id.is_(None))
        scoped = scoped.filter(Invoice.account_id.in_(customer_account_ids))
    elif account_id:
        scoped = scoped.filter(Invoice.account_id == UUID(account_id))
    if partner_id:
        scoped = scoped.filter(
            Invoice.account.has(Subscriber.reseller_id == UUID(partner_id))
        )

    if include_status and status:
        if status == "unpaid":
            scoped = scoped.filter(Invoice.status.in_(_UNPAID_INVOICE_STATUSES))
        else:
            scoped = scoped.filter(
                Invoice.status == validate_enum(status, InvoiceStatus, "status")
            )
    if list_query.filter_value("proforma_only") == "true":
        scoped = scoped.filter(
            or_(
                Invoice.is_proforma.is_(True),
                Invoice.memo.ilike("%[PROFORMA]%"),
                Invoice.invoice_number.ilike("PF-%"),
            )
        )
    if list_query.search:
        term = f"%{list_query.search}%"
        scoped = scoped.filter(
            (Invoice.invoice_number.ilike(term)) | (Invoice.memo.ilike(term))
        )
    if created_range.start_at is not None:
        scoped = scoped.filter(Invoice.created_at >= created_range.start_at)
    if created_range.end_before is not None:
        scoped = scoped.filter(Invoice.created_at < created_range.end_before)
    return scoped


def _apply_invoice_list_sort(query, list_query: ListQuery):  # type: ignore[no-untyped-def]
    expressions = {
        "invoice_number": func.lower(func.coalesce(Invoice.invoice_number, "")),
        "status": Invoice.status,
        "total": Invoice.total,
        "issued_at": Invoice.issued_at,
        "due_at": Invoice.due_at,
        "created_at": Invoice.created_at,
    }
    expression = expressions[list_query.sort_by]
    ordered = expression.asc() if list_query.sort_dir == "asc" else expression.desc()
    return query.order_by(ordered, Invoice.id.asc())


def _invoice_status_summary(
    status_rows, *, default_currency: str
) -> dict[str, dict[str, object]]:
    summary: dict[str, dict[str, object]] = {
        key: _empty_invoice_total(default_currency)
        for key in ("draft", "issued", "partially_paid", "paid", "overdue", "void")
    }
    all_count = 0
    all_amounts: dict[str, Decimal] = {}
    all_due_amounts: dict[str, Decimal] = {}
    all_received_amounts: dict[str, Decimal] = {}
    for status_val, currency_value, count, amount, due in status_rows:
        key = (
            status_val.value
            if isinstance(status_val, InvoiceStatus)
            else str(status_val)
        )
        if key not in summary:
            summary[key] = _empty_invoice_total(default_currency)
        currency = display_format.currency_code(currency_value)
        amount_decimal = Decimal(str(amount or 0))
        due_decimal = Decimal(str(due or 0))
        received_decimal = max(amount_decimal - due_decimal, Decimal("0"))
        amounts = summary[key]["amounts"]
        due_amounts = summary[key]["due_amounts"]
        received_amounts = summary[key]["received_amounts"]
        assert isinstance(amounts, dict)
        assert isinstance(due_amounts, dict)
        assert isinstance(received_amounts, dict)
        amounts[currency] = amounts.get(currency, Decimal("0")) + amount_decimal
        due_amounts[currency] = due_amounts.get(currency, Decimal("0")) + due_decimal
        received_amounts[currency] = (
            received_amounts.get(currency, Decimal("0")) + received_decimal
        )
        summary[key]["count"] = int(summary[key]["count"]) + int(count or 0)
        summary[key]["amount"] = float(
            Decimal(str(summary[key]["amount"])) + amount_decimal
        )
        summary[key]["due_total"] = float(
            Decimal(str(summary[key]["due_total"])) + due_decimal
        )
        summary[key]["received_total"] = float(
            Decimal(str(summary[key]["received_total"])) + received_decimal
        )
        all_amounts[currency] = all_amounts.get(currency, Decimal("0")) + amount_decimal
        all_due_amounts[currency] = (
            all_due_amounts.get(currency, Decimal("0")) + due_decimal
        )
        all_received_amounts[currency] = (
            all_received_amounts.get(currency, Decimal("0")) + received_decimal
        )
        all_count += int(count or 0)
    for item in summary.values():
        _finalize_invoice_total(item, currency=default_currency)
    summary["all"] = {
        "count": all_count,
        "amount": sum(float(amount) for amount in all_amounts.values()),
        "due_total": sum(float(amount) for amount in all_due_amounts.values()),
        "received_total": sum(
            float(amount) for amount in all_received_amounts.values()
        ),
        "amounts": all_amounts,
        "due_amounts": all_due_amounts,
        "received_amounts": all_received_amounts,
        "display": display_format.format_currency_groups(
            all_amounts, empty_currency=default_currency
        ),
        "due_display": display_format.format_currency_groups(
            all_due_amounts, empty_currency=default_currency
        ),
        "received_display": display_format.format_currency_groups(
            all_received_amounts, empty_currency=default_currency
        ),
    }
    return summary


def list_invoices_for_scope(db, *, list_query: ListQuery) -> list[Invoice]:
    """Return the full canonical invoice scope for exports and reconciliation."""

    if list_query.definition.key != INVOICE_LIST_DEFINITION.key:
        raise ValueError("Invoice scope requires the billing invoice definition")
    customer_account_ids = _invoice_customer_account_ids(
        db, list_query.filter_value("customer_ref")
    )
    query = _apply_invoice_list_filters(
        db.query(Invoice),
        list_query=list_query,
        customer_account_ids=customer_account_ids,
        include_status=True,
    )
    return _apply_invoice_list_sort(query, list_query).all()


def build_invoices_list_data(
    db,
    *,
    list_query: ListQuery | None = None,
    account_id: str | None = None,
    partner_id: str | None = None,
    status: str | None = None,
    proforma_only: bool = False,
    customer_ref: str | None = None,
    search: str | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    sort_by: str | None = None,
    sort_dir: SortDirection | str | None = None,
    page: int = 1,
    per_page: int | str | None = 25,
) -> dict[str, object]:
    """Build the canonical invoice-list projection and compatibility context."""
    default_currency = display_format.default_currency(db)

    if list_query is None:
        list_query = build_invoice_list_query(
            account_id=account_id,
            partner_id=partner_id,
            status=status,
            proforma_only=proforma_only,
            customer_ref=customer_ref,
            search=search,
            start_date=start_date,
            end_date=end_date,
            sort_by=sort_by,
            sort_dir=sort_dir,
            page=page,
            per_page=per_page,
        )
    if list_query.definition.key != INVOICE_LIST_DEFINITION.key:
        raise ValueError("Invoice list requires the billing invoice definition")

    customer_account_ids = _invoice_customer_account_ids(
        db, list_query.filter_value("customer_ref")
    )
    filtered_query = _apply_invoice_list_filters(
        db.query(Invoice),
        list_query=list_query,
        customer_account_ids=customer_account_ids,
        include_status=True,
    )
    total = filtered_query.order_by(None).count()
    page_meta = PageMeta.from_query(list_query, total)
    effective_query = list_query.with_page(page_meta.page)
    invoices = (
        _apply_invoice_list_sort(filtered_query, effective_query)
        .offset(effective_query.offset)
        .limit(effective_query.per_page)
        .all()
    )

    status_rows = (
        _apply_invoice_list_filters(
            db.query(
                Invoice.status,
                Invoice.currency,
                func.count(Invoice.id),
                func.coalesce(func.sum(Invoice.total), 0),
                func.coalesce(func.sum(Invoice.balance_due), 0),
            ),
            list_query=effective_query,
            customer_account_ids=customer_account_ids,
            include_status=False,
        )
        .group_by(Invoice.status, Invoice.currency)
        .all()
    )
    status_totals = _invoice_status_summary(
        status_rows, default_currency=default_currency
    )
    proforma_count = (
        _apply_invoice_list_filters(
            db.query(func.count(Invoice.id)),
            list_query=effective_query,
            customer_account_ids=customer_account_ids,
            include_status=True,
        )
        .filter(Invoice.is_proforma.is_(True))
        .scalar()
        or 0
    )
    partner_options = [
        {"id": str(item.id), "name": item.name}
        for item in db.query(Reseller)
        .filter(Reseller.is_active.is_(True))
        .order_by(Reseller.name.asc())
        .all()
    ]
    return {
        "invoices": invoices,
        "invoice_status_presentations": {
            str(invoice.id): invoice_status_presentation(invoice.status)
            for invoice in invoices
        },
        "status_totals": status_totals,
        "list_query": effective_query,
        "page_meta": page_meta,
        "page": page_meta.page,
        "per_page": page_meta.per_page,
        "total": page_meta.total_items,
        "total_pages": page_meta.total_pages,
        "account_id": effective_query.filter_value("account_id"),
        "selected_partner_id": effective_query.filter_value("partner_id"),
        "partner_options": partner_options,
        "status": effective_query.filter_value("status"),
        "proforma_only": effective_query.filter_value("proforma_only") == "true",
        "proforma_summary": {"count": proforma_count},
        "customer_ref": effective_query.filter_value("customer_ref"),
        "search": effective_query.search,
        "start_date": effective_query.filter_value("start_date"),
        "end_date": effective_query.filter_value("end_date"),
    }


_INVOICE_CSV_HEADER = (
    "invoice_id",
    "invoice_number",
    "customer_name",
    "status",
    "total",
    "balance_due",
    "payment_received",
    "currency",
    "issued_at",
    "due_at",
    "created_at",
    "memo",
)

# Rows are streamed from a server-side cursor in batches of this size, so an
# uncapped export stays bounded to one batch in memory rather than loading the
# whole result set (and then the whole CSV string) at once.
_INVOICE_CSV_YIELD_PER = 1000


def _invoice_csv_row(invoice: Invoice) -> list[str]:
    total = Decimal(str(getattr(invoice, "total", 0) or 0))
    due = Decimal(str(getattr(invoice, "balance_due", 0) or 0))
    received = total - due
    raw_status = getattr(invoice, "status", None)
    status_value = (
        raw_status.value if hasattr(raw_status, "value") else str(raw_status or "")
    )
    return [
        str(invoice.id),
        invoice.invoice_number or "",
        invoice.account.name if invoice.account else "",
        status_value,
        f"{total:.2f}",
        f"{due:.2f}",
        f"{received:.2f}",
        display_format.currency_code(invoice.currency),
        invoice.issued_at.isoformat() if invoice.issued_at else "",
        invoice.due_at.isoformat() if invoice.due_at else "",
        invoice.created_at.isoformat() if invoice.created_at else "",
        invoice.memo or "",
    ]


def render_invoices_csv(invoices: list[Invoice]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(_INVOICE_CSV_HEADER)
    for invoice in invoices:
        writer.writerow(_invoice_csv_row(invoice))
    return buffer.getvalue()


def stream_invoices_csv(db: Session, *, list_query: ListQuery) -> Iterator[str]:
    """Yield the canonical invoice-scope CSV one row at a time.

    Same scope and column contract as ``render_invoices_csv``, but iterated from
    a server-side cursor so the export never materializes the full result set or
    the full CSV body in memory. The scalar customer relationship is joined in
    the same query so customer names do not introduce one query per invoice;
    ``yield_per`` still bounds result iteration to one batch in memory.
    """
    if list_query.definition.key != INVOICE_LIST_DEFINITION.key:
        raise ValueError("Invoice scope requires the billing invoice definition")
    customer_account_ids = _invoice_customer_account_ids(
        db, list_query.filter_value("customer_ref")
    )
    query = _apply_invoice_list_filters(
        db.query(Invoice),
        list_query=list_query,
        customer_account_ids=customer_account_ids,
        include_status=True,
    )
    query = (
        _apply_invoice_list_sort(query, list_query)
        .options(joinedload(Invoice.account))
        .yield_per(_INVOICE_CSV_YIELD_PER)
    )

    buffer = io.StringIO()
    writer = csv.writer(buffer)

    def _emit(values) -> str:
        buffer.seek(0)
        buffer.truncate(0)
        writer.writerow(values)
        return buffer.getvalue()

    yield _emit(_INVOICE_CSV_HEADER)
    for invoice in query:
        yield _emit(_invoice_csv_row(invoice))


def _account_label(invoice: Invoice) -> str:
    account = getattr(invoice, "account", None)
    if not account:
        return "Account"

    display_name = getattr(account, "display_name", None)
    if display_name:
        return str(display_name)

    first_name = (getattr(account, "first_name", "") or "").strip()
    last_name = (getattr(account, "last_name", "") or "").strip()
    full_name = " ".join(part for part in (first_name, last_name) if part)
    if full_name:
        return full_name

    email = getattr(account, "email", None)
    if email:
        return str(email)

    account_number = getattr(account, "account_number", None)
    if account_number:
        return str(account_number)
    return "Account"


def _last_payment_date(invoice: Invoice) -> date | None:
    last_seen: datetime | None = None
    for allocation in getattr(invoice, "payment_allocations", []) or []:
        payment = getattr(allocation, "payment", None)
        if payment:
            candidate = getattr(payment, "paid_at", None) or getattr(
                allocation, "created_at", None
            )
        else:
            candidate = getattr(allocation, "created_at", None)
        if candidate and (last_seen is None or candidate > last_seen):
            last_seen = candidate
    return last_seen.date() if last_seen else None


def _classify_aging_bucket(invoice: Invoice, *, today: date) -> str:
    due_at = invoice.due_at.date() if invoice.due_at else None
    if not due_at or due_at >= today:
        return "current"

    days = (today - due_at).days
    if days <= 30:
        return "1_30"
    if days <= 60:
        return "31_60"
    if days <= 90:
        return "61_90"
    return "90_plus"


def _in_period(invoice: Invoice, *, period: str, today: date) -> bool:
    if period == "all":
        return True
    if period == "this_year":
        due_at = invoice.due_at.date() if invoice.due_at else None
        return bool(due_at and due_at.year == today.year)
    return False


def build_ar_aging_data(
    db,
    *,
    period: str = "all",
    bucket: str | None = None,
    partner_id: str | None = None,
    location: str | None = None,
    debtor_period: str | None = None,
) -> dict[str, object]:
    default_currency = display_format.default_currency(db)
    selected_period = period if period in {"all", "this_year"} else "all"
    selected_bucket = bucket if bucket in _BUCKET_SEQUENCE else None
    selected_partner_id = (partner_id or "").strip() or None
    selected_location = (location or "").strip() or None
    selected_debtor_period = (
        debtor_period if debtor_period in {"all", "this_month", "last_month"} else "all"
    )

    # Query open AR directly (issued/partially_paid/overdue with a balance), so
    # current debt is never hidden behind paid history. Oldest-due first means a
    # cap (if ever hit) keeps the most-overdue rows, which matter most here.
    invoices = (
        db.query(Invoice)
        .options(
            selectinload(Invoice.payment_allocations).selectinload(
                PaymentAllocation.payment
            )
        )
        .filter(Invoice.is_active.is_(True))
        .filter(Invoice.status.in_(_AR_OPEN_STATUSES))
        .filter(Invoice.balance_due > 0)
        .order_by(Invoice.due_at.asc())
        .limit(_AR_AGING_MAX_INVOICES)
        .all()
    )
    if len(invoices) >= _AR_AGING_MAX_INVOICES:
        logger.warning(
            "AR aging hit the %s-invoice cap; totals may understate open AR — "
            "move bucket totals to a SQL aggregation.",
            _AR_AGING_MAX_INVOICES,
        )

    today = datetime.now(UTC).date()
    period_filtered_invoices = [
        invoice
        for invoice in invoices
        if invoice.status
        not in {InvoiceStatus.paid, InvoiceStatus.void, InvoiceStatus.written_off}
        and _in_period(invoice, period=selected_period, today=today)
    ]
    account_ids = {
        invoice.account_id
        for invoice in period_filtered_invoices
        if getattr(invoice, "account_id", None)
    }
    accounts_by_id = {
        account.id: account
        for account in (
            db.query(Subscriber).filter(Subscriber.id.in_(account_ids)).all()
            if account_ids
            else []
        )
    }

    from app.models.subscriber import Reseller as ResellerModel

    partner_rows = (
        db.query(ResellerModel)
        .filter(ResellerModel.is_active.is_(True))
        .order_by(ResellerModel.name)
        .all()
    )
    partner_options = {str(r.id): r.name for r in partner_rows}
    location_options = set(_billing_location_options(db))

    filtered_invoices = []
    for invoice in period_filtered_invoices:
        account = getattr(invoice, "account", None) or accounts_by_id.get(
            invoice.account_id
        )
        if selected_partner_id:
            account_partner = str(getattr(account, "reseller_id", "") or "")
            if account_partner != selected_partner_id:
                continue
        if selected_location:
            account_location = (
                str(getattr(account, "region", "") or "")
                or str(getattr(account, "billing_region", "") or "")
                or str(getattr(account, "city", "") or "")
            )
            if account_location.lower() != selected_location.lower():
                continue
        filtered_invoices.append(invoice)

    buckets: dict[str, list[Invoice]] = {key: [] for key in _BUCKET_SEQUENCE}
    for invoice in filtered_invoices:
        bucket_key = _classify_aging_bucket(invoice, today=today)
        buckets[bucket_key].append(invoice)

    totals = {
        key: sum(float(getattr(inv, "balance_due", 0) or 0) for inv in items)
        for key, items in buckets.items()
    }
    totals_by_currency: dict[str, dict[str, Decimal]] = {}
    for key, items in buckets.items():
        amounts: dict[str, Decimal] = {}
        for invoice in items:
            currency = display_format.currency_code(getattr(invoice, "currency", None))
            amount = Decimal(str(getattr(invoice, "balance_due", 0) or 0))
            amounts[currency] = amounts.get(currency, Decimal("0")) + amount
        totals_by_currency[key] = amounts
    counts = {key: len(items) for key, items in buckets.items()}

    bucket_rows: dict[str, list[dict[str, object]]] = {}
    bucket_invoice_ids: dict[str, str] = {}
    for key, items in buckets.items():
        bucket_rows[key] = [
            {
                "invoice": invoice,
                "account_label": _account_label(invoice),
                "last_payment_at": _last_payment_date(invoice),
            }
            for invoice in items
        ]
        bucket_invoice_ids[key] = ",".join(str(invoice.id) for invoice in items)

    overdue_invoices = [
        invoice
        for key in ("1_30", "31_60", "61_90", "90_plus")
        for invoice in buckets[key]
    ]
    this_month_start = date(today.year, today.month, 1)
    this_month_end = _month_end(this_month_start)
    previous_month_end = this_month_start - timedelta(days=1)
    last_month_start = date(previous_month_end.year, previous_month_end.month, 1)

    def _debtor_period_match(invoice: Invoice) -> bool:
        if selected_debtor_period == "all":
            return True
        due_at = invoice.due_at.date() if invoice.due_at else None
        if not due_at:
            return False
        if selected_debtor_period == "this_month":
            return this_month_start <= due_at <= this_month_end
        if selected_debtor_period == "last_month":
            return last_month_start <= due_at <= previous_month_end
        return True

    debtor_source_invoices = (
        filtered_invoices if selected_debtor_period != "all" else overdue_invoices
    )

    debtor_totals: dict[UUID, float] = {}
    debtor_amounts: dict[UUID, dict[str, Decimal]] = {}
    debtor_names: dict[UUID, str] = {}
    for invoice in debtor_source_invoices:
        if not _debtor_period_match(invoice):
            continue
        account_id = invoice.account_id
        amount = Decimal(str(invoice.balance_due or 0))
        currency = display_format.currency_code(getattr(invoice, "currency", None))
        debtor_totals[account_id] = debtor_totals.get(account_id, 0.0) + float(amount)
        account_amounts = debtor_amounts.setdefault(account_id, {})
        account_amounts[currency] = account_amounts.get(currency, Decimal("0")) + amount
        debtor_names.setdefault(account_id, _account_label(invoice))

    top_debtors = [
        {
            "account_id": str(account_id),
            "account_label": debtor_names.get(account_id, "Account"),
            "amount": amount,
            "amounts": debtor_amounts.get(account_id, {}),
            "display": display_format.format_currency_groups(
                debtor_amounts.get(account_id, {}),
                empty_currency=default_currency,
            ),
        }
        for account_id, amount in sorted(
            debtor_totals.items(), key=lambda item: item[1], reverse=True
        )[:10]
    ]

    bucket_order = [
        {
            "key": key,
            "label": _BUCKET_LABELS[key],
            "amount": totals[key],
            "amounts": totals_by_currency[key],
            "display": display_format.format_currency_groups(
                totals_by_currency[key],
                empty_currency=default_currency,
            ),
            "count": counts[key],
            "is_selected": selected_bucket == key,
        }
        for key in _BUCKET_SEQUENCE
    ]

    visible_keys = [selected_bucket] if selected_bucket else list(_BUCKET_SEQUENCE)

    # Aging trend over last 6 months (snapshot at month-end).
    current_month = date(today.year, today.month, 1)
    trend_months = [_add_months(current_month, -5 + idx) for idx in range(6)]
    trend_labels = [month.strftime("%b %Y") for month in trend_months]
    trend_series: dict[str, list[float]] = {key: [] for key in _BUCKET_SEQUENCE}
    for month_start in trend_months:
        snapshot_at = min(_month_end(month_start), today)
        snapshot_buckets: dict[str, float] = dict.fromkeys(_BUCKET_SEQUENCE, 0.0)
        for invoice in filtered_invoices:
            due_at = invoice.due_at.date() if invoice.due_at else None
            if not due_at:
                snapshot_buckets["current"] += float(invoice.balance_due or 0)
                continue
            if due_at >= snapshot_at:
                snapshot_buckets["current"] += float(invoice.balance_due or 0)
                continue
            days = (snapshot_at - due_at).days
            if days <= 30:
                snapshot_buckets["1_30"] += float(invoice.balance_due or 0)
            elif days <= 60:
                snapshot_buckets["31_60"] += float(invoice.balance_due or 0)
            elif days <= 90:
                snapshot_buckets["61_90"] += float(invoice.balance_due or 0)
            else:
                snapshot_buckets["90_plus"] += float(invoice.balance_due or 0)
        for key in _BUCKET_SEQUENCE:
            trend_series[key].append(snapshot_buckets[key])

    return {
        "buckets": buckets,
        "totals": totals,
        "totals_by_currency": totals_by_currency,
        "counts": counts,
        "bucket_rows": bucket_rows,
        "bucket_invoice_ids": bucket_invoice_ids,
        "bucket_order": bucket_order,
        "visible_bucket_keys": visible_keys,
        "selected_bucket": selected_bucket,
        "selected_period": selected_period,
        "selected_partner_id": selected_partner_id,
        "selected_location": selected_location,
        "selected_debtor_period": selected_debtor_period,
        "partner_options": [
            {"id": partner_key, "name": partner_name}
            for partner_key, partner_name in sorted(
                partner_options.items(), key=lambda item: item[1].lower()
            )
        ],
        "location_options": sorted(location_options),
        "top_debtors": top_debtors,
        "aging_trend": {
            "labels": trend_labels,
            "series": trend_series,
        },
    }
