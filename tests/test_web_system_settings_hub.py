from fastapi.routing import APIRoute

from app.services import web_system_settings_hub
from app.web.admin import system as admin_system


def test_settings_hub_includes_branding_link(db_session):
    context = web_system_settings_hub.build_settings_hub_context(db_session)

    system_category = next(
        category for category in context["categories"] if category["id"] == "system"
    )
    branding_link = next(
        link
        for link in system_category["links"]
        if link["url"] == "/admin/system/branding"
    )

    assert branding_link["name"] == "Branding & Assets"


def test_settings_hub_includes_control_plane_link(db_session):
    context = web_system_settings_hub.build_settings_hub_context(db_session)
    system_category = next(
        category for category in context["categories"] if category["id"] == "system"
    )

    link = next(
        item
        for item in system_category["links"]
        if item["url"] == "/admin/system/control-plane"
    )

    assert link["name"] == "Control Plane"


def test_system_router_registers_control_plane_read_route():
    route = next(
        route
        for route in admin_system.router.routes
        if isinstance(route, APIRoute) and route.path == "/system/control-plane"
    )

    assert route.methods == {"GET"}


def test_settings_hub_includes_canonical_email_link(db_session):
    context = web_system_settings_hub.build_settings_hub_context(db_session)

    system_category = next(
        category for category in context["categories"] if category["id"] == "system"
    )
    email_link = next(
        link for link in system_category["links"] if link["name"] == "Email / SMTP"
    )

    assert email_link["url"] == "/admin/system/email"


def test_settings_hub_does_not_include_removed_data_retention_page(db_session):
    context = web_system_settings_hub.build_settings_hub_context(db_session)

    system_category = next(
        category for category in context["categories"] if category["id"] == "system"
    )

    assert all(
        link["url"] != "/admin/system/config/data-retention"
        for link in system_category["links"]
    )


def test_settings_hub_does_not_include_removed_dead_config_pages(db_session):
    context = web_system_settings_hub.build_settings_hub_context(db_session)
    links = [link for category in context["categories"] for link in category["links"]]

    removed_urls = {
        "/admin/system/config/data-retention",
        "/admin/system/config/subscribers",
        "/admin/system/config/ipv6",
        "/admin/system/config/cpe",
        "/admin/system/config/fup",
    }

    assert removed_urls.isdisjoint({link["url"] for link in links})


def test_system_router_does_not_register_removed_dead_config_pages():
    registered_paths = {
        route.path
        for route in admin_system.router.routes
        if isinstance(route, APIRoute)
    }

    assert "/config/data-retention" not in registered_paths
    assert "/config/subscribers" not in registered_paths
    assert "/config/ipv6" not in registered_paths
    assert "/config/cpe" not in registered_paths
    assert "/config/fup" not in registered_paths


def test_settings_hub_categories_can_build_query_links(db_session):
    context = web_system_settings_hub.build_settings_hub_context(db_session)

    category_ids = [category["id"] for category in context["categories"]]

    assert category_ids
    assert all(category_id.strip() for category_id in category_ids)


def test_settings_hub_includes_whats_new_link(db_session):
    context = web_system_settings_hub.build_settings_hub_context(db_session)

    system_category = next(
        category for category in context["categories"] if category["id"] == "system"
    )
    whats_new_link = next(
        link
        for link in system_category["links"]
        if link["url"] == "/admin/system/whats-new"
    )

    assert whats_new_link["name"] == "What's New"


def test_settings_hub_includes_ticket_settings_link(db_session):
    context = web_system_settings_hub.build_settings_hub_context(db_session)

    system_category = next(
        category for category in context["categories"] if category["id"] == "system"
    )
    ticket_settings_link = next(
        link
        for link in system_category["links"]
        if link["url"] == "/admin/system/ticket-settings"
    )

    assert ticket_settings_link["name"] == "Ticket Settings"


def test_settings_hub_service_team_link_declares_read_permission(db_session):
    context = web_system_settings_hub.build_settings_hub_context(db_session)

    system_category = next(
        category for category in context["categories"] if category["id"] == "system"
    )
    service_team_link = next(
        link
        for link in system_category["links"]
        if link["url"] == "/admin/system/service-teams"
    )

    assert service_team_link["name"] == "Service Teams"
    assert service_team_link["permission"] == "operations:service_team:read"


def test_settings_hub_includes_bulk_notification_setup_link(db_session):
    context = web_system_settings_hub.build_settings_hub_context(db_session)

    notifications_category = next(
        category
        for category in context["categories"]
        if category["id"] == "notifications"
    )
    bulk_setup_link = next(
        link
        for link in notifications_category["links"]
        if link["url"] == "/admin/notifications/setup"
    )

    assert bulk_setup_link["name"] == "Bulk Notification Setup"
