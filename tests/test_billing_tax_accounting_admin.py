from decimal import Decimal
from unittest.mock import patch

from starlette.requests import Request

from app.models.payment_proof import WithholdingTaxRecord, WithholdingTaxStatus
from app.models.subscriber import Reseller, Subscriber
from app.services import billing as billing_service


def _request(path: str) -> Request:
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "query_string": b"",
            "headers": [],
        }
    )
    request.state.actor_id = "finance-admin"
    return request


def test_tax_accounting_operator_console_renders_owned_state(db_session) -> None:
    reseller = Reseller(
        name="Tax Console Reseller",
        contact_email="tax-console@example.com",
    )
    db_session.add(reseller)
    db_session.commit()
    billing_account = billing_service.billing_accounts.get_for_reseller(
        db_session,
        str(reseller.id),
    )
    db_session.add(
        WithholdingTaxRecord(
            billing_account_id=billing_account.id,
            reseller_id=reseller.id,
            gross_amount=Decimal("100000.00"),
            net_amount=Decimal("95000.00"),
            wht_amount=Decimal("5000.00"),
            wht_rate=Decimal("5.00"),
            currency="NGN",
            status=WithholdingTaxStatus.pending,
        )
    )
    db_session.commit()

    from app.web.admin.billing_reporting import billing_tax_accounting

    with (
        patch("app.web.admin.get_current_user", return_value={"id": "admin"}),
        patch("app.web.admin.get_sidebar_stats", return_value={}),
    ):
        response = billing_tax_accounting(
            request=_request("/admin/billing/tax-accounting"),
            date_from=None,
            date_to=None,
            wht_status=None,
            wht_search=None,
            wht_page=1,
            error=None,
            message=None,
            db=db_session,
        )

    text = response.body.decode()
    assert response.status_code == 200
    assert "Tax Source Register" in text
    assert "Dotmac ERP is authoritative" in text
    assert "TaxCode account mappings" in text
    assert "ENABLE TAX SHADOW" not in text
    assert "Certificate reference" in text
    assert "Reconciliation and parity" not in text
    assert "Pending certificate" in text
    assert "Tax Console Reseller" in text
    assert "Page 1 of 1" in text


def test_tax_accounting_operator_console_renders_direct_customer_identity(
    db_session,
) -> None:
    subscriber = Subscriber(
        first_name="Helping",
        last_name="Hands",
        company_name="Helping Hands NGO",
        display_name="Helping Hands NGO",
        email="ngo@example.com",
        # A business subscriber so Subscriber.name resolves to the company name
        # the console renders for the direct-customer WHT identity.
        metadata_={"subscriber_category": "business"},
    )
    db_session.add(subscriber)
    db_session.flush()
    db_session.add(
        WithholdingTaxRecord(
            account_id=subscriber.id,
            billing_account_id=None,
            reseller_id=None,
            gross_amount=Decimal("107500.00"),
            net_amount=Decimal("102500.00"),
            wht_amount=Decimal("5000.00"),
            wht_rate=Decimal("5.00"),
            vat_exclusive_amount=Decimal("100000.00"),
            vat_amount=Decimal("7500.00"),
            currency="NGN",
            status=WithholdingTaxStatus.pending,
        )
    )
    db_session.commit()

    from app.web.admin.billing_reporting import billing_tax_accounting

    with (
        patch("app.web.admin.get_current_user", return_value={"id": "admin"}),
        patch("app.web.admin.get_sidebar_stats", return_value={}),
    ):
        response = billing_tax_accounting(
            request=_request("/admin/billing/tax-accounting"),
            date_from=None,
            date_to=None,
            wht_status=None,
            wht_search=None,
            wht_page=1,
            error=None,
            message=None,
            db=db_session,
        )

    text = response.body.decode()
    assert response.status_code == 200
    assert "Helping Hands NGO" in text
    assert str(subscriber.id) in text
