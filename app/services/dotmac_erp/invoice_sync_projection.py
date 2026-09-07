"""Read-only accounting projection for Dotmac ERP invoice synchronization.

The projection exposes the billing owner's stored header and line tax facts and
classifies contradictions without mutating or silently repairing an Invoice.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from sqlalchemy.orm import Session, selectinload

from app.models.billing import (
    Invoice,
    InvoiceDiscountType,
    InvoiceLine,
    InvoiceStatus,
    TaxApplication,
)
from app.schemas.billing import (
    InvoiceAccountingSyncDisposition,
    InvoiceAccountingSyncIssueCode,
    InvoiceAccountingSyncIssueRead,
    InvoiceAccountingSyncLineRead,
    InvoiceAccountingSyncRead,
    InvoiceAccountingSyncSourceKind,
)
from app.schemas.common import ListResponse
from app.services.billing._common import _calculate_tax_amount
from app.services.common import round_money, to_decimal
from app.services.sync_feeds import apply_sync_page

ACCOUNTING_SYNC_CONTRACT_VERSION: Literal["invoice-accounting-sync.v2"] = (
    "invoice-accounting-sync.v2"
)


@dataclass(frozen=True)
class InvoiceAccountingSyncQuery:
    """Typed filters for one deterministic ERP accounting-sync page."""

    invoice_id: UUID | None
    account_id: UUID | None
    status: InvoiceStatus | None
    is_active: bool | None
    updated_since: datetime | None
    limit: int
    offset: int


def _issue(
    code: InvoiceAccountingSyncIssueCode,
    *,
    line_id: UUID | None = None,
    expected_amount: Decimal | None = None,
    actual_amount: Decimal | None = None,
) -> InvoiceAccountingSyncIssueRead:
    return InvoiceAccountingSyncIssueRead(
        code=code,
        line_id=line_id,
        expected_amount=expected_amount,
        actual_amount=actual_amount,
    )


def _project_line(
    line: InvoiceLine,
) -> tuple[InvoiceAccountingSyncLineRead, list[InvoiceAccountingSyncIssueRead]]:
    issues: list[InvoiceAccountingSyncIssueRead] = []
    amount = round_money(to_decimal(line.amount))
    expected_amount = round_money(
        to_decimal(line.quantity) * to_decimal(line.unit_price)
    )
    if amount != expected_amount:
        issues.append(
            _issue(
                InvoiceAccountingSyncIssueCode.LINE_AMOUNT_MISMATCH,
                line_id=line.id,
                expected_amount=expected_amount,
                actual_amount=amount,
            )
        )

    has_tax_snapshot = (
        line.tax_rate_snapshot_version == 1
        and line.tax_rate_percent_snapshot is not None
        and line.tax_rate_is_active_snapshot is not None
    )
    if line.tax_rate_id is not None and not has_tax_snapshot:
        issues.append(
            _issue(
                InvoiceAccountingSyncIssueCode.TAX_SNAPSHOT_MISSING,
                line_id=line.id,
            )
        )

    rate_percent = (
        to_decimal(line.tax_rate_percent_snapshot)
        if has_tax_snapshot
        else Decimal("0.00")
    )
    tax_amount = Decimal("0.00")
    if has_tax_snapshot:
        tax_amount = _calculate_tax_amount(
            amount,
            rate_percent,
            line.tax_application,
        )
    net_amount = (
        round_money(amount - tax_amount)
        if line.tax_application == TaxApplication.inclusive
        else amount
    )
    gross_amount = round_money(net_amount + tax_amount)

    return (
        InvoiceAccountingSyncLineRead(
            id=line.id,
            description=line.description,
            quantity=line.quantity,
            unit_price=line.unit_price,
            source_amount=amount,
            net_amount_before_discount=net_amount,
            tax_amount_before_discount=tax_amount,
            gross_amount_before_discount=gross_amount,
            tax_rate_id=line.tax_rate_id,
            tax_rate_code=(line.tax_rate_code_snapshot if has_tax_snapshot else None),
            tax_rate_percent=(
                line.tax_rate_percent_snapshot if has_tax_snapshot else None
            ),
            tax_rate_is_active=(
                line.tax_rate_is_active_snapshot if has_tax_snapshot else None
            ),
            tax_application=line.tax_application,
        ),
        issues,
    )


def project_invoice_for_accounting(invoice: Invoice) -> InvoiceAccountingSyncRead:
    """Resolve one Invoice into an exact, fail-closed accounting projection."""

    issues: list[InvoiceAccountingSyncIssueRead] = []
    line_projections: list[InvoiceAccountingSyncLineRead] = []
    for line in sorted(
        (item for item in invoice.lines if item.is_active),
        key=lambda item: str(item.id),
    ):
        projected, line_issues = _project_line(line)
        line_projections.append(projected)
        issues.extend(line_issues)

    subtotal = round_money(to_decimal(invoice.subtotal))
    discount_amount = round_money(to_decimal(invoice.discount_amount))
    discounted_subtotal = max(Decimal("0.00"), subtotal - discount_amount)
    tax_total = round_money(to_decimal(invoice.tax_total))
    total = round_money(to_decimal(invoice.total))
    projected_subtotal = round_money(
        sum(
            (line.net_amount_before_discount for line in line_projections),
            Decimal("0.00"),
        )
    )
    projected_tax = round_money(
        sum(
            (line.tax_amount_before_discount for line in line_projections),
            Decimal("0.00"),
        )
    )
    projected_tax_after_discount = projected_tax
    if discount_amount > Decimal("0.00") and projected_subtotal > Decimal("0.00"):
        projected_discounted_subtotal = max(
            Decimal("0.00"), projected_subtotal - discount_amount
        )
        projected_tax_after_discount = round_money(
            projected_tax * projected_discounted_subtotal / projected_subtotal
        )

    if not line_projections:
        issues.append(_issue(InvoiceAccountingSyncIssueCode.NO_ACTIVE_LINES))

    legacy_totals_missing = (
        invoice.splynx_invoice_id is not None
        and subtotal == Decimal("0.00")
        and tax_total == Decimal("0.00")
        and total != Decimal("0.00")
        and projected_subtotal == total
    )
    if legacy_totals_missing:
        issues.append(
            _issue(
                InvoiceAccountingSyncIssueCode.LEGACY_HEADER_TOTALS_MISSING,
                expected_amount=projected_subtotal,
                actual_amount=subtotal,
            )
        )
    elif projected_subtotal != subtotal:
        issues.append(
            _issue(
                InvoiceAccountingSyncIssueCode.HEADER_SUBTOTAL_MISMATCH,
                expected_amount=projected_subtotal,
                actual_amount=subtotal,
            )
        )

    if projected_tax_after_discount != tax_total:
        code = InvoiceAccountingSyncIssueCode.HEADER_TAX_MISMATCH
        if tax_total > Decimal("0.00") and projected_tax_after_discount == Decimal(
            "0.00"
        ):
            code = InvoiceAccountingSyncIssueCode.TAXED_HEADER_WITHOUT_LINE_TAX
        issues.append(
            _issue(
                code,
                expected_amount=projected_tax_after_discount,
                actual_amount=tax_total,
            )
        )

    expected_total = round_money(discounted_subtotal + tax_total)
    if expected_total != total:
        issues.append(
            _issue(
                InvoiceAccountingSyncIssueCode.HEADER_TOTAL_MISMATCH,
                expected_amount=expected_total,
                actual_amount=total,
            )
        )

    if discount_amount > Decimal("0.00"):
        # The owner stores an Invoice-level discount. A line/group apportionment
        # rule has not been approved, so the accounting projection refuses to
        # invent one. Header facts remain visible for diagnosis and shadowing.
        issues.append(
            _issue(
                InvoiceAccountingSyncIssueCode.DISCOUNT_ALLOCATION_UNDEFINED,
                actual_amount=discount_amount,
            )
        )

    if invoice.is_proforma or invoice.status == InvoiceStatus.draft:
        disposition = InvoiceAccountingSyncDisposition.NOT_APPLICABLE
    elif issues:
        disposition = InvoiceAccountingSyncDisposition.BLOCKED
    else:
        disposition = InvoiceAccountingSyncDisposition.READY

    return InvoiceAccountingSyncRead(
        contract_version=ACCOUNTING_SYNC_CONTRACT_VERSION,
        source_kind=(
            InvoiceAccountingSyncSourceKind.SPLYNX_LEGACY
            if invoice.splynx_invoice_id is not None
            else InvoiceAccountingSyncSourceKind.NATIVE
        ),
        source_invoice_id=invoice.id,
        source_splynx_invoice_id=invoice.splynx_invoice_id,
        account_id=invoice.account_id,
        account=invoice.account,
        invoice_number=invoice.invoice_number,
        status=invoice.status,
        currency=invoice.currency,
        subtotal_before_discount=subtotal,
        discount_type=(
            InvoiceDiscountType(invoice.discount_type)
            if invoice.discount_type is not None
            else None
        ),
        discount_value=invoice.discount_value,
        discount_amount=discount_amount,
        discounted_subtotal=discounted_subtotal,
        tax_total=tax_total,
        total=total,
        balance_due=round_money(to_decimal(invoice.balance_due)),
        issued_at=invoice.issued_at,
        due_at=invoice.due_at,
        paid_at=invoice.paid_at,
        memo=invoice.memo,
        is_proforma=invoice.is_proforma,
        updated_at=invoice.updated_at,
        disposition=disposition,
        issues=issues,
        lines=line_projections,
    )


def list_invoice_accounting_sync(
    db: Session,
    query: InvoiceAccountingSyncQuery,
) -> ListResponse[InvoiceAccountingSyncRead]:
    """Return one deterministic page without changing billing state."""

    statement = db.query(Invoice).options(
        selectinload(Invoice.account),
        selectinload(Invoice.lines.and_(InvoiceLine.is_active.is_(True))),
    )
    if query.invoice_id is not None:
        statement = statement.filter(Invoice.id == query.invoice_id)
    if query.account_id is not None:
        statement = statement.filter(Invoice.account_id == query.account_id)
    if query.status is not None:
        statement = statement.filter(Invoice.status == query.status)
    if query.is_active is None:
        statement = statement.filter(Invoice.is_active.is_(True))
    else:
        statement = statement.filter(Invoice.is_active == query.is_active)
    invoices = apply_sync_page(
        statement,
        Invoice,
        updated_since=query.updated_since,
        limit=query.limit,
        offset=query.offset,
    ).all()
    items = [project_invoice_for_accounting(invoice) for invoice in invoices]
    return ListResponse[InvoiceAccountingSyncRead](
        items=items,
        count=len(items),
        limit=query.limit,
        offset=query.offset,
    )
