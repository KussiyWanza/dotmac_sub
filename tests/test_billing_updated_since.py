"""Tests for the incremental-sync ``updated_since`` watermark on the billing
list endpoints (invoices / payments / credit-notes).

Context: the ERP AR sync re-listed every row each cycle via OFFSET pagination
over an unindexed global ``created_at`` sort, starving dotmac_sub's DB pool.
The fix threads an ``updated_since`` cutoff into the list query (filtering on
``updated_at``) so ERP can pull only the delta. These tests pin the filter
semantics, backward compatibility when the param is omitted, and stable
ordering for deterministic forward paging.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.models.billing import (
    CreditNote,
    CreditNoteLine,
    InvoiceLine,
    Payment,
    PaymentAllocation,
    TaxApplication,
    TaxRate,
)
from app.schemas.billing import (
    CreditNoteSyncRead,
    InvoiceCreate,
    InvoiceLineUpdate,
    InvoiceSyncRead,
    PaymentSyncRead,
)
from app.services import billing as billing_service

_T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def _make_invoice(db, account_id, updated_at):
    invoice = billing_service.invoices.create(
        db,
        InvoiceCreate(
            account_id=account_id,
            currency="NGN",
            subtotal=Decimal("0.00"),
            tax_total=Decimal("0.00"),
            total=Decimal("0.00"),
            balance_due=Decimal("0.00"),
        ),
    )
    invoice.updated_at = updated_at
    db.add(invoice)
    db.flush()
    return invoice


def test_invoice_updated_since_filters_at_or_after_cutoff(
    db_session, subscriber_account
):
    old = _make_invoice(db_session, subscriber_account.id, _T0)
    on_cutoff = _make_invoice(
        db_session, subscriber_account.id, _T0 + timedelta(days=1)
    )
    newer = _make_invoice(db_session, subscriber_account.id, _T0 + timedelta(days=2))

    cutoff = _T0 + timedelta(days=1)
    rows = billing_service.invoices.list(
        db_session,
        None,
        None,
        None,
        "updated_at",
        "asc",
        50,
        0,
        updated_since=cutoff,
    )
    ids = {r.id for r in rows}

    # Row exactly at the cutoff is inclusive; strictly-older row excluded.
    assert on_cutoff.id in ids
    assert newer.id in ids
    assert old.id not in ids


def test_invoice_updated_since_omitted_is_unchanged(db_session, subscriber_account):
    _make_invoice(db_session, subscriber_account.id, _T0)
    _make_invoice(db_session, subscriber_account.id, _T0 + timedelta(days=5))

    # Omitting the watermark returns everything (fully backward-compatible).
    rows = billing_service.invoices.list(
        db_session, None, None, None, "created_at", "desc", 50, 0
    )
    assert len(rows) == 2


def test_invoice_updated_since_ordering_is_stable(db_session, subscriber_account):
    # Two rows share the same updated_at → the id tiebreaker must give a
    # deterministic forward-paging order.
    same = _T0 + timedelta(days=3)
    a = _make_invoice(db_session, subscriber_account.id, same)
    b = _make_invoice(db_session, subscriber_account.id, same)
    later = _make_invoice(db_session, subscriber_account.id, same + timedelta(hours=1))

    rows = billing_service.invoices.list(
        db_session,
        None,
        None,
        None,
        "updated_at",
        "asc",
        50,
        0,
        updated_since=_T0,
    )
    ordered_ids = [r.id for r in rows]

    # updated_at ascending, then id ascending among the tied pair.
    tied_expected = sorted([a.id, b.id], key=str)
    assert ordered_ids[:2] == tied_expected
    assert ordered_ids[2] == later.id


def test_invoice_sync_feed_is_lightweight_and_watermarked(
    db_session, subscriber_account
):
    old = _make_invoice(db_session, subscriber_account.id, _T0)
    current = _make_invoice(db_session, subscriber_account.id, _T0 + timedelta(days=1))
    vat = TaxRate(name="VAT 7.5%", code="VAT75", rate=Decimal("7.5"))
    db_session.add(vat)
    db_session.flush()
    db_session.add(
        InvoiceLine(
            invoice_id=current.id,
            description="Internet service",
            quantity=Decimal("1"),
            unit_price=Decimal("100.00"),
            amount=Decimal("100.00"),
            tax_rate_id=vat.id,
            tax_application=TaxApplication.inclusive,
        )
    )
    db_session.add(
        InvoiceLine(
            invoice_id=current.id,
            description="Removed charge",
            quantity=Decimal("1"),
            unit_price=Decimal("50.00"),
            amount=Decimal("50.00"),
            is_active=False,
        )
    )
    db_session.flush()

    response = billing_service.invoices.sync_list_response(
        db_session,
        account_id=None,
        status=None,
        is_active=None,
        updated_since=_T0 + timedelta(days=1),
        limit=500,
        offset=0,
    )

    assert [row.id for row in response["items"]] == [current.id]
    assert old.id not in {row.id for row in response["items"]}
    payload = InvoiceSyncRead.model_validate(response["items"][0]).model_dump()
    assert payload["lines"][0]["description"] == "Internet service"
    assert payload["lines"][0]["tax_rate_id"] == vat.id
    assert payload["lines"][0]["tax_application"] == TaxApplication.inclusive
    assert len(payload["lines"]) == 1
    assert "payment_allocations" not in payload
    assert "billing_period_start" not in payload


def test_invoice_line_edit_advances_parent_sync_watermark(
    db_session, subscriber_account
):
    invoice = _make_invoice(db_session, subscriber_account.id, _T0)
    line = InvoiceLine(
        invoice_id=invoice.id,
        description="Old description",
        quantity=Decimal("1"),
        unit_price=Decimal("100.00"),
        amount=Decimal("100.00"),
    )
    db_session.add(line)
    db_session.commit()
    invoice.updated_at = _T0
    db_session.commit()

    billing_service.invoice_lines.update(
        db_session,
        str(line.id),
        InvoiceLineUpdate(description="Corrected description"),
    )

    db_session.refresh(invoice)
    updated_at = invoice.updated_at
    if updated_at.tzinfo is None:  # SQLite drops timezone information.
        updated_at = updated_at.replace(tzinfo=UTC)
    assert updated_at > _T0


def test_payment_updated_since_filters(db_session, subscriber_account):
    old = Payment(
        account_id=subscriber_account.id,
        amount=Decimal("10.00"),
        currency="NGN",
        updated_at=_T0,
    )
    new = Payment(
        account_id=subscriber_account.id,
        amount=Decimal("20.00"),
        currency="NGN",
        updated_at=_T0 + timedelta(days=2),
    )
    db_session.add_all([old, new])
    db_session.flush()

    rows = billing_service.payments.list(
        db_session,
        None,
        None,
        None,
        None,
        "updated_at",
        "asc",
        50,
        0,
        updated_since=_T0 + timedelta(days=1),
    )
    ids = {r.id for r in rows}
    assert new.id in ids
    assert old.id not in ids


def test_credit_note_updated_since_filters(db_session, subscriber_account):
    old = CreditNote(
        account_id=subscriber_account.id,
        currency="NGN",
        updated_at=_T0,
    )
    new = CreditNote(
        account_id=subscriber_account.id,
        currency="NGN",
        updated_at=_T0 + timedelta(days=2),
    )
    db_session.add_all([old, new])
    db_session.flush()

    rows = billing_service.credit_notes.list(
        db_session,
        None,
        None,
        None,
        None,
        "updated_at",
        "asc",
        50,
        0,
        updated_since=_T0 + timedelta(days=1),
    )
    ids = {r.id for r in rows}
    assert new.id in ids
    assert old.id not in ids


def test_payment_sync_feed_is_lightweight_and_watermarked(
    db_session, subscriber_account
):
    invoice = _make_invoice(db_session, subscriber_account.id, _T0)
    old = Payment(
        account_id=subscriber_account.id,
        amount=Decimal("10.00"),
        currency="NGN",
        updated_at=_T0,
    )
    current = Payment(
        account_id=subscriber_account.id,
        amount=Decimal("20.00"),
        currency="NGN",
        updated_at=_T0 + timedelta(days=1),
    )
    db_session.add_all([old, current])
    db_session.flush()
    db_session.add(
        PaymentAllocation(
            payment_id=current.id,
            invoice_id=invoice.id,
            amount=Decimal("5.00"),
        )
    )
    db_session.flush()

    response = billing_service.payments.sync_list_response(
        db_session,
        account_id=None,
        status=None,
        is_active=None,
        updated_since=_T0 + timedelta(days=1),
        limit=500,
        offset=0,
    )

    assert [row.id for row in response["items"]] == [current.id]
    payload = PaymentSyncRead.model_validate(response["items"][0]).model_dump()
    assert payload["allocations"][0]["invoice_id"] == invoice.id
    assert "provider_events" not in payload
    assert "created_at" not in payload


def test_credit_note_sync_feed_only_loads_active_lines(db_session, subscriber_account):
    note = CreditNote(
        account_id=subscriber_account.id,
        currency="NGN",
        subtotal=Decimal("100.00"),
        tax_total=Decimal("0.00"),
        total=Decimal("100.00"),
        updated_at=_T0 + timedelta(days=1),
    )
    db_session.add(note)
    db_session.flush()
    db_session.add_all(
        [
            CreditNoteLine(
                credit_note_id=note.id,
                description="Valid correction",
                amount=Decimal("100.00"),
            ),
            CreditNoteLine(
                credit_note_id=note.id,
                description="Removed correction",
                amount=Decimal("50.00"),
                is_active=False,
            ),
        ]
    )
    db_session.flush()

    response = billing_service.credit_notes.sync_list_response(
        db_session,
        account_id=None,
        status=None,
        is_active=None,
        updated_since=_T0 + timedelta(days=1),
        limit=500,
        offset=0,
    )

    payload = CreditNoteSyncRead.model_validate(response["items"][0]).model_dump()
    assert [line["description"] for line in payload["lines"]] == ["Valid correction"]
    assert "applications" not in payload


# ---------------------------------------------------------------------------
# Additive keyset cursor (after_updated_at / after_id) on the legacy feed.
#
# This shares its implementation (app.services.sync_feeds.apply_sync_page)
# with the v2 accounting-sync feed, whose test module carries the full
# tie-group / mid-walk-update / replay property tests. These two are the
# narrower legacy-feed checks: cursor omitted stays byte-identical, and the
# cursor mode itself does the basic right thing here too.
# ---------------------------------------------------------------------------


def test_invoice_sync_feed_omitting_cursor_is_byte_identical_to_offset_only(
    db_session, subscriber_account
):
    a = _make_invoice(db_session, subscriber_account.id, _T0)
    b = _make_invoice(db_session, subscriber_account.id, _T0 + timedelta(days=1))

    without_cursor_kwargs = billing_service.invoices.sync_list_response(
        db_session,
        account_id=None,
        status=None,
        is_active=None,
        updated_since=None,
        limit=500,
        offset=0,
    )
    explicit_none_cursor = billing_service.invoices.sync_list_response(
        db_session,
        account_id=None,
        status=None,
        is_active=None,
        updated_since=None,
        limit=500,
        offset=0,
        after_updated_at=None,
        after_id=None,
    )

    assert [row.id for row in without_cursor_kwargs["items"]] == [a.id, b.id]
    assert [row.id for row in explicit_none_cursor["items"]] == [a.id, b.id]
    assert without_cursor_kwargs == explicit_none_cursor


def test_invoice_sync_feed_keyset_walk_covers_every_row_once(
    db_session, subscriber_account
):
    a = _make_invoice(db_session, subscriber_account.id, _T0)
    b = _make_invoice(db_session, subscriber_account.id, _T0 + timedelta(days=1))
    c = _make_invoice(db_session, subscriber_account.id, _T0 + timedelta(days=2))

    page1 = billing_service.invoices.sync_list_response(
        db_session,
        account_id=None,
        status=None,
        is_active=None,
        updated_since=None,
        limit=2,
        offset=0,
    )
    assert [row.id for row in page1["items"]] == [a.id, b.id]

    cursor = page1["items"][-1]
    page2 = billing_service.invoices.sync_list_response(
        db_session,
        account_id=None,
        status=None,
        is_active=None,
        updated_since=None,
        limit=2,
        offset=0,
        after_updated_at=cursor.updated_at,
        after_id=cursor.id,
    )
    assert [row.id for row in page2["items"]] == [c.id]


def test_invoice_sync_feed_rejects_a_partial_cursor_pair_defensively(
    db_session, subscriber_account
):
    # Belt-and-suspenders check inside the shared helper itself, independent
    # of the HTTP-layer 422 validation covered in
    # tests/test_invoice_accounting_sync_v2.py.
    with pytest.raises(ValueError, match="after_updated_at and after_id"):
        billing_service.invoices.sync_list_response(
            db_session,
            account_id=None,
            status=None,
            is_active=None,
            updated_since=None,
            limit=500,
            offset=0,
            after_updated_at=_T0,
            after_id=None,
        )
