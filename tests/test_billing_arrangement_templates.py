from pathlib import Path


def test_payment_arrangement_templates_include_currency_with_amounts():
    detail = Path("templates/admin/billing/payment_arrangement_detail.html").read_text()
    listing = Path("templates/admin/billing/payment_arrangements.html").read_text()
    service = Path("app/services/web_billing_arrangements.py").read_text()

    # The detail view renders amounts that the read-owner already formatted with
    # currency (display_format.format_currency_amount), rather than composing the
    # currency inline in the template.
    assert '{{ info_row("Total Amount", arrangement_detail.total_amount) }}' in detail
    assert (
        '{{ info_row("Installment Amount", arrangement_detail.installment_amount) }}'
        in detail
    )
    assert "{{ installment.amount }}" in detail
    assert "format_currency_amount(" in service
    # The listing still composes the currency inline from the invoice.
    assert "arrangement_currency" in listing
    assert (
        '{{ arrangement_currency }} {{ "{:,.2f}".format(arr.total_amount) }}' in listing
    )
