"""Public and staff-facing Lead intake surface contracts."""

from pathlib import Path

from app.main import _CSRF_PROTECTED_PATHS


def test_public_form_has_required_privacy_address_and_customer_fields():
    template = Path("templates/public/lead_intake/form.html").read_text(
        encoding="utf-8"
    )
    for field in (
        "full_name",
        "gender",
        "date_of_birth",
        "organization_name",
        "representative_name",
        "representative_role",
        "address_confirmation",
        "privacy_acknowledged",
    ):
        assert f'name="{field}"' in template
    assert 'name="_csrf_token"' in template
    assert '<meta name="referrer" content="no-referrer">' in template
    assert "marketing" not in template.casefold()
    assert "/lead-intake/" in _CSRF_PROTECTED_PATHS


def test_inbox_drawer_exposes_only_permission_gated_issue_and_revoke_actions():
    template = Path("templates/admin/inbox/_contact_drawer.html").read_text(
        encoding="utf-8"
    )
    assert "can_manage_leads" in template
    assert "/lead-intake/issue" in template
    assert "/revoke" in template
    assert "invitation.token" not in template


def test_admin_template_surface_explains_publish_gate_and_immutability():
    index = Path("templates/admin/sales/lead_intake/index.html").read_text(
        encoding="utf-8"
    )
    form = Path("templates/admin/sales/lead_intake/form.html").read_text(
        encoding="utf-8"
    )
    assert "each customer type" in index
    assert "Published versions are immutable" in form
    assert "{link}" in form
