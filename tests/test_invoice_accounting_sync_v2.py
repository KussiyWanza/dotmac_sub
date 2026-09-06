"""Contracts for the fail-closed ERP Invoice accounting projection."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from app.models.billing import (
    Invoice,
    InvoiceDiscountType,
    InvoiceLine,
    InvoiceStatus,
    TaxApplication,
    TaxRate,
)
from app.schemas.billing import (
    InvoiceAccountingSyncDisposition,
    InvoiceAccountingSyncIssueCode,
    InvoiceAccountingSyncSourceKind,
    InvoiceLineCreate,
)
from app.services.billing.invoices import DraftInvoiceLineReplacement, InvoiceLines
from app.services.dotmac_erp.invoice_sync_projection import (
    ACCOUNTING_SYNC_CONTRACT_VERSION,
    InvoiceAccountingSyncQuery,
    list_invoice_accounting_sync,
    project_invoice_for_accounting,
)

_NOW = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)


def _invoice(db_session, subscriber, **overrides) -> Invoice:
    values = {
        "account_id": subscriber.id,
        "invoice_number": "INV-ACCOUNTING-V2",
        "status": InvoiceStatus.issued,
        "currency": "NGN",
        "subtotal": Decimal("100.00"),
        "discount_amount": Decimal("0.00"),
        "tax_total": Decimal("7.50"),
        "total": Decimal("107.50"),
        "balance_due": Decimal("107.50"),
        "issued_at": _NOW,
        "updated_at": _NOW,
        "is_active": True,
    }
    values.update(overrides)
    invoice = Invoice(**values)
    db_session.add(invoice)
    db_session.flush()
    return invoice


def _line(db_session, invoice, **overrides) -> InvoiceLine:
    snapshot_tax_rate = overrides.pop("snapshot_tax_rate", True)
    values = {
        "invoice_id": invoice.id,
        "description": "Internet service",
        "quantity": Decimal("1.000"),
        "unit_price": Decimal("100.00"),
        "amount": Decimal("100.00"),
        "tax_application": TaxApplication.exclusive,
        "is_active": True,
    }
    values.update(overrides)
    tax_rate_id = values.get("tax_rate_id")
    if tax_rate_id is not None and snapshot_tax_rate:
        tax_rate = db_session.get(TaxRate, tax_rate_id)
        assert tax_rate is not None
        values.update(
            {
                "tax_rate_snapshot_version": 1,
                "tax_rate_code_snapshot": tax_rate.code,
                "tax_rate_percent_snapshot": tax_rate.rate,
                "tax_rate_is_active_snapshot": tax_rate.is_active,
            }
        )
    line = InvoiceLine(**values)
    db_session.add(line)
    db_session.flush()
    return line


def _issue_codes(projection) -> set[InvoiceAccountingSyncIssueCode]:
    return {issue.code for issue in projection.issues}


def test_projection_is_ready_with_exact_source_tax_facts(
    db_session, subscriber
) -> None:
    tax_rate = TaxRate(
        name="VAT 7.5%",
        code="VAT75",
        rate=Decimal("7.5000"),
        is_active=True,
    )
    db_session.add(tax_rate)
    db_session.flush()
    invoice = _invoice(db_session, subscriber)
    line = _line(db_session, invoice, tax_rate_id=tax_rate.id)
    db_session.refresh(invoice)

    projection = project_invoice_for_accounting(invoice)

    assert projection.contract_version == ACCOUNTING_SYNC_CONTRACT_VERSION
    assert projection.source_kind is InvoiceAccountingSyncSourceKind.NATIVE
    assert projection.disposition is InvoiceAccountingSyncDisposition.READY
    assert projection.issues == []
    assert len(projection.lines) == 1
    projected_line = projection.lines[0]
    assert projected_line.id == line.id
    assert projected_line.tax_rate_code == "VAT75"
    assert projected_line.tax_rate_percent == Decimal("7.5000")
    assert projected_line.net_amount_before_discount == Decimal("100.00")
    assert projected_line.tax_amount_before_discount == Decimal("7.50")
    assert projected_line.gross_amount_before_discount == Decimal("107.50")


def test_projection_extracts_inclusive_tax_without_changing_gross(
    db_session, subscriber
) -> None:
    tax_rate = TaxRate(
        name="VAT 7.5%",
        code="VAT75",
        rate=Decimal("7.5000"),
        is_active=True,
    )
    db_session.add(tax_rate)
    db_session.flush()
    invoice = _invoice(
        db_session,
        subscriber,
        subtotal=Decimal("93.02"),
        tax_total=Decimal("6.98"),
        total=Decimal("100.00"),
        balance_due=Decimal("100.00"),
    )
    _line(
        db_session,
        invoice,
        amount=Decimal("100.00"),
        tax_rate_id=tax_rate.id,
        tax_application=TaxApplication.inclusive,
    )
    db_session.refresh(invoice)

    projection = project_invoice_for_accounting(invoice)

    assert projection.disposition is InvoiceAccountingSyncDisposition.READY
    assert projection.lines[0].net_amount_before_discount == Decimal("93.02")
    assert projection.lines[0].tax_amount_before_discount == Decimal("6.98")
    assert projection.lines[0].gross_amount_before_discount == Decimal("100.00")


def test_projection_uses_immutable_tax_snapshot_after_catalog_change(
    db_session, subscriber
) -> None:
    tax_rate = TaxRate(
        name="VAT 7.5%",
        code="VAT75",
        rate=Decimal("7.5000"),
        is_active=True,
    )
    db_session.add(tax_rate)
    db_session.flush()
    invoice = _invoice(db_session, subscriber)
    _line(db_session, invoice, tax_rate_id=tax_rate.id)
    db_session.refresh(invoice)

    before = project_invoice_for_accounting(invoice)
    tax_rate.code = "VAT200"
    tax_rate.rate = Decimal("20.0000")
    tax_rate.is_active = False
    db_session.flush()
    after = project_invoice_for_accounting(invoice)

    assert after.updated_at == before.updated_at
    assert after.disposition is InvoiceAccountingSyncDisposition.READY
    assert after.lines[0].tax_rate_code == "VAT75"
    assert after.lines[0].tax_rate_percent == Decimal("7.5000")
    assert after.lines[0].tax_rate_is_active is True
    assert after.lines[0].tax_amount_before_discount == Decimal("7.50")


def test_draft_line_owner_records_current_tax_snapshot(db_session, subscriber) -> None:
    tax_rate = TaxRate(
        name="VAT 7.5%",
        code="VAT75",
        rate=Decimal("7.5000"),
        is_active=True,
    )
    db_session.add(tax_rate)
    db_session.flush()
    invoice = _invoice(
        db_session,
        subscriber,
        status=InvoiceStatus.draft,
        issued_at=None,
    )

    InvoiceLines.replace_admin_draft_lines(
        db_session,
        invoice.id,
        (
            DraftInvoiceLineReplacement(
                payload=InvoiceLineCreate(
                    invoice_id=invoice.id,
                    description="Internet service",
                    quantity=Decimal("1.000"),
                    unit_price=Decimal("100.00"),
                    amount=Decimal("100.00"),
                    tax_rate_id=tax_rate.id,
                    tax_application=TaxApplication.exclusive,
                )
            ),
        ),
    )

    line = db_session.query(InvoiceLine).filter_by(invoice_id=invoice.id).one()
    assert line.tax_rate_snapshot_version == 1
    assert line.tax_rate_code_snapshot == "VAT75"
    assert line.tax_rate_percent_snapshot == Decimal("7.5000")
    assert line.tax_rate_is_active_snapshot is True


def test_projection_blocks_legacy_tax_line_without_snapshot(
    db_session, subscriber
) -> None:
    tax_rate = TaxRate(
        name="VAT 7.5%",
        code="VAT75",
        rate=Decimal("7.5000"),
        is_active=True,
    )
    db_session.add(tax_rate)
    db_session.flush()
    invoice = _invoice(db_session, subscriber)
    _line(
        db_session,
        invoice,
        tax_rate_id=tax_rate.id,
        snapshot_tax_rate=False,
    )
    db_session.refresh(invoice)

    projection = project_invoice_for_accounting(invoice)

    assert projection.disposition is InvoiceAccountingSyncDisposition.BLOCKED
    assert _issue_codes(projection) == {
        InvoiceAccountingSyncIssueCode.TAX_SNAPSHOT_MISSING,
        InvoiceAccountingSyncIssueCode.TAXED_HEADER_WITHOUT_LINE_TAX,
    }
    assert projection.lines[0].tax_rate_percent is None


def test_projection_names_taxed_header_without_line_tax(db_session, subscriber) -> None:
    invoice = _invoice(
        db_session,
        subscriber,
        subtotal=Decimal("17500.00"),
        tax_total=Decimal("1312.50"),
        total=Decimal("18812.50"),
        balance_due=Decimal("18812.50"),
    )
    _line(
        db_session,
        invoice,
        unit_price=Decimal("17500.00"),
        amount=Decimal("17500.00"),
    )
    db_session.refresh(invoice)

    projection = project_invoice_for_accounting(invoice)

    assert projection.disposition is InvoiceAccountingSyncDisposition.BLOCKED
    assert _issue_codes(projection) == {
        InvoiceAccountingSyncIssueCode.TAXED_HEADER_WITHOUT_LINE_TAX
    }
    issue = projection.issues[0]
    assert issue.expected_amount == Decimal("0.00")
    assert issue.actual_amount == Decimal("1312.50")


def test_projection_marks_splynx_archive_header_gap_explicitly(
    db_session, subscriber
) -> None:
    invoice = _invoice(
        db_session,
        subscriber,
        subtotal=Decimal("0.00"),
        tax_total=Decimal("0.00"),
        total=Decimal("100.00"),
        balance_due=Decimal("100.00"),
        splynx_invoice_id=987,
    )
    _line(db_session, invoice)
    db_session.refresh(invoice)

    projection = project_invoice_for_accounting(invoice)

    assert projection.source_kind is InvoiceAccountingSyncSourceKind.SPLYNX_LEGACY
    assert projection.disposition is InvoiceAccountingSyncDisposition.BLOCKED
    assert _issue_codes(projection) == {
        InvoiceAccountingSyncIssueCode.LEGACY_HEADER_TOTALS_MISSING,
        InvoiceAccountingSyncIssueCode.HEADER_TOTAL_MISMATCH,
    }


def test_projection_refuses_to_invent_discount_line_allocation(
    db_session, subscriber
) -> None:
    tax_rate = TaxRate(
        name="VAT 7.5%",
        code="VAT75",
        rate=Decimal("7.5000"),
        is_active=True,
    )
    db_session.add(tax_rate)
    db_session.flush()
    invoice = _invoice(
        db_session,
        subscriber,
        subtotal=Decimal("1000.00"),
        tax_total=Decimal("75.00"),
        total=Decimal("1075.00"),
        balance_due=Decimal("1075.00"),
    )
    _line(
        db_session,
        invoice,
        unit_price=Decimal("1000.00"),
        amount=Decimal("1000.00"),
        tax_rate_id=tax_rate.id,
    )
    db_session.refresh(invoice)
    # Exercise the pure resolver after all DB reads. Setting these in-memory
    # avoids manufacturing unrelated discount actor/history evidence merely to
    # test the read model's fail-closed apportionment behavior.
    invoice.discount_type = InvoiceDiscountType.percentage.value
    invoice.discount_value = Decimal("10.00")
    invoice.discount_amount = Decimal("100.00")
    invoice.tax_total = Decimal("67.50")
    invoice.total = Decimal("967.50")
    invoice.balance_due = Decimal("967.50")

    projection = project_invoice_for_accounting(invoice)

    assert projection.discounted_subtotal == Decimal("900.00")
    assert projection.disposition is InvoiceAccountingSyncDisposition.BLOCKED
    assert _issue_codes(projection) == {
        InvoiceAccountingSyncIssueCode.DISCOUNT_ALLOCATION_UNDEFINED,
    }


def test_list_query_is_watermarked_and_returns_typed_page(
    db_session, subscriber
) -> None:
    old = _invoice(
        db_session,
        subscriber,
        invoice_number="INV-OLD",
        subtotal=Decimal("0.00"),
        tax_total=Decimal("0.00"),
        total=Decimal("0.00"),
        balance_due=Decimal("0.00"),
        updated_at=datetime(2026, 9, 5, 12, 0, tzinfo=UTC),
    )
    current = _invoice(
        db_session,
        subscriber,
        invoice_number="INV-CURRENT",
        subtotal=Decimal("0.00"),
        tax_total=Decimal("0.00"),
        total=Decimal("0.00"),
        balance_due=Decimal("0.00"),
    )

    page = list_invoice_accounting_sync(
        db_session,
        InvoiceAccountingSyncQuery(
            invoice_id=None,
            account_id=None,
            status=None,
            is_active=None,
            updated_since=_NOW,
            limit=500,
            offset=0,
        ),
    )

    assert [item.source_invoice_id for item in page.items] == [current.id]
    assert old.id not in {item.source_invoice_id for item in page.items}
    assert page.count == 1
    assert page.limit == 500
    assert page.offset == 0


def test_list_query_can_target_one_invoice_for_explicit_replay(
    db_session, subscriber
) -> None:
    selected = _invoice(
        db_session,
        subscriber,
        invoice_number="INV-REPLAY-SELECTED",
        subtotal=Decimal("0.00"),
        tax_total=Decimal("0.00"),
        total=Decimal("0.00"),
        balance_due=Decimal("0.00"),
    )
    other = _invoice(
        db_session,
        subscriber,
        invoice_number="INV-REPLAY-OTHER",
        subtotal=Decimal("0.00"),
        tax_total=Decimal("0.00"),
        total=Decimal("0.00"),
        balance_due=Decimal("0.00"),
    )

    page = list_invoice_accounting_sync(
        db_session,
        InvoiceAccountingSyncQuery(
            invoice_id=selected.id,
            account_id=None,
            status=None,
            is_active=None,
            updated_since=None,
            limit=500,
            offset=0,
        ),
    )

    assert [item.source_invoice_id for item in page.items] == [selected.id]
    assert other.id not in {item.source_invoice_id for item in page.items}
