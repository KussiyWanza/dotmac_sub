"""Typed UI projection for the administrative document-discount report."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.billing import (
    InvoiceDiscountAction,
    InvoiceDiscountSource,
    InvoiceDiscountType,
    InvoiceStatus,
)
from app.models.sales import QuoteDiscountAction, QuoteDiscountType, QuoteStatus
from app.schemas.status_presentation import (
    StatusIcon,
    StatusPresentation,
    StatusTone,
)
from app.services import display_format, invoice_discounts, status_presentation
from app.services.list_query import (
    ListDefinition,
    ListFieldDefinition,
    ListQuery,
    PageMeta,
)
from app.services.sales import quote_discount_reporting


class DiscountReportTab(StrEnum):
    invoices = "invoices"
    quotes = "quotes"


class DocumentDiscountType(StrEnum):
    percentage = "percentage"
    fixed_amount = "fixed_amount"


@dataclass(frozen=True, slots=True)
class DocumentDiscountReportQuery:
    tab: DiscountReportTab = DiscountReportTab.invoices
    date_from: date | None = None
    date_to: date | None = None
    customer: str | None = None
    salesperson_id: UUID | None = None
    discount_type: DocumentDiscountType | None = None
    invoice_status: InvoiceStatus | None = None
    quote_status: QuoteStatus | None = None
    source: InvoiceDiscountSource | None = None
    page: int = 1
    page_size: int = 25


@dataclass(frozen=True, slots=True)
class DiscountReportOption:
    value: str
    label: str


@dataclass(frozen=True, slots=True)
class DiscountReportRow:
    document_label: str
    document_url: str
    customer_name: str
    original_subtotal_display: str
    discount_value_display: str
    discount_amount_display: str
    discounted_subtotal_display: str
    total_after_discount_display: str
    reason_display: str
    actor_name: str
    applied_at_display: str
    revision: int
    action: StatusPresentation
    document_status: StatusPresentation
    source_label: str
    source_quote_url: str | None


@dataclass(frozen=True, slots=True)
class DocumentDiscountReport:
    query: DocumentDiscountReportQuery
    rows: tuple[DiscountReportRow, ...]
    total_count: int
    list_query: ListQuery
    page_meta: PageMeta
    actor_options: tuple[DiscountReportOption, ...]
    discount_type_options: tuple[DiscountReportOption, ...]
    status_options: tuple[DiscountReportOption, ...]
    source_options: tuple[DiscountReportOption, ...]


DOCUMENT_DISCOUNT_LIST = ListDefinition(
    key="document_discounts",
    fields=(
        ListFieldDefinition("customer", "Customer", searchable=True),
        ListFieldDefinition("tab", "Document type", filterable=True),
        ListFieldDefinition("date_from", "From date", filterable=True),
        ListFieldDefinition("date_to", "To date", filterable=True),
        ListFieldDefinition("salesperson_id", "Salesperson", filterable=True),
        ListFieldDefinition("discount_type", "Discount type", filterable=True),
        ListFieldDefinition("invoice_status", "Invoice status", filterable=True),
        ListFieldDefinition("quote_status", "Quote status", filterable=True),
        ListFieldDefinition("source", "Invoice discount source", filterable=True),
        ListFieldDefinition("applied_at", "Applied", sortable=True),
    ),
    default_sort="applied_at",
    default_sort_dir="desc",
)


_ACTION_PRESENTATIONS: dict[str, tuple[str, StatusTone, StatusIcon]] = {
    "applied": ("Applied", StatusTone.positive, StatusIcon.check),
    "changed": ("Changed", StatusTone.warning, StatusIcon.clock),
    "removed": ("Removed", StatusTone.neutral, StatusIcon.minus),
    "inherited": ("Inherited", StatusTone.info, StatusIcon.info),
}


def _action_presentation(
    action: InvoiceDiscountAction | QuoteDiscountAction,
) -> StatusPresentation:
    label, tone, icon = _ACTION_PRESENTATIONS[action.value]
    return StatusPresentation(value=action.value, label=label, tone=tone, icon=icon)


def _discount_value_display(
    *,
    discount_type: InvoiceDiscountType | QuoteDiscountType,
    value: object,
    currency: str,
) -> str:
    if discount_type.value == DocumentDiscountType.percentage.value:
        return f"{value}%"
    return display_format.format_currency_amount(value, currency)


def _list_query(query: DocumentDiscountReportQuery) -> ListQuery:
    return DOCUMENT_DISCOUNT_LIST.build_query(
        search=query.customer,
        filters={
            "tab": query.tab.value,
            "date_from": query.date_from.isoformat() if query.date_from else None,
            "date_to": query.date_to.isoformat() if query.date_to else None,
            "salesperson_id": query.salesperson_id,
            "discount_type": query.discount_type.value if query.discount_type else None,
            "invoice_status": (
                query.invoice_status.value
                if query.tab is DiscountReportTab.invoices and query.invoice_status
                else None
            ),
            "quote_status": (
                query.quote_status.value
                if query.tab is DiscountReportTab.quotes and query.quote_status
                else None
            ),
            "source": (
                query.source.value
                if query.tab is DiscountReportTab.invoices and query.source
                else None
            ),
        },
        page=query.page,
        per_page=query.page_size,
    )


def _invoice_report(
    db: Session,
    query: DocumentDiscountReportQuery,
    list_query: ListQuery,
) -> DocumentDiscountReport:
    result = invoice_discounts.list_invoice_discount_history(
        db,
        invoice_discounts.InvoiceDiscountHistoryQuery(
            date_from=query.date_from,
            date_to=query.date_to,
            customer=query.customer,
            salesperson_id=query.salesperson_id,
            discount_type=(
                InvoiceDiscountType(query.discount_type.value)
                if query.discount_type
                else None
            ),
            invoice_status=query.invoice_status,
            source=query.source,
            page=query.page,
            page_size=query.page_size,
        ),
    )
    rows = tuple(
        DiscountReportRow(
            document_label=item.invoice_number or f"Invoice {str(item.invoice_id)[:8]}",
            document_url=f"/admin/billing/invoices/{item.invoice_id}",
            customer_name=item.customer_name,
            original_subtotal_display=display_format.format_currency_amount(
                item.original_subtotal, item.currency
            ),
            discount_value_display=_discount_value_display(
                discount_type=item.discount_type,
                value=item.discount_value,
                currency=item.currency,
            ),
            discount_amount_display=display_format.format_currency_amount(
                item.discount_amount, item.currency
            ),
            discounted_subtotal_display=display_format.format_currency_amount(
                item.discounted_subtotal, item.currency
            ),
            total_after_discount_display=display_format.format_currency_amount(
                item.total_after_discount, item.currency
            ),
            reason_display=item.reason or display_format.MISSING_DISPLAY,
            actor_name=item.actor_name,
            applied_at_display=display_format.format_timestamp(item.applied_at, db),
            revision=item.revision,
            action=_action_presentation(item.action),
            document_status=status_presentation.invoice_status_presentation(
                item.invoice_status
            ),
            source_label=(
                "Inherited from quote"
                if item.source is InvoiceDiscountSource.quote
                else "Manual invoice discount"
            ),
            source_quote_url=(
                f"/admin/sales/quotes/{item.source_quote_id}"
                if item.source_quote_id
                else None
            ),
        )
        for item in result.items
    )
    actors = invoice_discounts.invoice_discount_actor_options(db)
    return DocumentDiscountReport(
        query=query,
        rows=rows,
        total_count=result.total_count,
        list_query=list_query,
        page_meta=PageMeta.from_query(list_query, result.total_count),
        actor_options=tuple(
            DiscountReportOption(value=str(actor.system_user_id), label=actor.label)
            for actor in actors
        ),
        discount_type_options=_discount_type_options(),
        status_options=tuple(
            DiscountReportOption(
                value=status.value,
                label=status_presentation.invoice_status_presentation(status).label,
            )
            for status in InvoiceStatus
        ),
        source_options=(
            DiscountReportOption(value="manual", label="Manual invoice discount"),
            DiscountReportOption(value="quote", label="Inherited from quote"),
        ),
    )


def _quote_report(
    db: Session,
    query: DocumentDiscountReportQuery,
    list_query: ListQuery,
) -> DocumentDiscountReport:
    result = quote_discount_reporting.list_quote_discount_history(
        db,
        quote_discount_reporting.QuoteDiscountHistoryQuery(
            date_from=query.date_from,
            date_to=query.date_to,
            customer=query.customer,
            salesperson_id=query.salesperson_id,
            discount_type=(
                QuoteDiscountType(query.discount_type.value)
                if query.discount_type
                else None
            ),
            quote_status=query.quote_status,
            page=query.page,
            page_size=query.page_size,
        ),
    )
    rows = tuple(
        DiscountReportRow(
            document_label=f"Quote {str(item.quote_id)[:8]}",
            document_url=f"/admin/sales/quotes/{item.quote_id}",
            customer_name=item.customer_name,
            original_subtotal_display=display_format.format_currency_amount(
                item.original_subtotal, item.currency
            ),
            discount_value_display=_discount_value_display(
                discount_type=item.discount_type,
                value=item.discount_value,
                currency=item.currency,
            ),
            discount_amount_display=display_format.format_currency_amount(
                item.discount_amount, item.currency
            ),
            discounted_subtotal_display=display_format.format_currency_amount(
                item.discounted_subtotal, item.currency
            ),
            total_after_discount_display=display_format.format_currency_amount(
                item.total_after_discount, item.currency
            ),
            reason_display=item.reason or display_format.MISSING_DISPLAY,
            actor_name=item.actor_name,
            applied_at_display=display_format.format_timestamp(item.applied_at, db),
            revision=item.revision,
            action=_action_presentation(item.action),
            document_status=status_presentation.quote_status_presentation(
                item.quote_status
            ),
            source_label="Quote discount",
            source_quote_url=None,
        )
        for item in result.items
    )
    actors = quote_discount_reporting.quote_discount_actor_options(db)
    return DocumentDiscountReport(
        query=query,
        rows=rows,
        total_count=result.total_count,
        list_query=list_query,
        page_meta=PageMeta.from_query(list_query, result.total_count),
        actor_options=tuple(
            DiscountReportOption(value=str(actor.system_user_id), label=actor.label)
            for actor in actors
        ),
        discount_type_options=_discount_type_options(),
        status_options=tuple(
            DiscountReportOption(
                value=status.value,
                label=status_presentation.quote_status_presentation(status).label,
            )
            for status in QuoteStatus
        ),
        source_options=(),
    )


def _discount_type_options() -> tuple[DiscountReportOption, ...]:
    return (
        DiscountReportOption(value="percentage", label="Percentage"),
        DiscountReportOption(value="fixed_amount", label="Fixed amount"),
    )


def build_document_discount_report(
    db: Session,
    query: DocumentDiscountReportQuery,
) -> DocumentDiscountReport:
    """Compose one read-only report tab from the canonical history owners."""

    list_query = _list_query(query)
    if query.tab is DiscountReportTab.invoices:
        return _invoice_report(db, query, list_query)
    return _quote_report(db, query, list_query)
