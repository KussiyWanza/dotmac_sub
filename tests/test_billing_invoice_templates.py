from pathlib import Path


def test_invoice_detail_consumes_server_owned_status_presentation():
    template = Path("templates/admin/billing/invoice_detail.html").read_text()

    assert "status_presentation_badge(invoice_status_presentation)" in template
    assert "status_variant_map" not in template


def test_customer_invoice_pay_button_has_light_and_dark_mode_contrast():
    template = Path("templates/customer/billing/invoice.html").read_text()
    pay_button = template.split(
        'href="/portal/billing/pay?invoice={{ invoice.id }}"',
        maxsplit=1,
    )[1].split("</a>", maxsplit=1)[0]

    assert "from-[var(--color-brand-500)]" in pay_button
    assert "to-[var(--color-brand-600)]" in pay_button
    assert "text-white" in pay_button
    assert "border border-[var(--color-brand-700)]" in pay_button
    assert "dark:border-[var(--color-brand-400)]" in pay_button
    assert "dark:text-white" in pay_button
    assert "from-brand-500" not in pay_button
    assert "to-brand-600" not in pay_button


def test_invoice_detail_shows_authoritative_paid_date_only_when_available():
    template = Path("templates/admin/billing/invoice_detail.html").read_text()

    assert "{% if invoice.paid_at %}" in template
    assert "Paid:" in template
    assert (
        "invoice.paid_at | app_datetime('%b %d, %Y', 'N/A', include_tz=False)"
        in template
    )


def test_prepaid_draft_reconciliation_uses_owner_preview_and_shared_confirmation():
    detail = Path("templates/admin/billing/invoice_detail.html").read_text()
    confirmation = Path(
        "templates/admin/billing/prepaid_pay_now_confirm.html"
    ).read_text()

    assert "prepaid_draft_reconciliation_preview" in detail
    assert "prepaid-draft-reconciliation/preview" in detail
    assert "prepaid_recovery_settlement" not in detail
    assert "action_form(review.action_form)" in confirmation
    assert "preview.payment_backed_credit" in confirmation
    assert "preview.opening_funding_required" in confirmation


def test_invoice_edit_form_locks_currency_while_submitting_existing_value():
    template = Path("templates/admin/billing/invoice_form.html").read_text()

    assert (
        '<input type="hidden" name="currency" value="{{ invoice_currency }}">'
        in template
    )
    assert "name=\"{{ 'currency_display' if invoice else 'currency' }}\"" in template
    assert '{% if invoice %}disabled aria-disabled="true"{% endif %}' in template


def test_invoice_batch_uses_server_review_and_shared_confirmation():
    template = Path("templates/admin/billing/invoice_batch.html").read_text()

    assert "/admin/billing/invoices/generate-batch/preview" in template
    assert "action_form(batch_action_form)" in template
    assert "batch_preview.subscriptions" in template
    assert "confirm(" not in template


def test_billing_money_templates_render_currency_codes_not_naira_glyphs():
    template_paths = [
        "templates/admin/billing/invoice_detail.html",
        "templates/admin/billing/_invoices_table.html",
        "templates/admin/billing/ledger.html",
        "templates/admin/billing/ar_aging.html",
    ]

    for template_path in template_paths:
        template = Path(template_path).read_text()
        assert "₦" not in template
        assert "currency" in template or "_display" in template
