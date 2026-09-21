from decimal import Decimal
from pathlib import Path

from fastapi.templating import Jinja2Templates
from sqlalchemy import event

from app.models.billing import Invoice, InvoiceStatus
from app.models.catalog import BillingMode
from app.models.subscriber import Subscriber, SubscriberStatus, UserType
from app.services import web_billing_accounts
from app.services.ui_contracts import StateKind


def test_billing_accounts_list_applies_search_and_status_filters(db_session):
    alpha = Subscriber(
        first_name="Alpha",
        last_name="Customer",
        email="alpha@example.com",
        user_type=UserType.customer,
        subscriber_number="ALPHA-001",
        status=SubscriberStatus.active,
    )
    beta = Subscriber(
        first_name="Beta",
        last_name="Customer",
        email="beta@example.com",
        user_type=UserType.customer,
        subscriber_number="BETA-001",
        status=SubscriberStatus.blocked,
    )
    db_session.add_all([alpha, beta])
    db_session.commit()

    state = web_billing_accounts.build_accounts_list_data(
        db_session,
        page=1,
        per_page=25,
        customer_ref=None,
        search="BETA",
        status="blocked",
    )

    assert [row.account for row in state["accounts"]] == [beta]
    assert "balance" not in beta.__dict__
    assert state["total"] == 1
    assert state["search"] == "BETA"
    assert state["status_filter"] == "blocked"


def test_billing_accounts_list_applies_open_balance_filter(db_session):
    due = Subscriber(
        first_name="Due",
        last_name="Customer",
        email="due@example.com",
        user_type=UserType.customer,
        subscriber_number="DUE-001",
        status=SubscriberStatus.active,
    )
    credit = Subscriber(
        first_name="Credit",
        last_name="Customer",
        email="credit@example.com",
        user_type=UserType.customer,
        subscriber_number="CREDIT-001",
        status=SubscriberStatus.active,
    )
    zero = Subscriber(
        first_name="Zero",
        last_name="Customer",
        email="zero@example.com",
        user_type=UserType.customer,
        subscriber_number="ZERO-001",
        status=SubscriberStatus.active,
    )
    db_session.add_all([due, credit, zero])
    db_session.flush()
    db_session.add_all(
        [
            Invoice(
                account_id=due.id,
                status=InvoiceStatus.issued,
                currency="NGN",
                total=Decimal("125.00"),
                balance_due=Decimal("125.00"),
            ),
            Invoice(
                account_id=due.id,
                status=InvoiceStatus.draft,
                currency="NGN",
                total=Decimal("999.00"),
                balance_due=Decimal("999.00"),
            ),
            Invoice(
                account_id=credit.id,
                status=InvoiceStatus.issued,
                currency="NGN",
                total=Decimal("-25.00"),
                balance_due=Decimal("-25.00"),
            ),
        ]
    )
    db_session.commit()

    positive = web_billing_accounts.build_accounts_list_data(
        db_session,
        page=1,
        per_page=25,
        customer_ref=None,
        balance_filter="positive",
    )
    assert [row.account for row in positive["accounts"]] == [due]
    assert positive["accounts"][0].balance == Decimal("125.00")
    assert "balance" not in due.__dict__
    assert positive["total_balance"] == 125.0

    credit_state = web_billing_accounts.build_accounts_list_data(
        db_session,
        page=1,
        per_page=25,
        customer_ref=None,
        balance_filter="credit",
    )
    assert [row.account for row in credit_state["accounts"]] == [credit]
    assert credit_state["accounts"][0].balance == Decimal("-25.00")
    assert "balance" not in credit.__dict__

    zero_state = web_billing_accounts.build_accounts_list_data(
        db_session,
        page=1,
        per_page=25,
        customer_ref=None,
        balance_filter="zero",
    )
    assert [row.account for row in zero_state["accounts"]] == [zero]
    assert zero_state["balance_filter"] == "zero"
    assert "balance" not in zero.__dict__


def test_billing_accounts_template_uses_normal_get_filters():
    template = Path("templates/admin/billing/accounts.html").read_text()

    assert 'method="get" action="/admin/billing/accounts"' in template
    assert 'hx-target="#accounts-table"' not in template
    assert 'name="balance_filter"' in template
    assert "status_filter" in template


def test_billing_accounts_template_has_no_dead_deactivate_or_delete_actions():
    template = Path("templates/admin/billing/accounts.html").read_text()

    assert 'hx-post="/admin/billing/accounts/' not in template
    assert 'hx-delete="/admin/billing/accounts/' not in template
    assert "/deactivate" not in template
    assert "Delete Account" not in template


def test_billing_account_detail_projects_receivables_separately_from_funding(
    db_session,
):
    account = Subscriber(
        first_name="Postpaid",
        last_name="Customer",
        email="postpaid-account-360@example.com",
        user_type=UserType.customer,
        status=SubscriberStatus.active,
        billing_mode=BillingMode.postpaid,
    )
    db_session.add(account)
    db_session.flush()
    db_session.add(
        Invoice(
            account_id=account.id,
            status=InvoiceStatus.overdue,
            currency="NGN",
            total=Decimal("125.00"),
            balance_due=Decimal("75.00"),
        )
    )
    db_session.commit()

    state = web_billing_accounts.build_account_detail_data(
        db_session, account_id=str(account.id)
    )
    overview = state["account_overview"]

    assert overview.status.label == "Active"
    assert overview.billing_mode.value == "Postpaid"
    assert overview.outstanding_receivables.value == "NGN 75.00"
    assert overview.overdue_receivables.value == "NGN 75.00"
    assert overview.prepaid_funding.kind is StateKind.not_applicable
    assert overview.outstanding_url.endswith(f"account_id={account.id}")


def test_postpaid_billing_account_overview_stays_within_query_budget(
    db_session, subscriber
):
    subscriber.billing_mode = BillingMode.postpaid
    db_session.add(subscriber)
    db_session.commit()
    query_count = 0

    def count_query(*_args, **_kwargs):
        nonlocal query_count
        query_count += 1

    bind = db_session.get_bind()
    event.listen(bind, "before_cursor_execute", count_query)
    try:
        web_billing_accounts.build_account_detail_data(
            db_session, account_id=str(subscriber.id)
        )
    finally:
        event.remove(bind, "before_cursor_execute", count_query)

    assert query_count <= 12


def test_billing_account_detail_template_does_not_invent_a_generic_balance():
    template = Path("templates/admin/billing/account_detail.html").read_text()
    statement_template = Path(
        "templates/admin/billing/_account_statement.html"
    ).read_text()

    assert "account.balance" not in template
    assert '"Balance"' not in template
    assert "account_overview.outstanding_receivables" in template
    assert "account_overview.prepaid_funding" in template
    assert 'hx-trigger="load"' in template
    assert "statement.summaries" in statement_template
    assert "statement.opening_balance" not in statement_template
    assert "statement.closing_balance" not in statement_template


def test_billing_account_detail_does_not_turn_missing_prepaid_authority_into_zero(
    db_session, subscriber, monkeypatch
):
    subscriber.billing_mode = BillingMode.prepaid
    db_session.add(subscriber)
    db_session.commit()

    def unavailable_funding(*_args, **_kwargs):
        raise web_billing_accounts.PrepaidFundingBaselineMissingError(
            "missing reviewed opening position"
        )

    monkeypatch.setattr(
        web_billing_accounts,
        "prepaid_available_balance",
        unavailable_funding,
    )

    state = web_billing_accounts.build_account_detail_data(
        db_session, account_id=str(subscriber.id)
    )

    funding = state["account_overview"].prepaid_funding
    assert funding.kind is StateKind.unavailable
    assert funding.value is None


def test_billing_account_detail_template_compiles():
    env = Jinja2Templates(directory="templates").env
    env.get_template("admin/billing/account_detail.html")
    env.get_template("admin/billing/_account_statement.html")
