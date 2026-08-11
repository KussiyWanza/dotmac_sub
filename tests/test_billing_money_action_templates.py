from pathlib import Path

from fastapi.templating import Jinja2Templates


def _template(path: str) -> str:
    return Path(path).read_text()


def test_manual_payment_and_credit_forms_confirm_and_bound_amounts():
    payment_form = _template("templates/admin/billing/payment_form.html")
    payment_confirmation = _template(
        "templates/admin/billing/payment_create_confirm.html"
    )
    payment_amount = _template("templates/admin/billing/_payment_amount_field.html")
    credit_form = _template("templates/admin/billing/credit_form.html")
    credit_confirmation = _template("templates/admin/billing/credit_issue_confirm.html")

    assert "Record this payment and allocate it" not in payment_form
    assert 'action="{{ action_url }}"' in payment_form
    assert "button[type=submit]').disabled" not in payment_form
    assert 'name="idempotency_token"' in payment_form
    assert "Confirm and Record" in payment_confirmation
    assert "Prepaid funding position" in payment_confirmation
    assert "Unallocated account credit" in payment_confirmation
    assert 'name="preview_fingerprint"' in payment_confirmation
    assert 'min="0.01"' in payment_amount
    assert 'action="{{ action_url }}"' in credit_form
    assert "Confirm and issue" in credit_confirmation
    assert "Exact ledger result" in credit_confirmation
    assert "Application disposition" in credit_confirmation
    assert "Named invoice result" in credit_confirmation
    assert "preview.application_amount" in credit_confirmation
    assert "preview.residual_credit" in credit_confirmation
    assert "restoration is not inferred from this credit" in credit_confirmation
    assert 'name="preview_fingerprint"' in credit_confirmation
    assert 'name="idempotency_key"' in credit_confirmation
    assert 'min="0.01"' in credit_form


def test_credit_and_collection_forms_seed_currency_from_default_setting_context():
    credit_form = _template("templates/admin/billing/credit_form.html")
    collection_accounts = _template("templates/admin/billing/collection_accounts.html")

    assert 'value="{{ default_currency|default(' in credit_form
    assert 'value="NGN"' not in credit_form
    assert "default_currency|default(" in collection_accounts
    assert 'value="NGN"' not in collection_accounts


def test_dunning_and_routing_actions_confirm_before_state_changes():
    dunning = _template("templates/admin/billing/dunning.html")
    dunning_detail = _template("templates/admin/billing/dunning_detail.html")
    dunning_confirmation = _template(
        "templates/admin/billing/dunning_bulk_confirm.html"
    )
    arrangement = _template("templates/admin/billing/payment_arrangement_detail.html")
    shared_action = _template("templates/components/forms/action_form.html")
    channels = _template("templates/admin/billing/payment_channels.html")
    collection_accounts = _template("templates/admin/billing/collection_accounts.html")

    # Dunning and payment-arrangement actions route through the review/action-form
    # flow (this PR's feature).
    assert "/bulk/{{ bulk_action.key }}/preview" in dunning
    assert "{{ action_form(case_action) }}" in dunning_detail
    assert "{{ action_form(bulk_action_form) }}" in dunning_confirmation
    assert "confirm(" not in dunning
    assert "confirm(" not in dunning_detail
    assert "{{ action_form(arrangement_action) }}" in arrangement
    assert 'name="confirmed"' in shared_action
    assert "window.confirm" not in shared_action
    # Payment-channel and collection-account lifecycle changes route through the
    # server-owned review/confirm flow (landed in #1590): the list action cannot
    # change state directly — it opens /review.
    assert "/payment-configuration/payment_channel/" in channels
    assert "'deactivate' if channel.is_active else 'activate' }}/review" in channels
    assert "/payment-configuration/collection_account/" in collection_accounts
    assert (
        "'deactivate' if account.is_active else 'activate' }}/review"
        in collection_accounts
    )


def test_credit_application_requires_owner_preview_and_exact_confirmation():
    invoice_detail = _template("templates/admin/billing/invoice_detail.html")
    confirmation = _template("templates/admin/billing/credit_apply_confirm.html")

    assert "/apply-credit/preview" in invoice_detail
    assert "option.available_amount" in invoice_detail
    assert "note.total" not in invoice_detail
    assert "Confirm credit application" in confirmation
    assert "Exact ledger transaction" in confirmation
    assert 'name="preview_fingerprint"' in confirmation
    assert 'name="idempotency_key"' in confirmation
    assert "restoration is not inferred from this credit" in confirmation


def test_credit_application_templates_compile():
    env = Jinja2Templates(directory="templates").env

    env.get_template("admin/billing/invoice_detail.html")
    env.get_template("admin/billing/credit_apply_confirm.html")
    env.get_template("admin/billing/credit_issue_confirm.html")
    env.get_template("admin/billing/payment_refund_confirm.html")
    env.get_template("admin/billing/invoice_closure_confirm.html")
    env.get_template("admin/billing/invoice_bulk_void_confirm.html")
    env.get_template("admin/billing/invoice_bulk_review.html")
    env.get_template("admin/billing/invoice_batch.html")


def test_payment_refund_requires_owner_preview_and_confirmation():
    detail = _template("templates/admin/billing/payment_detail.html")
    confirmation = _template("templates/admin/billing/payment_refund_confirm.html")

    assert "/refund/preview" in detail
    assert "refund_capability.allowed" in detail
    assert "Review confirmed refund" in detail
    assert "Confirm completed refund" in confirmation
    assert "Exact evidence and access consequence" in confirmation
    assert 'name="preview_fingerprint"' in confirmation
    assert 'name="idempotency_key"' in confirmation
