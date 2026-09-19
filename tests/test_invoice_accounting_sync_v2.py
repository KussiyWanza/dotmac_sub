"""Contracts for the fail-closed ERP Invoice accounting projection."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.billing import (
    INTEGRATION_ACCOUNTING_SYNC_READ_SCOPE,
)
from app.api.billing import (
    router as billing_router,
)
from app.db import get_db
from app.models.auth import ApiKey
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
from app.services.auth import hash_api_key
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


# ---------------------------------------------------------------------------
# Additive keyset cursor (after_updated_at / after_id)
# ---------------------------------------------------------------------------


def _zero_invoice(db_session, subscriber, **overrides) -> Invoice:
    values = {
        "subtotal": Decimal("0.00"),
        "tax_total": Decimal("0.00"),
        "total": Decimal("0.00"),
        "balance_due": Decimal("0.00"),
    }
    values.update(overrides)
    return _invoice(db_session, subscriber, **values)


def _query(**overrides) -> InvoiceAccountingSyncQuery:
    values: dict = {
        "invoice_id": None,
        "account_id": None,
        "status": None,
        "is_active": None,
        "updated_since": None,
        "limit": 500,
        "offset": 0,
        "after_updated_at": None,
        "after_id": None,
    }
    values.update(overrides)
    return InvoiceAccountingSyncQuery(**values)


def test_query_defaults_the_new_cursor_fields_so_old_callers_are_unaffected() -> None:
    # Every existing caller in this test module constructs the dataclass
    # without after_updated_at/after_id; the defaults must keep that
    # construction byte-identical (offset-only) behavior.
    query = InvoiceAccountingSyncQuery(
        invoice_id=None,
        account_id=None,
        status=None,
        is_active=None,
        updated_since=None,
        limit=500,
        offset=0,
    )
    assert query.after_updated_at is None
    assert query.after_id is None


def test_keyset_walk_returns_every_row_in_a_tie_group_exactly_once(
    db_session, subscriber
) -> None:
    """Several rows share the exact same updated_at (a tie group bigger than
    one page). The (updated_at, id) tiebreaker must let a keyset walk cross
    the tie without skipping or repeating a row within the same revision."""
    same = _NOW
    rows = [
        _zero_invoice(
            db_session, subscriber, invoice_number=f"INV-TIE-{i}", updated_at=same
        )
        for i in range(3)
    ]
    rows_by_id = sorted(rows, key=lambda r: str(r.id))

    page1 = list_invoice_accounting_sync(db_session, _query(limit=2))
    assert [item.source_invoice_id for item in page1.items] == [
        r.id for r in rows_by_id[:2]
    ]

    last = page1.items[-1]
    page2 = list_invoice_accounting_sync(
        db_session,
        _query(
            limit=2, after_updated_at=last.updated_at, after_id=last.source_invoice_id
        ),
    )
    assert [item.source_invoice_id for item in page2.items] == [rows_by_id[2].id]

    # The walk terminates: one more page with the final cursor is empty.
    tail = page2.items[-1]
    page3 = list_invoice_accounting_sync(
        db_session,
        _query(
            limit=2, after_updated_at=tail.updated_at, after_id=tail.source_invoice_id
        ),
    )
    assert page3.items == []

    seen = [item.source_invoice_id for item in (*page1.items, *page2.items)]
    assert sorted(seen, key=str) == sorted((r.id for r in rows), key=str)
    assert len(seen) == len(set(seen))


def test_keyset_walk_does_not_skip_a_stationary_row_when_an_earlier_row_is_updated(
    db_session, subscriber
) -> None:
    """Reproduces the concrete OFFSET-paging hazard this cursor replaces: a
    row already returned (A) is updated so it re-sorts past rows the walk
    has not reached yet. Under OFFSET paging this shifts every later row's
    position and can drop an untouched row (C) entirely. Under the keyset
    cursor, C's own (updated_at, id) never changed, so it must still be
    returned — this is the property under test, not A's reappearance."""
    t0 = _NOW
    t1 = t0 + timedelta(minutes=1)
    t2 = t0 + timedelta(minutes=2)
    t3 = t0 + timedelta(minutes=3)
    row_a = _zero_invoice(db_session, subscriber, invoice_number="INV-A", updated_at=t0)
    row_b = _zero_invoice(db_session, subscriber, invoice_number="INV-B", updated_at=t1)
    row_c = _zero_invoice(db_session, subscriber, invoice_number="INV-C", updated_at=t2)
    row_d = _zero_invoice(db_session, subscriber, invoice_number="INV-D", updated_at=t3)

    page1 = list_invoice_accounting_sync(db_session, _query(limit=2))
    assert [item.source_invoice_id for item in page1.items] == [row_a.id, row_b.id]
    cursor = page1.items[-1]

    # Concurrent write: A (already returned, unrelated to the still-pending
    # walk) is modified and now sorts after D. This is exactly the shape
    # that causes an OFFSET walker to skip the untouched row C.
    t4 = t3 + timedelta(minutes=1)
    row_a.updated_at = t4
    db_session.add(row_a)
    db_session.flush()

    page2 = list_invoice_accounting_sync(
        db_session,
        _query(
            limit=2,
            after_updated_at=cursor.updated_at,
            after_id=cursor.source_invoice_id,
        ),
    )
    # C is stationary and must not be skipped, regardless of A's update.
    assert [item.source_invoice_id for item in page2.items] == [row_c.id, row_d.id]

    cursor2 = page2.items[-1]
    page3 = list_invoice_accounting_sync(
        db_session,
        _query(
            limit=2,
            after_updated_at=cursor2.updated_at,
            after_id=cursor2.source_invoice_id,
        ),
    )
    # A reappears with its new revision — intended per Decision A (the
    # downstream consumer is idempotent on (invoice, source_updated_at)),
    # not a duplicate: A was never returned at this later position before.
    assert [item.source_invoice_id for item in page3.items] == [row_a.id]
    # SQLite's DATETIME column drops tzinfo on round-trip (unlike PostgreSQL),
    # and this assertion now exercises a genuine reload — `apply_sync_page`
    # sets `populate_existing=True`, so this is no longer the same in-memory
    # `row_a` object mutated above, it's a fresh read from the DB. Normalize
    # before comparing, same as the PostgreSQL dual-session counterpart in
    # tests/integration/test_invoice_accounting_sync_keyset_postgres.py.
    observed_updated_at = page3.items[0].updated_at
    if observed_updated_at.tzinfo is None:
        observed_updated_at = observed_updated_at.replace(tzinfo=UTC)
    assert observed_updated_at.astimezone(UTC) == t4


def test_keyset_replay_is_deterministic_when_nothing_changes(
    db_session, subscriber
) -> None:
    row_a = _zero_invoice(
        db_session, subscriber, invoice_number="INV-R-A", updated_at=_NOW
    )
    _zero_invoice(
        db_session,
        subscriber,
        invoice_number="INV-R-B",
        updated_at=_NOW + timedelta(minutes=1),
    )

    query = _query(limit=1, after_updated_at=row_a.updated_at, after_id=row_a.id)
    first = list_invoice_accounting_sync(db_session, query)
    second = list_invoice_accounting_sync(db_session, query)

    assert [item.source_invoice_id for item in first.items] == [
        item.source_invoice_id for item in second.items
    ]
    assert [item.updated_at for item in first.items] == [
        item.updated_at for item in second.items
    ]


# ---------------------------------------------------------------------------
# HTTP-level: partial cursor validation + scope enforcement
# ---------------------------------------------------------------------------


@pytest.fixture()
def billing_client(db_session) -> TestClient:
    app = FastAPI()
    app.include_router(billing_router)

    def _db():
        yield db_session

    app.dependency_overrides[get_db] = _db
    return TestClient(app)


def _api_key(db_session, *, scopes: list[str], raw: str) -> str:
    db_session.add(
        ApiKey(
            label=raw,
            key_hash=hash_api_key(raw),
            scopes=scopes,
            is_active=True,
        )
    )
    db_session.commit()
    return raw


class TestPartialCursorPairIs422:
    def test_legacy_sync_feed_rejects_a_partial_cursor_pair(
        self, db_session, billing_client
    ) -> None:
        raw = _api_key(db_session, scopes=["billing:invoice:read"], raw="legacy-key")
        response = billing_client.get(
            "/invoices/sync",
            params={"after_updated_at": _NOW.isoformat()},
            headers={"x-api-key": raw},
        )
        assert response.status_code == 422
        assert "after_updated_at and after_id" in response.json()["detail"]

    def test_v2_feed_rejects_a_partial_cursor_pair(
        self, db_session, billing_client
    ) -> None:
        raw = _api_key(db_session, scopes=["billing:invoice:read"], raw="v2-key")
        response = billing_client.get(
            "/invoices/accounting-sync/v2",
            params={"after_id": "00000000-0000-0000-0000-000000000001"},
            headers={"x-api-key": raw},
        )
        assert response.status_code == 422
        assert "after_updated_at and after_id" in response.json()["detail"]


class TestAccountingSyncReadScope:
    def test_narrow_scope_reaches_v2_but_not_the_legacy_feed(
        self, db_session, billing_client
    ) -> None:
        raw = _api_key(
            db_session,
            scopes=[INTEGRATION_ACCOUNTING_SYNC_READ_SCOPE],
            raw="narrow-key",
        )
        headers = {"x-api-key": raw}

        v2_response = billing_client.get(
            "/invoices/accounting-sync/v2", headers=headers
        )
        assert v2_response.status_code == 200

        legacy_response = billing_client.get("/invoices/sync", headers=headers)
        assert legacy_response.status_code == 403

    def test_billing_invoice_read_still_reaches_both_feeds(
        self, db_session, billing_client
    ) -> None:
        raw = _api_key(db_session, scopes=["billing:invoice:read"], raw="broad-key")
        headers = {"x-api-key": raw}

        assert (
            billing_client.get(
                "/invoices/accounting-sync/v2", headers=headers
            ).status_code
            == 200
        )
        assert billing_client.get("/invoices/sync", headers=headers).status_code == 200

    def test_unrelated_scope_is_refused_by_v2(self, db_session, billing_client) -> None:
        # Pins the authorization floor against a future accidental widening:
        # holding some OTHER, unrelated permission must not reach the v2 feed.
        raw = _api_key(db_session, scopes=["reports:billing:read"], raw="unrelated-key")
        response = billing_client.get(
            "/invoices/accounting-sync/v2", headers={"x-api-key": raw}
        )
        assert response.status_code == 403

    def test_no_scope_is_refused_by_v2(self, db_session, billing_client) -> None:
        raw = _api_key(db_session, scopes=[], raw="empty-scope-key")
        response = billing_client.get(
            "/invoices/accounting-sync/v2", headers={"x-api-key": raw}
        )
        assert response.status_code == 403
