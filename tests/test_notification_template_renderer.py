import pytest

from app.services.notification_template_renderer import (
    default_preview_variables,
    render_template_text,
    template_placeholder_names,
    validate_template_activation_text,
    validate_template_text,
)


def test_render_template_text_single_brace_contract():
    rendered = render_template_text(
        "Hello {subscriber_name}. Invoice {invoice_number} due {due_date}.",
        {
            "subscriber_name": "Ada",
            "invoice_number": "INV-1",
            "due_date": "2026-03-01",
        },
    )
    assert rendered == "Hello Ada. Invoice INV-1 due 2026-03-01."


def test_render_template_text_does_not_substitute_double_braces():
    # Double-brace is not the supported syntax; the live renderer only fills
    # single braces, so a {{var}} token must not render cleanly.
    rendered = render_template_text(
        "Hi {{subscriber_name}}", {"subscriber_name": "Ada"}
    )
    assert rendered != "Hi Ada"


def test_render_template_text_keeps_unknown_variables():
    rendered = render_template_text("Hi {known} {unknown}", {"known": "x"})
    assert rendered == "Hi x {unknown}"


def test_default_preview_variables_uses_supported_keys():
    values = default_preview_variables()
    assert "subscriber_name" in values
    assert "amount" in values
    # old, unsupported keys must be gone
    assert "customer_name" not in values
    assert "amount_due" not in values


def test_validate_template_text_rejects_double_brace_and_unknown():
    with pytest.raises(ValueError):
        validate_template_text("x", "Balance {{amount}}")
    with pytest.raises(ValueError):
        validate_template_text("x", "Pay at {payment_link}")


def test_validate_is_context_aware_for_automated_codes():
    # Automated event template: event variables OK, bulk-only var rejected.
    validate_template_text(
        "Invoice {invoice_number}",
        "Hi {subscriber_name}, pay {amount}",
        code="invoice_overdue",
    )
    with pytest.raises(ValueError):
        # {customer_name} is bulk-only; the event sender leaves it literal.
        validate_template_text("Hi {customer_name}", code="invoice_overdue")


def test_validate_is_context_aware_for_bulk_codes():
    # Bulk/custom template: bulk variables OK, event-only var rejected.
    validate_template_text(
        "Hi {customer_name}, login {pppoe_login}",
        code="outage_blast_2026",
    )
    with pytest.raises(ValueError):
        # {amount} is not available to the bulk-message context.
        validate_template_text("You owe {amount}", code="outage_blast_2026")


def test_payment_receipt_contract_accepts_and_reports_receipt_fields():
    subject = "Payment receipt {receipt_number}"
    body = "View or download {receipt_url} after paying {amount}."

    validate_template_text(subject, body, code="payment_received")

    assert template_placeholder_names(subject, body) == frozenset(
        {"amount", "receipt_number", "receipt_url"}
    )
    preview = default_preview_variables()
    assert preview["receipt_number"]
    assert preview["receipt_url"]


def test_active_payment_receipt_requires_receipt_fields_in_the_body():
    with pytest.raises(ValueError, match="require these body fields"):
        validate_template_activation_text(
            subject="Receipt {receipt_number}: {receipt_url}",
            body="Thank you for paying {amount}.",
            code="payment_received",
        )

    validate_template_activation_text(
        subject="Payment receipt {receipt_number}",
        body="Receipt {receipt_number}: {receipt_url}",
        code="payment_received",
    )
