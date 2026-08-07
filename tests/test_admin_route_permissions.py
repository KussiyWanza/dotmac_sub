from pathlib import Path

from fastapi.routing import APIRoute

from app.api import catalog as api_catalog
from app.api import network_device_groups as api_network_device_groups
from app.api import support as api_support
from app.web.admin import admin_hub as admin_system_hub
from app.web.admin import catalog as admin_catalog
from app.web.admin import catalog_settings as admin_catalog_settings
from app.web.admin import configuration as admin_configuration
from app.web.admin import dashboard as admin_dashboard
from app.web.admin import design_system as admin_design_system
from app.web.admin import dispatch_work_orders as admin_dispatch_work_orders
from app.web.admin import gis as admin_gis
from app.web.admin import inbox as admin_inbox
from app.web.admin import integrations as admin_integrations
from app.web.admin import legal as admin_legal
from app.web.admin import network_device_groups as admin_network_device_groups
from app.web.admin import network_olts_profiles as admin_network_olts_profiles
from app.web.admin import notifications as admin_notifications
from app.web.admin import reports as admin_reports
from app.web.admin import resellers as admin_resellers
from app.web.admin import support_assignment_rules as admin_support_assignment_rules
from app.web.admin import support_automation as admin_support_automation
from app.web.admin import support_tickets as admin_support_tickets
from app.web.admin import system as admin_system
from app.web.admin import system_whats_new as admin_system_whats_new
from app.web.admin import usage as admin_usage


def _get_route(module_router, path: str, method: str) -> APIRoute:
    for route in module_router.routes:
        if (
            isinstance(route, APIRoute)
            and route.path == path
            and method in route.methods
        ):
            return route
    raise AssertionError(f"Route not found: {method} {path}")


def _contains_value(value, expected: str) -> bool:
    if isinstance(value, str):
        return value == expected
    if isinstance(value, (tuple, list, set)):
        return any(_contains_value(item, expected) for item in value)
    if isinstance(value, dict):
        return any(_contains_value(item, expected) for item in value.values())
    return False


def _route_has_permission(module_router, path: str, method: str, expected: str) -> bool:
    route = _get_route(module_router, path, method)
    for dependency in route.dependant.dependencies:
        call = dependency.call
        closure = getattr(call, "__closure__", None) or ()
        for cell in closure:
            if _contains_value(cell.cell_contents, expected):
                return True
    return False


def test_catalog_routes_require_catalog_permissions():
    assert _route_has_permission(
        admin_catalog.router, "/catalog", "GET", "catalog:read"
    )
    assert _route_has_permission(
        admin_catalog.router, "/catalog/offers", "POST", "catalog:write"
    )
    assert _route_has_permission(
        admin_catalog.router,
        "/catalog/offers",
        "POST",
        "catalog:billing_write",
    )
    for path, method in (
        ("/catalog/subscriptions/service-access/available-ipv4", "GET"),
        ("/catalog/subscriptions/{subscription_id}/access/move", "GET"),
        ("/catalog/subscriptions/{subscription_id}/access/move/preview", "POST"),
        ("/catalog/subscriptions/{subscription_id}/access/move/confirm", "POST"),
    ):
        assert _route_has_permission(
            admin_catalog.router,
            path,
            method,
            "catalog:write",
        )


def test_billing_catalog_api_mutations_require_narrow_permission():
    for path, method in (
        ("/offers", "POST"),
        ("/offers/{offer_id}", "PATCH"),
        ("/offer-prices", "POST"),
        ("/offer-prices/{price_id}", "PATCH"),
        ("/add-on-prices/{price_id}", "PATCH"),
        ("/offer-versions", "POST"),
        ("/offer-version-prices/{price_id}", "PATCH"),
    ):
        assert _route_has_permission(
            api_catalog.router,
            path,
            method,
            "catalog:billing_write",
        )


def test_dashboard_routes_require_any_domain_read_permission():
    assert _route_has_permission(
        admin_dashboard.router,
        "/dashboard",
        "GET",
        "billing:invoice:read",
    )
    assert _route_has_permission(
        admin_dashboard.router,
        "/dashboard/workers/restart",
        "POST",
        "system:settings:write",
    )


def test_dispatch_work_order_routes_require_operations_dispatch_permission():
    # Granular dispatch RBAC (#1329): read to view, write to mutate, assign to
    # queue — replacing the coarse operations:dispatch guard.
    for path, method, permission in [
        ("/dispatch/work-orders", "GET", "operations:dispatch:read"),
        ("/dispatch/work-orders", "POST", "operations:dispatch:write"),
        ("/dispatch/work-orders/{work_order_id}", "POST", "operations:dispatch:write"),
        (
            "/dispatch/work-orders/{work_order_id}/queue",
            "POST",
            "operations:dispatch:assign",
        ),
    ]:
        assert _route_has_permission(
            admin_dispatch_work_orders.router,
            path,
            method,
            permission,
        )


def test_ticket_work_order_handoff_requires_support_and_dispatch_permissions():
    admin_path = "/support/tickets/{ticket_id}/work-orders"
    api_path = "/support/tickets/{ticket_id}/work-orders"
    for router, path in (
        (admin_support_tickets.router, admin_path),
        (api_support.router, api_path),
    ):
        assert _route_has_permission(router, path, "POST", "support:ticket:update")
        assert _route_has_permission(router, path, "POST", "operations:dispatch:write")


def test_notification_routes_require_granular_notification_permissions():
    # Granular notification RBAC (migration 320): read to view templates/queue/
    # history, write to manage — replacing the coarse system:read/system:write.
    for path, method, permission in [
        ("/notifications/templates", "GET", "notification:read"),
        ("/notifications/queue", "GET", "notification:read"),
        ("/notifications/history", "GET", "notification:read"),
        (
            "/notifications/templates/{template_id}/toggle",
            "POST",
            "notification:write",
        ),
    ]:
        assert _route_has_permission(
            admin_notifications.router,
            path,
            method,
            permission,
        )


def test_catalog_settings_routes_require_catalog_permissions():
    assert _route_has_permission(
        admin_catalog_settings.router,
        "/catalog/settings",
        "GET",
        "catalog:read",
    )
    assert _route_has_permission(
        admin_catalog_settings.router,
        "/catalog/settings/region-zones",
        "POST",
        "catalog:write",
    )
    assert _route_has_permission(
        admin_catalog_settings.router,
        "/catalog/settings/add-ons",
        "POST",
        "catalog:billing_write",
    )


def test_gis_routes_require_granular_map_permissions():
    assert _route_has_permission(admin_gis.router, "/gis", "GET", "gis:map:view")
    # Approving a customer's location-change request is a distinct business
    # decision, gated separately from editing map features.
    for path, method in [
        ("/gis/location-requests/{request_id}/approve", "POST"),
        ("/gis/location-requests/{request_id}/reject", "POST"),
    ]:
        assert _route_has_permission(
            admin_gis.router, path, method, "gis:location_request:review"
        )
    for path, method in [
        ("/gis/locations/new", "GET"),
        ("/gis/locations/new", "POST"),
        ("/gis/locations/{location_id}/edit", "GET"),
        ("/gis/locations/{location_id}/edit", "POST"),
        ("/gis/locations/{location_id}/delete", "POST"),
    ]:
        assert _route_has_permission(
            admin_gis.router, path, method, "gis:location:write"
        )
    for path, method in [
        ("/gis/areas/new", "GET"),
        ("/gis/areas/new", "POST"),
        ("/gis/areas/{area_id}/edit", "GET"),
        ("/gis/areas/{area_id}/edit", "POST"),
        ("/gis/areas/{area_id}/delete", "POST"),
    ]:
        assert _route_has_permission(admin_gis.router, path, method, "gis:area:write")
    for path, method in [
        ("/gis/layers/new", "GET"),
        ("/gis/layers/new", "POST"),
        ("/gis/layers/{layer_id}/edit", "GET"),
        ("/gis/layers/{layer_id}/edit", "POST"),
        ("/gis/layers/{layer_id}/delete", "POST"),
    ]:
        assert _route_has_permission(admin_gis.router, path, method, "gis:layer:write")


def test_profile_sync_task_routes_require_network_permissions():
    assert _route_has_permission(
        admin_network_olts_profiles.router,
        "/network/profile-sync-tasks",
        "GET",
        "network:olt:read",
    )
    assert _route_has_permission(
        admin_network_olts_profiles.router,
        "/network/profile-sync-tasks/{task_id}/approve",
        "POST",
        "network:olt:write",
    )
    assert _route_has_permission(
        admin_network_olts_profiles.router,
        "/network/profile-sync-tasks/{task_id}/cancel",
        "POST",
        "network:olt:write",
    )
    assert _route_has_permission(
        admin_network_olts_profiles.router,
        "/network/profile-sync-tasks/{task_id}/execute",
        "POST",
        "network:olt:write",
    )
    assert _route_has_permission(
        admin_network_olts_profiles.router,
        "/network/profile-sync-tasks/execute-due",
        "POST",
        "network:olt:write",
    )
    assert _route_has_permission(
        admin_network_olts_profiles.router,
        "/network/profile-sync-tasks/{task_id}/retry",
        "POST",
        "network:olt:write",
    )
    assert _route_has_permission(
        admin_network_olts_profiles.router,
        "/network/profile-sync-tasks/drift-check",
        "POST",
        "network:olt:write",
    )


def test_device_group_routes_require_network_permissions():
    assert _route_has_permission(
        admin_network_device_groups.router,
        "/network/device-groups",
        "GET",
        "network:device:read",
    )
    assert _route_has_permission(
        admin_network_device_groups.router,
        "/network/device-groups",
        "POST",
        "network:device:write",
    )
    assert _route_has_permission(
        admin_network_device_groups.router,
        "/network/device-groups/{group_id}",
        "GET",
        "network:device:read",
    )
    assert _route_has_permission(
        admin_network_device_groups.router,
        "/network/device-groups/{group_id}/settings",
        "POST",
        "network:device:write",
    )
    assert _route_has_permission(
        admin_network_device_groups.router,
        "/network/device-groups/{group_id}/archive",
        "POST",
        "network:device:write",
    )
    assert _route_has_permission(
        admin_network_device_groups.router,
        "/network/device-groups/{group_id}/members",
        "POST",
        "network:device:write",
    )
    assert _route_has_permission(
        admin_network_device_groups.router,
        "/network/device-groups/{group_id}/member-candidates",
        "GET",
        "network:device:read",
    )
    assert _route_has_permission(
        admin_network_device_groups.router,
        "/network/device-groups/{group_id}/members/import",
        "POST",
        "network:device:write",
    )
    assert _route_has_permission(
        admin_network_device_groups.router,
        "/network/device-groups/{group_id}/members/import-filter",
        "POST",
        "network:device:write",
    )
    assert _route_has_permission(
        admin_network_device_groups.router,
        "/network/device-groups/{group_id}/actions",
        "POST",
        "network:device:write",
    )
    assert _route_has_permission(
        admin_network_device_groups.router,
        "/network/device-groups/{group_id}/members/{member_id}/remove",
        "POST",
        "network:device:write",
    )


def test_device_group_api_routes_require_network_permissions():
    assert _route_has_permission(
        api_network_device_groups.router,
        "/network/device-groups",
        "GET",
        "network:device:read",
    )
    assert _route_has_permission(
        api_network_device_groups.router,
        "/network/device-groups",
        "POST",
        "network:device:write",
    )
    assert _route_has_permission(
        api_network_device_groups.router,
        "/network/device-groups/{group_id}",
        "GET",
        "network:device:read",
    )
    assert _route_has_permission(
        api_network_device_groups.router,
        "/network/device-groups/{group_id}",
        "PATCH",
        "network:device:write",
    )
    assert _route_has_permission(
        api_network_device_groups.router,
        "/network/device-groups/{group_id}",
        "DELETE",
        "network:device:write",
    )
    assert _route_has_permission(
        api_network_device_groups.router,
        "/network/device-groups/{group_id}/members",
        "POST",
        "network:device:write",
    )
    assert _route_has_permission(
        api_network_device_groups.router,
        "/network/device-groups/{group_id}/members/{member_id}",
        "DELETE",
        "network:device:write",
    )
    assert _route_has_permission(
        api_network_device_groups.router,
        "/network/device-groups/{group_id}/actions",
        "POST",
        "network:device:write",
    )


def test_support_automation_routes_require_automation_permissions():
    assert _route_has_permission(
        admin_support_automation.router,
        "/support/automation",
        "GET",
        "support:automation:read",
    )
    assert _route_has_permission(
        admin_support_automation.router,
        "/support/automation",
        "POST",
        "support:automation:write",
    )
    assert _route_has_permission(
        admin_support_automation.router,
        "/support/automation/{rule_id}/edit",
        "GET",
        "support:automation:write",
    )
    assert _route_has_permission(
        admin_support_automation.router,
        "/support/automation/{rule_id}/toggle",
        "POST",
        "support:automation:write",
    )


def test_support_assignment_rule_routes_require_automation_permissions():
    assert _route_has_permission(
        admin_support_assignment_rules.router,
        "/support/assignment-rules",
        "GET",
        "support:automation:read",
    )
    assert _route_has_permission(
        admin_support_assignment_rules.router,
        "/support/assignment-rules",
        "POST",
        "support:automation:write",
    )
    assert _route_has_permission(
        admin_support_assignment_rules.router,
        "/support/assignment-rules/{rule_id}/edit",
        "GET",
        "support:automation:write",
    )
    assert _route_has_permission(
        admin_support_assignment_rules.router,
        "/support/assignment-rules/{rule_id}/toggle",
        "POST",
        "support:automation:write",
    )


def test_reseller_routes_require_customer_permissions():
    # Granular reseller RBAC (#1334): reseller admin split off customer:read/
    # write onto reseller:read (list) / reseller:write (create/edit).
    assert _route_has_permission(
        admin_resellers.router, "/resellers", "GET", "reseller:read"
    )
    assert _route_has_permission(
        admin_resellers.router, "/resellers", "POST", "reseller:write"
    )


def test_design_system_routes_require_system_read():
    assert _route_has_permission(
        admin_design_system.router,
        "/design-system",
        "GET",
        "system:read",
    )


def test_system_hub_routes_require_system_read():
    assert _route_has_permission(
        admin_system_hub.router,
        "/admin-hub",
        "GET",
        "system:read",
    )
    assert _route_has_permission(
        admin_configuration.router,
        "/configuration",
        "GET",
        "system:read",
    )


def test_whats_new_routes_require_settings_permissions():
    assert _route_has_permission(
        admin_system_whats_new.router,
        "/system/whats-new",
        "GET",
        "system:settings:read",
    )
    for path, method in [
        ("/system/whats-new/new", "GET"),
        ("/system/whats-new/new", "POST"),
        ("/system/whats-new/{item_id}/edit", "GET"),
        ("/system/whats-new/{item_id}/edit", "POST"),
        ("/system/whats-new/{item_id}/status", "POST"),
    ]:
        expected = (
            "system:settings:write" if method == "POST" else "system:settings:read"
        )
        assert _route_has_permission(
            admin_system_whats_new.router, path, method, expected
        )


def test_whats_new_publish_actions_require_confirmation():
    index_template = Path("templates/admin/system/whats_new/index.html").read_text()
    form_template = Path("templates/admin/system/whats_new/form.html").read_text()

    assert "confirmWhatsNewVisibilityChange" in index_template
    assert "confirmWhatsNewVisibilityChange" in form_template
    assert "Publish this slide?" in index_template
    assert "Publish this slide?" in form_template


def test_legal_routes_require_system_permissions():
    assert _route_has_permission(
        admin_legal.router,
        "/legal",
        "GET",
        "system:read",
    )
    for path, method in [
        ("/legal/new", "GET"),
        ("/legal/new", "POST"),
        ("/legal/{document_id}/edit", "GET"),
        ("/legal/{document_id}/edit", "POST"),
        ("/legal/{document_id}/upload", "POST"),
        ("/legal/{document_id}/delete-file", "POST"),
        ("/legal/{document_id}/publish", "POST"),
        ("/legal/{document_id}/unpublish", "POST"),
        ("/legal/{document_id}/delete", "POST"),
    ]:
        assert _route_has_permission(admin_legal.router, path, method, "system:write")
    assert _route_has_permission(
        admin_legal.router,
        "/legal/{document_id}",
        "GET",
        "system:read",
    )


def test_legal_publish_actions_require_confirmation():
    template = Path("templates/admin/system/legal/detail.html").read_text()

    assert "Publish this legal document?" in template
    assert "Unpublish this legal document?" in template


def test_integrations_routes_require_settings_permissions():
    assert _route_has_permission(
        admin_integrations.router,
        "/integrations/connectors",
        "GET",
        "system:settings:read",
    )
    assert _route_has_permission(
        admin_integrations.router,
        "/integrations/installed/{connector_id}/toggle",
        "POST",
        "system:settings:write",
    )
    assert _route_has_permission(
        admin_integrations.router,
        "/integrations/whatsapp/config",
        "POST",
        "system:settings:write",
    )


def test_usage_routes_require_catalog_permissions():
    assert _route_has_permission(
        admin_usage.router,
        "/catalog/usage",
        "GET",
        "catalog:read",
    )
    assert _route_has_permission(
        admin_usage.router,
        "/catalog/usage/charges/{charge_id}/post",
        "POST",
        "catalog:write",
    )
    assert _route_has_permission(
        admin_usage.router,
        "/catalog/usage/rating/run",
        "POST",
        "catalog:write",
    )


def test_report_routes_require_domain_permissions():
    assert _route_has_permission(
        admin_reports.router, "/reports/revenue", "GET", "reports:billing:read"
    )
    assert _route_has_permission(
        admin_reports.router, "/reports/subscribers", "GET", "customer:read"
    )
    assert _route_has_permission(
        admin_reports.router, "/reports/network", "GET", "reports:network:read"
    )
    assert _route_has_permission(
        admin_reports.router,
        "/reports/technician",
        "GET",
        "reports:support:read",
    )
    assert _route_has_permission(
        admin_reports.router,
        "/reports/inbox-performance",
        "GET",
        "reports:support:read",
    )
    assert _route_has_permission(
        admin_reports.router,
        "/reports/inbox-escalations",
        "GET",
        "reports:support:read",
    )
    assert _route_has_permission(
        admin_reports.router,
        "/reports/inbox-escalations/{conversation_id}/action",
        "POST",
        "support:ticket:update",
    )
    assert _route_has_permission(
        admin_reports.router,
        "/reports/inbox-escalations/{conversation_id}/reply",
        "POST",
        "support:ticket:update",
    )


def test_team_inbox_routes_require_support_permissions():
    assert _route_has_permission(
        admin_inbox.router,
        "/inbox",
        "GET",
        "support:ticket:read",
    )
    assert _route_has_permission(
        admin_inbox.router,
        "/inbox/{conversation_id}",
        "GET",
        "support:ticket:read",
    )
    assert _route_has_permission(
        admin_inbox.router,
        "/inbox/{conversation_id}/reply",
        "POST",
        "support:ticket:update",
    )


def test_support_ticket_bulk_routes_require_update_permission():
    assert _route_has_permission(
        admin_support_tickets.router,
        "/support/tickets/bulk/preview",
        "POST",
        "support:ticket:update",
    )
    assert _route_has_permission(
        admin_support_tickets.router,
        "/support/tickets/bulk/update",
        "POST",
        "support:ticket:update",
    )


# --- 2026-06-29 admin-web authz hardening (regression locks) -----------------
# The build-failing arch test (tests/architecture/test_route_permission_guards.py)
# only audits /api/v1; these lock the high-sensitivity ADMIN-WEB routes that were
# found unguarded in the security review.


def test_system_secrets_routes_require_secrets_permissions():
    assert _route_has_permission(
        admin_system.router, "/system/secrets", "GET", "system:secrets:read"
    )
    assert _route_has_permission(
        admin_system.router,
        "/system/secrets/{path:path}/save",
        "POST",
        "system:secrets:write",
    )
    assert _route_has_permission(
        admin_system.router, "/system/secrets/create", "POST", "system:secrets:write"
    )
    assert _route_has_permission(
        admin_system.router,
        "/system/secrets/{path:path}/delete",
        "POST",
        "system:secrets:write",
    )


def test_system_api_key_mutations_require_settings_write():
    assert _route_has_permission(
        admin_system.router, "/system/api-keys", "POST", "system:settings:write"
    )
    assert _route_has_permission(
        admin_system.router,
        "/system/api-keys/{key_id}/revoke",
        "POST",
        "system:settings:write",
    )


def test_system_config_writes_require_settings_write():
    for path in (
        "/system/config/billing",
        "/system/config/direct-bank-transfer",
        "/system/config/radius/push-reject-rules",
    ):
        assert _route_has_permission(
            admin_system.router, path, "POST", "system:settings:write"
        ), path


def test_integrations_connector_lifecycle_require_permissions():
    for path in (
        "/integrations/connectors",
        "/integrations/installed/{connector_id}/uninstall",
        "/integrations/targets",
        "/integrations/jobs",
    ):
        assert _route_has_permission(
            admin_integrations.router, path, "POST", "system:settings:write"
        ), path
    for path in (
        "/integrations/payment-gateways/{provider_type}",
        "/integrations/payment-gateways/{provider_type}/enable",
        "/integrations/payment-gateways/{provider_type}/disable",
    ):
        assert _route_has_permission(
            admin_integrations.router, path, "POST", "system:settings:write"
        ), path


def test_catalog_settings_mutations_require_catalog_write():
    for path in (
        "/catalog/settings/usage-allowances/{allowance_id}/delete",
        "/catalog/settings/add-ons/{addon_id}/edit",
        "/catalog/settings/sla-profiles",
        "/catalog/settings/policy-sets/{policy_id}/delete",
        "/catalog/settings/region-zones/bulk-delete",
    ):
        assert _route_has_permission(
            admin_catalog_settings.router, path, "POST", "catalog:write"
        ), path
