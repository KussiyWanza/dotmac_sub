from datetime import date

from app.web.customer.branding import get_customer_templates


def test_customer_templates_currency_amount_filter_formats_ngn_style() -> None:
    templates = get_customer_templates()
    template = templates.env.from_string("{{ value | currency_amount }}")

    assert template.render(value=440000) == "440,000.00"
    assert template.render(value="440000.5") == "440,000.50"
    assert template.render(value=None) == "0.00"


def test_customer_templates_portal_date_filter_formats_calendar_date() -> None:
    templates = get_customer_templates()
    template = templates.env.from_string(
        "{{ value | portal_date('%b %d, %Y', 'N/A') }}"
    )

    assert template.render(value=date(2026, 7, 26)) == "Jul 26, 2026"
    assert template.render(value=None) == "N/A"
