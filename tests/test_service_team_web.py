from pathlib import Path

from fastapi.routing import APIRoute

from app.web.admin import service_teams


def test_service_team_web_exposes_native_capabilities_without_hard_delete():
    routes = {
        (route.path, frozenset(route.methods))
        for route in service_teams.router.routes
        if isinstance(route, APIRoute)
    }

    assert ("/system/service-teams", frozenset({"GET"})) in routes
    assert ("/system/service-teams", frozenset({"POST"})) in routes
    assert ("/system/service-teams/{team_id}", frozenset({"GET"})) in routes
    assert (
        "/system/service-teams/{team_id}/active",
        frozenset({"POST"}),
    ) in routes
    assert (
        "/system/service-teams/{team_id}/members",
        frozenset({"POST"}),
    ) in routes
    assert not any(path.endswith("/delete") for path, _methods in routes)


def test_service_team_forms_are_csrf_protected_and_keep_identity_evidence():
    template_root = Path("templates/admin/system/service_teams")
    form = (template_root / "form.html").read_text(encoding="utf-8")
    detail = (template_root / "detail.html").read_text(encoding="utf-8")
    index = (template_root / "index.html").read_text(encoding="utf-8")

    assert "components/forms/csrf_input.html" in form
    assert 'name="request_id"' in form
    assert 'name="expected_updated_at"' in form
    assert detail.count("components/forms/csrf_input.html") >= 4
    assert 'name="reason"' in detail
    assert "/delete" not in detail
    assert "detail.actions.can_add_member" in detail
    assert "detail.actions.can_edit" in detail
    assert "detail.actions.can_activate or detail.actions.can_deactivate" in detail
    assert "detail.actions.lifecycle_block_reason" in detail
    assert "can(request, service_team_permissions.membership)" in detail
    assert "can(request, service_team_permissions.retire)" in detail
    assert "member.role.value == role.value" in detail
    assert "member.role == role" not in detail
    assert "can(request, service_team_permissions.create)" in index
    assert "Page {{ page }} of {{ total_pages }}" in index
    assert "result.search|urlencode" in index
    assert "Agent roles by region" in index
    assert "role_region_groups" in index
    settings_hub = Path("templates/admin/system/settings_hub.html").read_text(
        encoding="utf-8"
    )
    assert 'link.get("permission")' in settings_hub
    assert "can(request, link.permission)" in settings_hub
    assert "cat.links | map" not in settings_hub
