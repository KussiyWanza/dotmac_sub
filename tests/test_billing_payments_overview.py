from __future__ import annotations

import csv
import io
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import event

from app.models.billing import (
    Invoice,
    InvoiceStatus,
    Payment,
    PaymentAllocation,
    PaymentMethod,
    PaymentMethodType,
    PaymentStatus,
)
from app.models.subscriber import Reseller, Subscriber, SubscriberCategory
from app.services import display_format
from app.services.inclusive_date_range import InclusiveDateRangeError
from app.services.web_billing_payments import (
    PAYMENTS_LIST_DEFINITION,
    build_payments_list_data,
    build_payments_list_query,
    list_payments_for_scope,
    render_payments_csv,
    stream_payments_csv,
)


def _create_payment_method(
    db_session, account_id, method_type: PaymentMethodType
) -> PaymentMethod:
    method = PaymentMethod(
        account_id=account_id,
        method_type=method_type,
        label=method_type.value,
        is_active=True,
    )
    db_session.add(method)
    db_session.commit()
    db_session.refresh(method)
    return method


def _create_payment(
    db_session,
    *,
    account_id,
    amount: str,
    status: PaymentStatus,
    created_at: datetime,
    memo: str,
    currency: str = "NGN",
    payment_method_id=None,
):
    payment = Payment(
        account_id=account_id,
        amount=Decimal(amount),
        currency=currency,
        status=status,
        memo=memo,
        payment_method_id=payment_method_id,
        created_at=created_at,
    )
    db_session.add(payment)
    db_session.commit()
    db_session.refresh(payment)
    return payment


def test_build_payments_list_data_filters_by_status_and_method(db_session, subscriber):
    card = _create_payment_method(db_session, subscriber.id, PaymentMethodType.card)
    cash = _create_payment_method(db_session, subscriber.id, PaymentMethodType.cash)
    now = datetime.now(UTC)

    _create_payment(
        db_session,
        account_id=subscriber.id,
        amount="100",
        status=PaymentStatus.succeeded,
        created_at=now,
        memo="card payment",
        payment_method_id=card.id,
    )
    _create_payment(
        db_session,
        account_id=subscriber.id,
        amount="50",
        status=PaymentStatus.pending,
        created_at=now,
        memo="cash pending",
        payment_method_id=cash.id,
    )

    result = build_payments_list_data(
        db_session,
        page=1,
        per_page=25,
        customer_ref=None,
        status="succeeded",
        method="card",
        search=None,
        start_date=None,
        end_date=None,
    )

    assert result["total"] == 1
    assert len(result["payments"]) == 1
    assert result["payments"][0].display_method == "Card"
    payment = result["payments"][0]
    assert result["payment_status_presentations"][str(payment.id)].model_dump(
        mode="json"
    ) == {
        "value": "succeeded",
        "label": "Succeeded",
        "tone": "positive",
        "icon": "check",
    }
    assert result["payment_status_options"] == [
        {"value": "pending", "label": "Pending"},
        {"value": "succeeded", "label": "Succeeded"},
        {"value": "failed", "label": "Failed"},
        {"value": "refunded", "label": "Refunded"},
        {"value": "partially_refunded", "label": "Partially refunded"},
        {"value": "reversed", "label": "Reversed"},
        {"value": "canceled", "label": "Canceled"},
    ]


def test_empty_payment_totals_use_display_owner_default_currency(
    db_session, monkeypatch
):
    monkeypatch.setattr(display_format, "default_currency", lambda _db: "USD")

    result = build_payments_list_data(
        db_session,
        page=1,
        per_page=25,
        customer_ref=None,
        partner_id=None,
        status=None,
        method=None,
        search=None,
        start_date=None,
        end_date=None,
    )

    assert result["status_totals"]["all"]["display"] == "USD 0.00"
    assert result["status_totals"]["pending"]["display"] == "USD 0.00"


def test_build_payments_list_data_search_and_inclusive_dates(db_session, subscriber):
    method = _create_payment_method(
        db_session, subscriber.id, PaymentMethodType.transfer
    )
    now = datetime.now(UTC)
    _create_payment(
        db_session,
        account_id=subscriber.id,
        amount="120",
        status=PaymentStatus.succeeded,
        created_at=now - timedelta(days=2),
        memo="NIP/WEMA/BANK/ENE CONNECTIVITY",
        payment_method_id=method.id,
    )
    _create_payment(
        db_session,
        account_id=subscriber.id,
        amount="80",
        status=PaymentStatus.succeeded,
        created_at=now - timedelta(days=50),
        memo="old transfer",
        payment_method_id=method.id,
    )

    result = build_payments_list_data(
        db_session,
        page=1,
        per_page=25,
        customer_ref=None,
        status=None,
        method=None,
        search="WEMA",
        start_date=(now - timedelta(days=7)).date(),
        end_date=now.date(),
    )

    assert result["total"] == 1
    payment = result["payments"][0]
    assert "Bank" in payment.display_number
    assert "WEMA" in payment.narration


def test_payment_end_date_includes_the_whole_utc_calendar_day(db_session, subscriber):
    method = _create_payment_method(
        db_session, subscriber.id, PaymentMethodType.transfer
    )
    _create_payment(
        db_session,
        account_id=subscriber.id,
        amount="120",
        status=PaymentStatus.succeeded,
        created_at=datetime(2026, 7, 31, 23, 59, 59, 999999, tzinfo=UTC),
        memo="included boundary",
        payment_method_id=method.id,
    )
    _create_payment(
        db_session,
        account_id=subscriber.id,
        amount="80",
        status=PaymentStatus.succeeded,
        created_at=datetime(2026, 8, 1, tzinfo=UTC),
        memo="excluded boundary",
        payment_method_id=method.id,
    )

    list_query = build_payments_list_query(
        start_date=date(2026, 7, 31),
        end_date=date(2026, 7, 31),
    )
    result = build_payments_list_data(
        db_session,
        list_query=list_query,
    )
    export_scope = list_payments_for_scope(db_session, list_query=list_query)

    assert [payment.narration for payment in result["payments"]] == [
        "included boundary"
    ]
    assert [payment.narration for payment in export_scope] == ["included boundary"]


def test_render_payments_csv_contains_narration_and_method(db_session, subscriber):
    method = _create_payment_method(db_session, subscriber.id, PaymentMethodType.cash)
    payment = _create_payment(
        db_session,
        account_id=subscriber.id,
        amount="30",
        status=PaymentStatus.succeeded,
        created_at=datetime.now(UTC),
        memo="cash till",
        payment_method_id=method.id,
    )
    payment.display_number = "Cash 123"  # type: ignore[attr-defined]
    payment.display_method = "Cash"  # type: ignore[attr-defined]
    payment.narration = "cash till"  # type: ignore[attr-defined]

    rows = list(csv.reader(io.StringIO(render_payments_csv([payment]))))

    assert rows[0] == [
        "payment_id",
        "display_number",
        "customer_name",
        "amount",
        "currency",
        "status",
        "method",
        "narration",
        "paid_at",
        "created_at",
    ]
    assert rows[1][1:8] == [
        "Cash 123",
        "Test User",
        "30.00",
        "NGN",
        "succeeded",
        "Cash",
        "cash till",
    ]
    assert str(subscriber.id) not in rows[1]


def test_render_payments_csv_uses_business_customer_name_and_csv_escaping(
    db_session, subscriber
):
    subscriber.company_name = "Dotmac, Łódź"
    subscriber.category = SubscriberCategory.business
    db_session.commit()
    payment = _create_payment(
        db_session,
        account_id=subscriber.id,
        amount="75.50",
        status=PaymentStatus.succeeded,
        created_at=datetime.now(UTC),
        memo="business payment",
    )

    rows = list(csv.reader(io.StringIO(render_payments_csv([payment]))))

    assert rows[1][2] == "Dotmac, Łódź"
    assert len(rows[1]) == len(rows[0])


def test_stream_payments_csv_matches_rendered_and_yields_incrementally(
    db_session, subscriber
):
    method = _create_payment_method(db_session, subscriber.id, PaymentMethodType.cash)
    now = datetime.now(UTC)
    for idx in range(3):
        _create_payment(
            db_session,
            account_id=subscriber.id,
            amount=str(30 + idx),
            status=PaymentStatus.succeeded,
            created_at=now - timedelta(minutes=idx),
            memo=f"stream payment {idx}",
            payment_method_id=method.id,
        )

    list_query = build_payments_list_query(search="stream payment")
    state = build_payments_list_data(
        db_session,
        page=1,
        per_page=25,
        customer_ref=None,
        search="stream payment",
    )
    scope = state["payments"]
    assert isinstance(scope, list)
    expected = render_payments_csv(scope)

    statements: list[str] = []

    def _record_statement(_conn, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    bind = db_session.get_bind()
    db_session.expire_all()
    event.listen(bind, "before_cursor_execute", _record_statement)
    try:
        chunks = list(stream_payments_csv(db_session, list_query=list_query))
    finally:
        event.remove(bind, "before_cursor_execute", _record_statement)

    assert "".join(chunks) == expected
    assert len(chunks) == len(scope) + 1
    assert chunks[0].startswith("payment_id,")
    assert all("stream payment" in chunk for chunk in chunks[1:])
    assert all("Test User" in chunk for chunk in chunks[1:])
    assert str(subscriber.id) not in "".join(chunks)
    assert (
        sum(statement.lstrip().upper().startswith("SELECT") for statement in statements)
        == 1
    )


def test_build_payments_list_data_unallocated_only(db_session, subscriber):
    method = _create_payment_method(
        db_session, subscriber.id, PaymentMethodType.transfer
    )
    invoice = Invoice(
        account_id=subscriber.id,
        status=InvoiceStatus.issued,
        subtotal=Decimal("100.00"),
        tax_total=Decimal("0.00"),
        total=Decimal("100.00"),
        balance_due=Decimal("100.00"),
    )
    db_session.add(invoice)
    db_session.commit()

    allocated = _create_payment(
        db_session,
        account_id=subscriber.id,
        amount="100",
        status=PaymentStatus.succeeded,
        created_at=datetime.now(UTC),
        memo="allocated",
        payment_method_id=method.id,
    )
    db_session.add(
        PaymentAllocation(
            payment_id=allocated.id,
            invoice_id=invoice.id,
            amount=Decimal("100.00"),
        )
    )
    db_session.commit()

    _create_payment(
        db_session,
        account_id=subscriber.id,
        amount="55",
        status=PaymentStatus.succeeded,
        created_at=datetime.now(UTC),
        memo="unallocated",
        payment_method_id=method.id,
    )

    result = build_payments_list_data(
        db_session,
        page=1,
        per_page=25,
        customer_ref=None,
        status=None,
        method=None,
        search=None,
        start_date=None,
        end_date=None,
        unallocated_only=True,
    )

    assert result["total"] == 1
    assert result["payments"][0].memo == "unallocated"


def test_build_payments_list_data_filters_by_partner(db_session):
    reseller_a = Reseller(name="Partner A")
    reseller_b = Reseller(name="Partner B")
    db_session.add_all([reseller_a, reseller_b])
    db_session.commit()

    account_a = Subscriber(
        first_name="Pay",
        last_name="A",
        email="pay-a@example.com",
        reseller_id=reseller_a.id,
    )
    account_b = Subscriber(
        first_name="Pay",
        last_name="B",
        email="pay-b@example.com",
        reseller_id=reseller_b.id,
    )
    db_session.add_all([account_a, account_b])
    db_session.commit()

    _create_payment(
        db_session,
        account_id=account_a.id,
        amount="100",
        status=PaymentStatus.succeeded,
        created_at=datetime.now(UTC),
        memo="partner a payment",
    )
    _create_payment(
        db_session,
        account_id=account_b.id,
        amount="70",
        status=PaymentStatus.succeeded,
        created_at=datetime.now(UTC),
        memo="partner b payment",
    )

    result = build_payments_list_data(
        db_session,
        page=1,
        per_page=25,
        customer_ref=None,
        partner_id=str(reseller_a.id),
        status=None,
        method=None,
        search=None,
        start_date=None,
        end_date=None,
    )

    assert result["total"] == 1
    assert len(result["payments"]) == 1
    assert result["payments"][0].memo == "partner a payment"
    assert result["selected_partner_id"] == str(reseller_a.id)


def test_build_payments_list_data_includes_status_totals_for_filtered_set(
    db_session, subscriber
):
    now = datetime.now(UTC)
    _create_payment(
        db_session,
        account_id=subscriber.id,
        amount="10",
        status=PaymentStatus.succeeded,
        created_at=now,
        memo="ok",
    )
    _create_payment(
        db_session,
        account_id=subscriber.id,
        amount="20",
        status=PaymentStatus.pending,
        created_at=now,
        memo="wait",
    )

    result = build_payments_list_data(
        db_session,
        page=1,
        per_page=10,
        customer_ref=None,
        partner_id=None,
        status=None,
        method=None,
        search=None,
        start_date=None,
        end_date=None,
    )

    assert result["total"] == 2
    assert result["status_totals"]["succeeded"]["count"] == 1
    assert result["status_totals"]["pending"]["count"] == 1
    assert result["status_totals"]["all"]["count"] == 2
    assert result["status_totals"]["all"]["amount"] == 30.0


def test_build_payments_list_data_groups_status_totals_by_currency(
    db_session, subscriber
):
    now = datetime.now(UTC)
    _create_payment(
        db_session,
        account_id=subscriber.id,
        amount="10",
        status=PaymentStatus.succeeded,
        created_at=now,
        memo="ngn",
        currency="NGN",
    )
    _create_payment(
        db_session,
        account_id=subscriber.id,
        amount="20",
        status=PaymentStatus.succeeded,
        created_at=now,
        memo="usd",
        currency="USD",
    )

    result = build_payments_list_data(
        db_session,
        page=1,
        per_page=25,
        customer_ref=None,
        partner_id=None,
        status=None,
        method=None,
        search=None,
        start_date=None,
        end_date=None,
    )

    succeeded = result["status_totals"]["succeeded"]
    assert succeeded["count"] == 2
    assert succeeded["amounts"] == {"NGN": Decimal("10.00"), "USD": Decimal("20.00")}
    assert succeeded["display"] == "NGN 10.00, USD 20.00"


# --- Payments list-projection contract (ui.payments_list_projection) ---


def test_payments_list_definition_declares_expected_capabilities():
    definition = PAYMENTS_LIST_DEFINITION
    assert definition.filterable_keys == (
        "customer_ref",
        "partner_id",
        "status",
        "method",
        "start_date",
        "end_date",
        "unallocated_only",
    )
    assert definition.sortable_keys == ("created_at",)
    assert definition.default_sort == "created_at"
    assert definition.default_sort_dir == "desc"
    assert definition.default_per_page == 25


def test_build_payments_list_query_normalizes_filters_and_flag():
    query = build_payments_list_query(
        status="succeeded",
        customer_ref=" ",
        start_date=date(2026, 7, 1),
        end_date=date(2026, 7, 31),
        unallocated_only=True,
        page=3,
    )
    assert query.filter_value("status") == "succeeded"
    assert query.filter_value("customer_ref") is None  # blank dropped
    assert query.filter_value("start_date") == "2026-07-01"
    assert query.filter_value("end_date") == "2026-07-31"
    assert query.filter_value("unallocated_only") == "true"
    assert query.sort_by == "created_at"
    assert query.sort_dir == "desc"
    assert query.page == 3
    assert query.per_page == 25


def test_build_payments_list_query_rejects_out_of_contract_params():
    with pytest.raises(ValueError):
        build_payments_list_query(sort_by="amount")
    with pytest.raises(ValueError):
        build_payments_list_query(per_page=30)
    with pytest.raises(
        InclusiveDateRangeError,
        match="start_date must be before or equal to end_date",
    ):
        build_payments_list_query(
            start_date=date(2026, 8, 2),
            end_date=date(2026, 8, 1),
        )


def test_build_payments_list_data_respects_sort_dir(db_session, subscriber):
    card = _create_payment_method(db_session, subscriber.id, PaymentMethodType.card)
    older = datetime.now(UTC) - timedelta(days=2)
    newer = datetime.now(UTC)
    _create_payment(
        db_session,
        account_id=subscriber.id,
        amount="10",
        status=PaymentStatus.succeeded,
        created_at=older,
        memo="older",
        payment_method_id=card.id,
    )
    _create_payment(
        db_session,
        account_id=subscriber.id,
        amount="20",
        status=PaymentStatus.succeeded,
        created_at=newer,
        memo="newer",
        payment_method_id=card.id,
    )

    desc = build_payments_list_data(
        db_session,
        page=1,
        per_page=25,
        customer_ref=None,
        sort_dir="desc",
    )
    asc = build_payments_list_data(
        db_session,
        page=1,
        per_page=25,
        customer_ref=None,
        sort_dir="asc",
    )

    assert [p.narration for p in desc["payments"]] == ["newer", "older"]
    assert [p.narration for p in asc["payments"]] == ["older", "newer"]
