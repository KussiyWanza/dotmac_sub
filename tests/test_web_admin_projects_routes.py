"""Route registration + permission-guard + template-compile tests for the
admin projects web UI (Phase 3 PR 10)."""

from pathlib import Path

from app.web.admin.projects import router, templates

EXPECTED_ROUTES = {
    ("GET", "/projects"),
    ("POST", "/projects"),
    ("GET", "/projects/export.csv"),
    ("GET", "/projects/new"),
    ("GET", "/projects/tasks"),
    ("POST", "/projects/tasks"),
    ("GET", "/projects/tasks/new"),
    ("GET", "/projects/tasks/{task_ref}"),
    ("GET", "/projects/tasks/{task_ref}/edit"),
    ("POST", "/projects/tasks/{task_ref}/edit"),
    ("POST", "/projects/tasks/{task_ref}/status"),
    ("POST", "/projects/tasks/{task_ref}/comments"),
    ("POST", "/projects/tasks/{task_ref}/delete"),
    ("GET", "/projects/templates"),
    ("POST", "/projects/templates"),
    ("GET", "/projects/templates/new"),
    ("GET", "/projects/templates/{template_id}"),
    ("GET", "/projects/templates/{template_id}/edit"),
    ("POST", "/projects/templates/{template_id}/edit"),
    ("POST", "/projects/templates/{template_id}/delete"),
    ("GET", "/projects/templates/{template_id}/tasks/editor"),
    ("POST", "/projects/templates/{template_id}/tasks/editor"),
    ("GET", "/projects/templates/{template_id}/tasks/new"),
    ("POST", "/projects/templates/{template_id}/tasks"),
    ("GET", "/projects/templates/{template_id}/tasks/{task_id}/edit"),
    ("POST", "/projects/templates/{template_id}/tasks/{task_id}/edit"),
    ("POST", "/projects/templates/{template_id}/tasks/{task_id}/delete"),
    ("GET", "/projects/{project_ref}"),
    ("GET", "/projects/{project_ref}/edit"),
    ("POST", "/projects/{project_ref}/edit"),
    ("POST", "/projects/{project_ref}/status"),
    ("POST", "/projects/{project_ref}/priority"),
    ("POST", "/projects/{project_ref}/comments"),
    ("POST", "/projects/{project_ref}/comments/{comment_id}/edit"),
    ("POST", "/projects/{project_ref}/delete"),
}

EXPECTED_PERMISSIONS = {
    ("GET", "/projects"): "project:read",
    ("POST", "/projects"): "project:create",
    ("GET", "/projects/export.csv"): "project:read",
    ("GET", "/projects/new"): "project:create",
    ("GET", "/projects/tasks"): "project:task:read",
    ("POST", "/projects/tasks"): "project:task:write",
    ("GET", "/projects/tasks/new"): "project:task:write",
    ("GET", "/projects/tasks/{task_ref}"): "project:task:read",
    ("GET", "/projects/tasks/{task_ref}/edit"): "project:task:write",
    ("POST", "/projects/tasks/{task_ref}/edit"): "project:task:write",
    ("POST", "/projects/tasks/{task_ref}/status"): "project:task:write",
    ("POST", "/projects/tasks/{task_ref}/comments"): "project:task:write",
    ("POST", "/projects/tasks/{task_ref}/delete"): "project:task:write",
    ("GET", "/projects/templates"): "project:read",
    ("POST", "/projects/templates"): "project:update",
    ("GET", "/projects/templates/new"): "project:update",
    ("GET", "/projects/templates/{template_id}"): "project:read",
    ("GET", "/projects/templates/{template_id}/edit"): "project:update",
    ("POST", "/projects/templates/{template_id}/edit"): "project:update",
    ("POST", "/projects/templates/{template_id}/delete"): "project:update",
    ("GET", "/projects/templates/{template_id}/tasks/editor"): "project:update",
    ("POST", "/projects/templates/{template_id}/tasks/editor"): "project:update",
    ("GET", "/projects/templates/{template_id}/tasks/new"): "project:update",
    ("POST", "/projects/templates/{template_id}/tasks"): "project:update",
    ("GET", "/projects/templates/{template_id}/tasks/{task_id}/edit"): (
        "project:update"
    ),
    ("POST", "/projects/templates/{template_id}/tasks/{task_id}/edit"): (
        "project:update"
    ),
    ("POST", "/projects/templates/{template_id}/tasks/{task_id}/delete"): (
        "project:update"
    ),
    ("GET", "/projects/{project_ref}"): "project:read",
    ("GET", "/projects/{project_ref}/edit"): "project:update",
    ("POST", "/projects/{project_ref}/edit"): "project:update",
    ("POST", "/projects/{project_ref}/status"): "project:update",
    ("POST", "/projects/{project_ref}/priority"): "project:update",
    ("POST", "/projects/{project_ref}/comments"): "project:update",
    ("POST", "/projects/{project_ref}/comments/{comment_id}/edit"): "project:update",
    ("POST", "/projects/{project_ref}/delete"): "project:delete",
}

PROJECT_TEMPLATES = [
    "admin/projects/index.html",
    "admin/projects/_table.html",
    "admin/projects/_components.html",
    "admin/projects/_filter_builder.html",
    "admin/projects/tasks.html",
    "admin/projects/project_form.html",
    "admin/projects/project_detail.html",
    "admin/projects/project_task_form.html",
    "admin/projects/project_task_detail.html",
    "admin/projects/project_templates.html",
    "admin/projects/project_template_form.html",
    "admin/projects/project_template_detail.html",
    "admin/projects/project_template_task_form.html",
    "admin/projects/project_template_tasks_editor.html",
]


def _routes():
    for route in router.routes:
        for method in route.methods - {"HEAD", "OPTIONS"}:
            yield (method, route.path), route


def test_all_expected_routes_registered():
    assert {key for key, _route in _routes()} == EXPECTED_ROUTES


def test_static_paths_declared_before_dynamic_detail():
    """/projects/tasks and /projects/templates must match before
    /projects/{project_ref} (FastAPI resolves in declaration order)."""
    get_paths = [route.path for route in router.routes if "GET" in route.methods]
    detail_index = get_paths.index("/projects/{project_ref}")
    assert get_paths.index("/projects/tasks") < detail_index
    assert get_paths.index("/projects/templates") < detail_index
    assert get_paths.index("/projects/new") < detail_index
    assert get_paths.index("/projects/export.csv") < detail_index


def _permission_keys(route) -> set[str]:
    keys: set[str] = set()
    for dep in route.dependencies:
        fn = dep.dependency
        for cell in getattr(fn, "__closure__", None) or []:
            if isinstance(cell.cell_contents, str):
                keys.add(cell.cell_contents)
    return keys


def test_every_route_has_expected_permission_guard():
    for key, route in _routes():
        expected = EXPECTED_PERMISSIONS[key]
        assert expected in _permission_keys(route), (
            f"{key} missing permission guard {expected!r}"
        )


def test_permission_keys_are_seeded():
    """§2.4 keys must exist in the RBAC seed (owned by seed_rbac; PR 12 only
    adds the crm:quote/sales_order keys — project keys shipped earlier)."""
    seed = (
        Path(__file__).resolve().parents[1] / "scripts" / "seed" / "seed_rbac.py"
    ).read_text()
    for permission in set(EXPECTED_PERMISSIONS.values()):
        assert f'"{permission}"' in seed, f"{permission} not seeded in RBAC"


def test_router_registered_in_admin_web_package():
    from app.web import admin as admin_web

    paths = {route.path for route in admin_web.router.routes if hasattr(route, "path")}
    # Email deep links from PR 6 land on /admin/projects/{id} — must resolve.
    assert "/admin/projects/{project_ref}" in paths
    assert "/admin/projects" in paths


def test_sidebar_has_projects_entry():
    sidebar = Path("templates/components/navigation/admin_sidebar.html").read_text()
    assert '"/admin/projects"' in sidebar
    assert "'project-tasks': 'projects'" in sidebar


def test_all_project_templates_compile():
    """Jinja compile smoke — get_template() parses and compiles each file."""
    for name in PROJECT_TEMPLATES:
        templates.env.get_template(name)


def test_template_management_actions_are_permission_aware():
    list_template = Path("templates/admin/projects/project_templates.html").read_text()
    detail_template = Path(
        "templates/admin/projects/project_template_detail.html"
    ).read_text()

    assert "{% if can_manage_project_templates %}" in list_template
    assert "{% if can_manage_project_templates %}" in detail_template
