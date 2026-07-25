from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_payment_proof_template_only_renders_declared_action_forms() -> None:
    template = _read("templates/admin/billing/payment_proof_detail.html")

    assert 'components/forms/action_form.html" import action_form' in template
    assert "{% for review_action in review_actions %}" in template
    assert "{{ action_form(review_action) }}" in template
    assert "if st == 'submitted'" not in template
    assert 'name="amount"' not in template
    assert 'name="auto_allocate"' not in template
    assert "return confirm('" not in template


def test_shared_renderer_exposes_accessible_contract_semantics() -> None:
    template = _read("templates/components/forms/action_form.html")
    design_system = _read("static/css/design-system.css")
    base = _read("templates/base.html")

    for marker in (
        "aria-labelledby",
        "aria-describedby",
        "aria-invalid",
        'aria-live="assertive"',
        'role="alert"',
        'name="confirmed"',
        'value="yes"',
        'aria-required="true"',
        "{% for hidden in form.hidden_values %}",
        "{% if not form.allowed %}disabled",
        'include "components/forms/csrf_input.html"',
    ):
        assert marker in template
    assert "window.confirm" not in template
    assert "data-confirm-message" not in template
    assert "form.tone.value" in template
    assert "bg-emerald" not in template
    assert "bg-rose" not in template
    assert ".action-form-submit" in design_system
    assert "background: var(--status-indicator)" in design_system
    assert "/static/css/design-system.css?v=20260714a" in base


def test_payment_proof_projection_delegates_eligibility_to_command_owner() -> None:
    web_projection = _read("app/services/web_billing_payment_proofs.py")
    command_owner = _read("app/services/payment_proofs.py")
    route = _read("app/web/admin/billing_payment_proofs.py")
    template = _read("templates/admin/billing/payment_proof_detail.html")

    assert "payment_proofs_service.review_eligibility(" in web_projection
    assert "class ReviewerIdentityProjection" in web_projection
    assert "_reviewer_identity(" in web_projection
    assert "class PaymentProofReviewEligibility" in command_owner
    assert "class PaymentProofReviewError" in command_owner
    assert 'has_permission(auth, db, "billing:proof:verify")' in route
    assert "PaymentProofStatus" not in route
    assert "proof.verified_by" not in template
    assert "reviewer.display_name" in template


def test_checked_in_sources_name_action_form_owner_and_migration() -> None:
    registry = _read("app/services/sot_relationships.py")
    relationships = _read("docs/SOT_RELATIONSHIP_MAP.md")
    frontend = _read("docs/FRONTEND_SPEC.md")

    assert 'name="ui.action_form_contracts"' in registry
    assert 'name="ui.payment_proof_review_projection"' in registry
    assert "## UI Action Forms" in relationships
    assert "Old owner: payment-proof detail Jinja" in relationships
    assert "### Server-owned action forms" in frontend


def test_payment_arrangement_template_only_renders_projected_safe_actions() -> None:
    template = _read("templates/admin/billing/payment_arrangement_detail.html")
    projection = _read("app/services/web_billing_arrangements.py")
    command = _read("app/services/payment_arrangement_staff_actions.py")

    assert "{{ action_form(arrangement_action) }}" in template
    assert "arrangement.status" not in template
    assert "onsubmit=" not in template
    assert "onclick=" not in template
    assert '"{:,.2f}".format' not in template
    assert "available_staff_action_previews(" in projection
    assert "confirm_staff_action(" in command


def test_dunning_templates_only_render_projected_safe_actions() -> None:
    listing = _read("templates/admin/billing/dunning.html")
    detail = _read("templates/admin/billing/dunning_detail.html")
    confirmation = _read("templates/admin/billing/dunning_bulk_confirm.html")
    projection = _read("app/services/web_billing_dunning.py")
    command = _read("app/services/dunning_staff_actions.py")

    assert "/bulk/{{ bulk_action.key }}/preview" in listing
    assert "{{ action_form(case_action) }}" in detail
    assert "{{ action_form(bulk_action_form) }}" in confirmation
    for template in (listing, detail, confirmation):
        assert "onsubmit=" not in template
        assert "window.confirm" not in template
        assert "return confirm" not in template
    assert "dunning_bulk_action_contract" in projection
    assert "preview_staff_action(" in projection
    assert "confirm_staff_action(" in command
    assert "except Exception" not in projection


def test_invoice_batch_and_bulk_actions_use_server_review_forms() -> None:
    batch = _read("templates/admin/billing/invoice_batch.html")
    history = _read("templates/admin/billing/_invoice_batch_history_table.html")
    invoices = _read("templates/admin/billing/invoices.html")
    bulk_review = _read("templates/admin/billing/invoice_bulk_review.html")
    aging = _read("templates/admin/billing/ar_aging.html")
    batch_projection = _read("app/services/web_billing_invoice_batch.py")

    assert "{{ action_form(batch_action_form) }}" in batch
    assert "{{ action_form(bulk_action_form) }}" in bulk_review
    assert "submitSelectionReview(actionKey)" in invoices
    assert "/bulk/review/send" in aging
    for template in (batch, history, invoices, bulk_review, aging):
        assert "window.confirm" not in template
        assert "return confirm" not in template
        assert "onsubmit=" not in template
    assert "preview_batch_action(" in batch_projection
    assert "confirm_batch_action(" in batch_projection
