"""Admin navigation must not advertise destinations the principal cannot open."""

from pathlib import Path
from types import SimpleNamespace

from jinja2 import Environment, FileSystemLoader

SIDEBAR = Path("templates/components/navigation/admin_sidebar.html")
ADMIN_LAYOUT = Path("templates/layouts/admin.html")


def _can(request: object, permission: str) -> bool:
    held = request.state.auth["permission_keys"]  # type: ignore[attr-defined]
    return "*" in held or permission in held


def _render_sidebar(*permission_keys: str) -> str:
    environment = Environment(loader=FileSystemLoader("templates"), autoescape=True)
    environment.globals.update(can=_can)
    request = SimpleNamespace(
        state=SimpleNamespace(auth={"permission_keys": frozenset(permission_keys)})
    )
    sidebar_stats = SimpleNamespace(
        module_states={},
        pending_location_requests=0,
        pending_orders=0,
        overdue_invoices=0,
    )
    return environment.get_template("components/navigation/admin_sidebar.html").render(
        request=request, active_page="dashboard", sidebar_stats=sidebar_stats
    )


def test_sidebar_renders_only_destinations_held_by_the_principal() -> None:
    customer_sidebar = _render_sidebar("customer:read")

    assert 'href="/admin/customers"' in customer_sidebar
    assert 'href="/admin/reports"' in customer_sidebar
    assert 'href="/admin/support/tickets"' not in customer_sidebar
    assert 'href="/admin/system"' not in customer_sidebar
    assert 'href="/admin/network"' not in customer_sidebar

    support_sidebar = _render_sidebar("support:ticket:read")
    assert 'href="/admin/support/tickets"' in support_sidebar
    assert 'href="/admin/dashboard"' not in support_sidebar

    admin_sidebar = _render_sidebar("*")
    assert 'href="/admin/customers"' in admin_sidebar
    assert 'href="/admin/system"' in admin_sidebar
    assert 'href="/admin/network"' in admin_sidebar


def test_sidebar_hides_empty_permission_groups() -> None:
    sidebar = _render_sidebar()

    assert ">Core<" not in sidebar
    assert ">Operations<" not in sidebar
    assert ">ADMIN<" in sidebar
    assert 'href="/admin/help"' in sidebar


def test_primary_admin_navigation_declares_route_permission_gates() -> None:
    source = SIDEBAR.read_text(encoding="utf-8")
    expected = {
        "Customers": "customer:read",
        "Support": "support:ticket:read",
        "Workqueue": "support:ticket:read",
        "Inbox": "support:ticket:read",
        "Surveys": "customer:read",
        "Service Requests": "provisioning:read",
        "Projects": "project:read",
        "Billing": "billing_account:read",
        "Catalog": "catalog:read",
        "Network": "network:hub:read",
        "VPN": "network:vpn:read",
        "Work Orders": "operations:dispatch:read",
        "Material Requests": "operations:material_request:read",
        "GIS / Map": "gis:map:view",
        "Integrations": "system:settings:read",
        "Notifications": "notification:read",
        "Provisioning": "provisioning:read",
        "System Overview": "system:settings:read",
        "Settings": "system:settings:read",
    }

    for label, permission in expected.items():
        call = source.split(f'nav_link("{label}"', maxsplit=1)[1].split(
            ") }}", maxsplit=1
        )[0]
        assert f'permission="{permission}"' in call, label

    for label in ("Sales", "Referrals", "Meta connection"):
        call = source.split(f'nav_link("{label}"', maxsplit=1)[1].split(
            ") }}", maxsplit=1
        )[0]
        assert 'permission="' in call, label

    help_call = source.split('nav_link("Help center"', maxsplit=1)[1].split(
        ") }}", maxsplit=1
    )[0]
    assert 'permission="' not in help_call


def test_navigation_macros_fail_closed_and_empty_groups_are_suppressed() -> None:
    source = SIDEBAR.read_text(encoding="utf-8")

    assert "{% if not permission or can(request, permission) %}" in source
    assert '{% if show_core %}{{ section_label("Core") }}{% endif %}' in source
    assert (
        '{% if show_operations %}{{ section_label("Operations") }}{% endif %}' in source
    )
    assert "{% set show_admin = true %}" in source
    assert '{% if show_admin %}{{ section_label("ADMIN") }}{% endif %}' in source
    assert "module_states.get('reports', True) and show_reports" in source


def test_admin_shell_shortcuts_follow_destination_permissions() -> None:
    source = ADMIN_LAYOUT.read_text(encoding="utf-8")

    assert "{% set admin_home_href = '/admin/dashboard' if can(request," in source
    assert "else '/admin/system/users/profile' %}" in source
    assert "{% if can(request, 'system:settings:read') %}" in source
    assert '<a href="/admin/system/settings" role="menuitem"' in source


def test_vendor_navigation_hides_each_destination_by_its_route_permission() -> None:
    source = SIDEBAR.read_text(encoding="utf-8")

    assert (
        'can(request, "inventory:read") or can(request, "network:fiber:read")' in source
    )
    assert (
        'subnav_link("Vendor Records", "/admin/vendors", "vendors", '
        'permission="inventory:read")' in source
    )
    assert 'permission="network:fiber:read")' in source
