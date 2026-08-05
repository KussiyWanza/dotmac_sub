"""Focused quotation payment eligibility and browser-adapter contracts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import Request
from pydantic import ValidationError
from starlette.responses import Response

from app.models.billing import Invoice, InvoiceStatus, TopupIntent
from app.models.sales import Quote, QuoteDepositInvoiceLink, QuoteStatus
from app.schemas.portal import QuotePaymentIntentRequest
from app.services import quote_deposits
from app.services.customer_context import resolve_customer_context
from app.web.customer import quotes as quote_routes


def _quote(db_session, subscriber, **overrides) -> Quote:
    values = {
        "subscriber_id": subscriber.id,
        "status": QuoteStatus.sent.value,
        "currency": "NGN",
        "subtotal": Decimal("100000.00"),
        "tax_total": Decimal("7500.00"),
        "total": Decimal("107500.00"),
        "expires_at": datetime.now(UTC) + timedelta(days=7),
        "metadata_": {"deposit_percent": 50},
        "is_active": True,
    }
    values.update(overrides)
    quote = Quote(**values)
    db_session.add(quote)
    db_session.commit()
    return quote


def _enable_paystack(monkeypatch) -> None:
    monkeypatch.setattr(
        quote_deposits,
        "gateway_options",
        lambda _db: (SimpleNamespace(provider_type=SimpleNamespace(value="paystack")),),
    )


def _query(quote: Quote, subscriber, *, observed_at: datetime | None = None):
    return quote_deposits.QuotePaymentQuery(
        quote_id=quote.id,
        authorized_subscriber_ids=(subscriber.id,),
        observed_at=observed_at or datetime.now(UTC),
    )


def test_quote_payment_get_query_is_side_effect_free_and_server_priced(
    db_session, subscriber, monkeypatch
):
    quote = _quote(db_session, subscriber)
    _enable_paystack(monkeypatch)
    invoice_count = db_session.query(Invoice).count()
    intent_count = db_session.query(TopupIntent).count()

    page = quote_deposits.quote_payment_page(db_session, _query(quote, subscriber))

    assert page.quote_id == quote.id
    assert page.subscriber_id == subscriber.id
    assert page.payable_amount == Decimal("53750.00")
    assert page.currency == "NGN"
    assert page.provider_type == "paystack"
    assert db_session.query(Invoice).count() == invoice_count
    assert db_session.query(TopupIntent).count() == intent_count


def test_quote_payment_query_hides_another_customers_quote(
    db_session, subscriber, monkeypatch
):
    quote = _quote(db_session, subscriber)
    _enable_paystack(monkeypatch)

    with pytest.raises(quote_deposits.QuoteDepositError) as exc_info:
        quote_deposits.quote_payment_page(
            db_session,
            quote_deposits.QuotePaymentQuery(
                quote_id=quote.id,
                authorized_subscriber_ids=(uuid4(),),
                observed_at=datetime.now(UTC),
            ),
        )

    assert exc_info.value.code == "sales.quote_deposits.quote_not_found"


@pytest.mark.parametrize(
    ("changes", "expected_code"),
    (
        ({"status": QuoteStatus.accepted.value}, "status_ineligible"),
        ({"status": QuoteStatus.rejected.value}, "status_ineligible"),
        ({"status": QuoteStatus.expired.value}, "status_ineligible"),
        ({"status": "cancelled"}, "status_ineligible"),
        (
            {"expires_at": datetime.now(UTC) - timedelta(seconds=1)},
            "quote_expired",
        ),
        ({"is_active": False}, "quote_not_found"),
        ({"metadata_": {"deposit_percent": 0}}, "amount_unavailable"),
    ),
)
def test_ineligible_quote_states_fail_closed(
    db_session, subscriber, monkeypatch, changes, expected_code
):
    quote = _quote(db_session, subscriber, **changes)
    _enable_paystack(monkeypatch)

    with pytest.raises(quote_deposits.QuoteDepositError) as exc_info:
        quote_deposits.quote_payment_page(db_session, _query(quote, subscriber))

    assert exc_info.value.code == f"sales.quote_deposits.{expected_code}"


def test_paid_quote_deposit_invoice_fails_closed(db_session, subscriber, monkeypatch):
    quote = _quote(db_session, subscriber)
    _enable_paystack(monkeypatch)
    invoice = Invoice(
        account_id=subscriber.id,
        status=InvoiceStatus.paid,
        currency="NGN",
        subtotal=Decimal("53750.00"),
        total=Decimal("53750.00"),
        balance_due=Decimal("0.00"),
        metadata_={
            "payment_flow": "quote_deposit",
            "quote_id": str(quote.id),
        },
    )
    db_session.add(invoice)
    db_session.flush()
    db_session.add(
        QuoteDepositInvoiceLink(
            quote_id=quote.id,
            invoice_id=invoice.id,
            account_id=subscriber.id,
        )
    )
    db_session.commit()

    with pytest.raises(quote_deposits.QuoteDepositError) as exc_info:
        quote_deposits.quote_payment_page(db_session, _query(quote, subscriber))

    assert exc_info.value.code == "sales.quote_deposits.already_paid"


def test_missing_paystack_route_fails_closed(db_session, subscriber, monkeypatch):
    quote = _quote(db_session, subscriber)
    monkeypatch.setattr(quote_deposits, "gateway_options", lambda _db: ())

    with pytest.raises(quote_deposits.QuoteDepositError) as exc_info:
        quote_deposits.quote_payment_page(db_session, _query(quote, subscriber))

    assert exc_info.value.code == "sales.quote_deposits.paystack_unavailable"


def test_multiple_payable_deposit_invoice_links_fail_closed(db_session, subscriber):
    quote = _quote(db_session, subscriber)
    for amount in (Decimal("53750.00"), Decimal("53749.00")):
        invoice = Invoice(
            account_id=subscriber.id,
            status=InvoiceStatus.issued,
            currency="NGN",
            subtotal=amount,
            total=amount,
            balance_due=amount,
        )
        db_session.add(invoice)
        db_session.flush()
        db_session.add(
            QuoteDepositInvoiceLink(
                quote_id=quote.id,
                invoice_id=invoice.id,
                account_id=subscriber.id,
            )
        )
    db_session.commit()

    with pytest.raises(quote_deposits.QuoteDepositError) as exc_info:
        quote_deposits._existing_payable_deposit_invoice(
            db_session,
            subscriber_id=subscriber.id,
            quote_id=quote.id,
        )

    assert exc_info.value.code == "sales.quote_deposits.invoice_ambiguous"


def test_initiation_replays_pending_server_owned_intent(
    db_session, subscriber, monkeypatch
):
    quote = _quote(db_session, subscriber)
    _enable_paystack(monkeypatch)
    invoice = Invoice(
        account_id=subscriber.id,
        status=InvoiceStatus.issued,
        currency="NGN",
        subtotal=Decimal("53750.00"),
        total=Decimal("53750.00"),
        balance_due=Decimal("53750.00"),
        metadata_={"payment_flow": "quote_deposit", "quote_id": str(quote.id)},
    )
    db_session.add(invoice)
    db_session.flush()
    db_session.add(
        QuoteDepositInvoiceLink(
            quote_id=quote.id,
            invoice_id=invoice.id,
            account_id=subscriber.id,
        )
    )
    provider_id = uuid4()
    binding_id = uuid4()
    intent = TopupIntent(
        account_id=subscriber.id,
        invoice_id=invoice.id,
        provider_id=None,
        capability_binding_id=None,
        reference="DMAC-QUOTE-PENDING",
        provider_type="paystack",
        currency="NGN",
        requested_amount=Decimal("53750.00"),
        status="pending",
        expires_at=datetime.now(UTC) + timedelta(minutes=20),
        metadata_={
            "payment_flow": "invoice_payment",
            "invoice_id": str(invoice.id),
            "invoice_number": invoice.invoice_number or "",
            "account_id": str(subscriber.id),
            "provider_id": str(provider_id),
            "capability_binding_id": str(binding_id),
        },
    )
    db_session.add(intent)
    db_session.commit()
    monkeypatch.setattr(
        quote_deposits,
        "select_checkout_provider",
        lambda *_args, **_kwargs: SimpleNamespace(
            provider_id=provider_id,
            capability_binding_id=binding_id,
        ),
    )
    monkeypatch.setattr(
        quote_deposits.payment_gateway_adapter,
        "build_context",
        lambda *_args, **_kwargs: SimpleNamespace(public_key="pk_test_quote"),
    )
    monkeypatch.setattr(
        quote_deposits,
        "initiate_deposit",
        lambda *_args, **_kwargs: pytest.fail("replay must not create another intent"),
    )
    customer = resolve_customer_context(
        db_session,
        {"subscriber_id": str(subscriber.id), "email": subscriber.email},
    )

    outcome = quote_deposits.initiate_quote_deposit(
        db_session,
        customer,
        quote_deposits.InitiateQuoteDepositCommand(
            quote_id=quote.id,
            idempotency_key="quote-payment-replay-key",
            redirect_url=f"https://selfcare.example.com/portal/quotes/{quote.id}/pay/verify",
        ),
    )

    assert outcome.replayed is True
    assert outcome.invoice_id == invoice.id
    assert outcome.payment_reference == intent.reference
    assert outcome.amount == Decimal("53750.00")
    assert outcome.checkout_metadata.invoice_id == invoice.id


def test_verification_rejects_reference_from_another_quote(
    db_session, subscriber, monkeypatch
):
    target = _quote(db_session, subscriber)
    other = _quote(db_session, subscriber)
    invoice = Invoice(
        account_id=subscriber.id,
        status=InvoiceStatus.issued,
        currency="NGN",
        subtotal=Decimal("53750.00"),
        total=Decimal("53750.00"),
        balance_due=Decimal("53750.00"),
        metadata_={"payment_flow": "quote_deposit", "quote_id": str(other.id)},
    )
    db_session.add(invoice)
    db_session.flush()
    db_session.add(
        QuoteDepositInvoiceLink(
            quote_id=other.id,
            invoice_id=invoice.id,
            account_id=subscriber.id,
        )
    )
    intent = TopupIntent(
        account_id=subscriber.id,
        invoice_id=invoice.id,
        reference="DMAC-QUOTE-WRONG-REFERENCE",
        provider_type="paystack",
        currency="NGN",
        requested_amount=Decimal("53750.00"),
        status="pending",
        expires_at=datetime.now(UTC) + timedelta(minutes=20),
        metadata_={
            "payment_flow": "invoice_payment",
            "invoice_id": str(invoice.id),
            "account_id": str(subscriber.id),
        },
    )
    db_session.add(intent)
    db_session.commit()
    customer = resolve_customer_context(
        db_session,
        {"subscriber_id": str(subscriber.id), "email": subscriber.email},
    )
    monkeypatch.setattr(
        quote_deposits,
        "verify_deposit",
        lambda *_args, **_kwargs: pytest.fail(
            "mismatched reference must not reach verification"
        ),
    )

    with pytest.raises(quote_deposits.QuoteDepositError) as exc_info:
        quote_deposits.verify_quote_deposit(
            db_session,
            customer,
            quote_deposits.VerifyQuoteDepositCommand(
                quote_id=target.id,
                reference=intent.reference,
            ),
        )

    assert exc_info.value.code == "sales.quote_deposits.reference_mismatch"


def _request(path: str, query: str = "") -> Request:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "https",
            "path": path,
            "raw_path": path.encode(),
            "query_string": query.encode(),
            "headers": (),
            "client": ("127.0.0.1", 1234),
            "server": ("selfcare.example.com", 443),
        }
    )


def test_browser_get_preserves_target_when_redirecting_to_login(
    db_session, monkeypatch
):
    quote_id = uuid4()
    monkeypatch.setattr(
        quote_routes, "get_current_customer_from_request", lambda *_args: None
    )

    response = quote_routes.customer_quote_payment(
        _request(f"/portal/quotes/{quote_id}/pay"),
        quote_id,
        db_session,
    )

    assert response.status_code == 303
    assert response.headers["location"] == (
        f"/portal/auth/login?next=%2Fportal%2Fquotes%2F{quote_id}%2Fpay"
    )


def test_browser_get_delegates_only_to_side_effect_free_query(
    db_session, subscriber, monkeypatch
):
    quote_id = uuid4()
    page = quote_deposits.QuotePaymentPage(
        quote_id=quote_id,
        subscriber_id=subscriber.id,
        status=QuoteStatus.sent,
        currency="NGN",
        payable_amount=Decimal("50000.00"),
        expires_at=datetime.now(UTC) + timedelta(days=1),
        provider_type="paystack",
    )
    monkeypatch.setattr(
        quote_routes,
        "get_current_customer_from_request",
        lambda *_args: {
            "subscriber_id": str(subscriber.id),
            "email": subscriber.email,
        },
    )
    monkeypatch.setattr(
        quote_routes.quote_deposits,
        "quote_payment_page",
        lambda *_args, **_kwargs: page,
    )
    monkeypatch.setattr(
        quote_routes.quote_deposits,
        "initiate_quote_deposit",
        lambda *_args, **_kwargs: pytest.fail("GET must not initiate payment"),
    )

    class FakeTemplates:
        @staticmethod
        def TemplateResponse(_name, _context, status_code=200):
            return Response(status_code=status_code)

    monkeypatch.setattr(quote_routes, "templates", FakeTemplates())

    response = quote_routes.customer_quote_payment(
        _request(f"/portal/quotes/{quote_id}/pay"),
        quote_id,
        db_session,
    )

    assert response.status_code == 200


def test_quote_payment_template_composes_server_owned_checkout_contract(subscriber):
    quote_id = uuid4()
    payment = quote_deposits.QuotePaymentPage(
        quote_id=quote_id,
        subscriber_id=subscriber.id,
        status=QuoteStatus.sent,
        currency="NGN",
        payable_amount=Decimal("53750.00"),
        expires_at=datetime.now(UTC) + timedelta(days=1),
        provider_type="paystack",
    )

    html = quote_routes.templates.env.get_template("customer/quotes/pay.html").render(
        request=_request(f"/portal/quotes/{quote_id}/pay"),
        customer={"email": subscriber.email},
        active_page="quotes",
        payment=payment,
    )

    assert "Pay with Paystack" in html
    assert "NGN 53,750.00" in html
    assert f"/portal/quotes/{quote_id}/pay/intent" in html
    assert "body: { idempotency_key: key }" in html
    assert "amount: amountMinor" in html
    assert 'name="amount"' not in html


def test_quote_payment_routes_separate_read_and_mutation_methods():
    route_methods = {
        (route.path, method)
        for route in quote_routes.router.routes
        for method in (route.methods or set())
    }

    assert ("/portal/quotes/{quote_id}/pay", "GET") in route_methods
    assert ("/portal/quotes/{quote_id}/pay/intent", "POST") in route_methods
    assert ("/portal/quotes/{quote_id}/pay/verify", "GET") in route_methods


def test_quote_payment_command_rejects_client_owned_amount_and_provider():
    with pytest.raises(ValidationError):
        QuotePaymentIntentRequest.model_validate(
            {
                "idempotency_key": "quote-payment-validation-key",
                "amount": "1.00",
                "provider": "wallet",
            }
        )
