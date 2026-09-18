from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.models.billing import LedgerEntry, LedgerEntryType, LedgerSource
from app.models.subscriber import Reseller, Subscriber
from app.services.web_billing_ledger import (
    CustomerLedgerQuery,
    LedgerEntryDetailQuery,
    build_customer_ledger_view,
    build_ledger_entries_data,
    build_ledger_entry_detail,
    render_ledger_csv,
)
from app.web.admin import billing_reporting


def _request(path: str) -> Request:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("utf-8"),
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 5555),
            "server": ("testserver", 80),
        }
    )


def _create_ledger_entry(
    db_session,
    *,
    account_id,
    entry_type: LedgerEntryType,
    amount: str,
    source: LedgerSource = LedgerSource.other,
    currency: str = "NGN",
    effective_date: datetime | None = None,
):
    entry = LedgerEntry(
        account_id=account_id,
        entry_type=entry_type,
        source=source,
        amount=Decimal(amount),
        currency=currency,
        memo="test",
        effective_date=effective_date,
    )
    db_session.add(entry)
    db_session.commit()
    db_session.refresh(entry)
    return entry


def test_build_ledger_entries_data_includes_totals(db_session, subscriber):
    _create_ledger_entry(
        db_session,
        account_id=subscriber.id,
        entry_type=LedgerEntryType.credit,
        amount="120.00",
    )
    _create_ledger_entry(
        db_session,
        account_id=subscriber.id,
        entry_type=LedgerEntryType.debit,
        amount="30.00",
    )

    state = build_ledger_entries_data(
        db_session,
        customer_ref=str(subscriber.id),
        entry_type=None,
    )

    assert state["ledger_totals"]["credit_count"] == 1
    assert state["ledger_totals"]["debit_count"] == 1
    assert state["ledger_totals"]["credit_total"] == pytest.approx(120.0)
    assert state["ledger_totals"]["debit_total"] == pytest.approx(30.0)
    assert state["ledger_totals"]["net_total"] == pytest.approx(90.0)


def test_build_ledger_entries_data_groups_totals_by_currency(db_session, subscriber):
    _create_ledger_entry(
        db_session,
        account_id=subscriber.id,
        entry_type=LedgerEntryType.credit,
        amount="120.00",
        currency="NGN",
    )
    _create_ledger_entry(
        db_session,
        account_id=subscriber.id,
        entry_type=LedgerEntryType.credit,
        amount="20.00",
        currency="USD",
    )
    _create_ledger_entry(
        db_session,
        account_id=subscriber.id,
        entry_type=LedgerEntryType.debit,
        amount="30.00",
        currency="NGN",
    )

    state = build_ledger_entries_data(
        db_session,
        customer_ref=str(subscriber.id),
        entry_type=None,
    )

    assert state["ledger_totals"]["credit_amounts"] == {
        "NGN": Decimal("120.00"),
        "USD": Decimal("20.00"),
    }
    assert state["ledger_totals"]["debit_amounts"] == {"NGN": Decimal("30.00")}
    assert state["ledger_totals"]["credit_display"] == "NGN 120.00, USD 20.00"
    assert state["ledger_totals"]["debit_display"] == "NGN 30.00"
    assert state["ledger_totals"]["net_display"] == "NGN 90.00, USD 20.00"


def test_ledger_totals_cover_full_filtered_cohort_not_display_limit(
    db_session, subscriber
):
    for _ in range(205):
        db_session.add(
            LedgerEntry(
                account_id=subscriber.id,
                entry_type=LedgerEntryType.credit,
                source=LedgerSource.payment,
                amount=Decimal("10.00"),
                currency="NGN",
                memo="high-cardinality cohort",
            )
        )
    db_session.commit()

    state = build_ledger_entries_data(
        db_session,
        customer_ref=str(subscriber.id),
        entry_type=None,
        limit=200,
    )

    assert len(state["entries"]) == 200
    assert state["ledger_totals"]["credit_count"] == 205
    assert state["ledger_totals"]["credit_amounts"] == {"NGN": Decimal("2050.00")}


def test_build_ledger_entries_data_applies_custom_date_range(db_session, subscriber):
    old_entry = _create_ledger_entry(
        db_session,
        account_id=subscriber.id,
        entry_type=LedgerEntryType.credit,
        amount="10.00",
    )
    old_entry.created_at = datetime(2026, 1, 20, 12, 0, tzinfo=UTC)
    db_session.add(old_entry)

    in_range_entry = _create_ledger_entry(
        db_session,
        account_id=subscriber.id,
        entry_type=LedgerEntryType.debit,
        amount="5.00",
    )
    in_range_entry.created_at = datetime(2026, 2, 28, 18, 30, tzinfo=UTC)
    db_session.add(in_range_entry)
    db_session.commit()

    state = build_ledger_entries_data(
        db_session,
        customer_ref=str(subscriber.id),
        entry_type=None,
        start_date="2026-02-01",
        end_date="2026-02-28",
    )

    assert len(state["entries"]) == 1
    assert state["entries"][0].id == in_range_entry.id
    assert state["ledger_totals"]["credit_count"] == 0
    assert state["ledger_totals"]["debit_count"] == 1
    assert state["start_date"] == "2026-02-01"
    assert state["end_date"] == "2026-02-28"


def test_build_ledger_entries_data_rejects_reversed_date_range(db_session, subscriber):
    with pytest.raises(HTTPException) as exc_info:
        build_ledger_entries_data(
            db_session,
            customer_ref=str(subscriber.id),
            entry_type=None,
            start_date="2026-03-01",
            end_date="2026-02-28",
        )

    assert exc_info.value.status_code == 400


def test_build_ledger_entries_data_filters_by_category(db_session, subscriber):
    _create_ledger_entry(
        db_session,
        account_id=subscriber.id,
        entry_type=LedgerEntryType.credit,
        amount="90.00",
        source=LedgerSource.payment,
    )
    _create_ledger_entry(
        db_session,
        account_id=subscriber.id,
        entry_type=LedgerEntryType.debit,
        amount="25.00",
        source=LedgerSource.invoice,
    )

    state = build_ledger_entries_data(
        db_session,
        customer_ref=str(subscriber.id),
        entry_type=None,
        category="payment",
    )

    assert len(state["entries"]) == 1
    assert state["entries"][0].source == LedgerSource.payment


def test_build_ledger_entries_data_filters_by_partner(db_session):
    reseller_a = Reseller(name="Partner A")
    reseller_b = Reseller(name="Partner B")
    db_session.add_all([reseller_a, reseller_b])
    db_session.commit()

    account_a = Subscriber(
        first_name="Partner",
        last_name="A",
        email="partner-a-ledger@example.com",
        reseller_id=reseller_a.id,
    )
    account_b = Subscriber(
        first_name="Partner",
        last_name="B",
        email="partner-b-ledger@example.com",
        reseller_id=reseller_b.id,
    )
    db_session.add_all([account_a, account_b])
    db_session.commit()

    _create_ledger_entry(
        db_session,
        account_id=account_a.id,
        entry_type=LedgerEntryType.credit,
        amount="70.00",
        source=LedgerSource.payment,
    )
    _create_ledger_entry(
        db_session,
        account_id=account_b.id,
        entry_type=LedgerEntryType.credit,
        amount="40.00",
        source=LedgerSource.payment,
    )

    state = build_ledger_entries_data(
        db_session,
        customer_ref=None,
        entry_type=None,
        partner_id=str(reseller_a.id),
    )

    assert len(state["entries"]) == 1
    assert state["entries"][0].account_id == account_a.id
    assert state["selected_partner_id"] == str(reseller_a.id)


def test_customer_ledger_view_is_strictly_scoped_and_entries_are_clickable(
    db_session, subscriber
):
    other = Subscriber(
        first_name="Other",
        last_name="Customer",
        email="other-customer-ledger@example.com",
    )
    db_session.add(other)
    db_session.commit()
    customer_entry = _create_ledger_entry(
        db_session,
        account_id=subscriber.id,
        entry_type=LedgerEntryType.credit,
        amount="120.00",
        source=LedgerSource.payment,
    )
    _create_ledger_entry(
        db_session,
        account_id=other.id,
        entry_type=LedgerEntryType.debit,
        amount="900.00",
        source=LedgerSource.adjustment,
    )

    view = build_customer_ledger_view(
        db_session,
        query=CustomerLedgerQuery(account_id=subscriber.id),
    )

    assert [entry.id for entry in view.entries] == [customer_entry.id]
    assert view.entries[0].detail_url == (f"/admin/billing/ledger/{customer_entry.id}")
    assert view.summary.credit_display == "NGN 120.00"
    assert view.summary.debit_display == "NGN 0.00"
    assert view.full_ledger_url == (
        f"/admin/billing/ledger?customer_ref={subscriber.id}"
    )
    assert view.export_url == (
        f"/admin/billing/ledger/export.csv?customer_ref={subscriber.id}"
    )
    assert view.page == 1
    assert view.per_page == 10
    assert view.total_entries == 1
    assert view.previous_page_url is None
    assert view.next_page_url is None


def test_customer_ledger_view_pages_newest_activity_in_ten_entry_slices(
    db_session, subscriber
):
    entries = [
        _create_ledger_entry(
            db_session,
            account_id=subscriber.id,
            entry_type=LedgerEntryType.credit,
            amount=str(index),
            source=LedgerSource.payment,
            effective_date=datetime(2026, 1, index, tzinfo=UTC),
        )
        for index in range(1, 13)
    ]

    first_page = build_customer_ledger_view(
        db_session,
        query=CustomerLedgerQuery(account_id=subscriber.id),
    )
    second_page = build_customer_ledger_view(
        db_session,
        query=CustomerLedgerQuery(account_id=subscriber.id, page=2),
    )

    assert [entry.id for entry in first_page.entries] == [
        entry.id for entry in reversed(entries[2:])
    ]
    assert [entry.running_balance for entry in first_page.entries] == [
        Decimal(str(sum(range(1, index + 1)))) for index in range(12, 2, -1)
    ]
    assert [entry.id for entry in second_page.entries] == [
        entry.id for entry in reversed(entries[:2])
    ]
    assert [entry.running_balance for entry in second_page.entries] == [
        Decimal("3"),
        Decimal("1"),
    ]
    assert first_page.summary.credit_count == 12
    assert second_page.summary.credit_count == 12
    assert first_page.total_entries == 12
    assert first_page.total_pages == 2
    assert first_page.page_start == 1
    assert first_page.page_end == 10
    assert first_page.previous_page_url is None
    assert first_page.next_page_url == (
        f"/admin/customers/person/{subscriber.id}/billing/ledger?page=2"
    )
    assert second_page.page_start == 11
    assert second_page.page_end == 12
    assert second_page.previous_page_url == (
        f"/admin/customers/person/{subscriber.id}/billing/ledger?page=1"
    )
    assert second_page.next_page_url is None


def test_ledger_entry_detail_preserves_customer_and_source_evidence(
    db_session, subscriber
):
    entry = _create_ledger_entry(
        db_session,
        account_id=subscriber.id,
        entry_type=LedgerEntryType.debit,
        amount="45.00",
        source=LedgerSource.adjustment,
    )

    detail = build_ledger_entry_detail(
        db_session,
        query=LedgerEntryDetailQuery(entry_id=entry.id),
    )

    assert detail is not None
    assert detail.id == entry.id
    assert detail.account_id == subscriber.id
    assert detail.entry_type is LedgerEntryType.debit
    assert detail.source is LedgerSource.adjustment
    assert detail.amount == Decimal("45.00")


def test_ledger_entry_detail_route_renders_clickthrough_target(
    monkeypatch, db_session, subscriber
):
    entry = _create_ledger_entry(
        db_session,
        account_id=subscriber.id,
        entry_type=LedgerEntryType.credit,
        amount="88.00",
        source=LedgerSource.payment,
    )
    import app.web.admin as admin_module

    monkeypatch.setattr(admin_module, "get_current_user", lambda request: None)
    monkeypatch.setattr(admin_module, "get_sidebar_stats", lambda db: {})

    response = billing_reporting.billing_ledger_entry_detail(
        request=_request(f"/admin/billing/ledger/{entry.id}"),
        entry_id=entry.id,
        db=db_session,
    )
    rendered = response.body.decode("utf-8")

    assert response.status_code == 200
    assert "Ledger Entry" in rendered
    assert "NGN 88.00" in rendered
    assert f'href="/admin/customers/person/{subscriber.id}"' in rendered


def test_render_ledger_csv_contains_split_debit_and_credit(db_session, subscriber):
    debit_entry = _create_ledger_entry(
        db_session,
        account_id=subscriber.id,
        entry_type=LedgerEntryType.debit,
        amount="15.00",
        source=LedgerSource.invoice,
    )
    credit_entry = _create_ledger_entry(
        db_session,
        account_id=subscriber.id,
        entry_type=LedgerEntryType.credit,
        amount="22.00",
        source=LedgerSource.payment,
    )

    csv_text = render_ledger_csv([debit_entry, credit_entry])

    assert (
        "entry_id,customer_name,entry_type,source,debit_amount,credit_amount,currency,description,date"
        in csv_text
    )
    assert subscriber.name in csv_text
    assert str(subscriber.id) not in csv_text
    assert ",debit,invoice,15.00,,NGN," in csv_text
    assert ",credit,payment,,22.00,NGN," in csv_text
