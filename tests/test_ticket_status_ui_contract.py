from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text()


def test_support_templates_consume_shared_status_presentation() -> None:
    admin_components = _read("templates/admin/support/tickets/_components.html")
    customer_list = _read("templates/customer/support/index.html")
    customer_detail = _read("templates/customer/support/detail.html")
    reseller_list = _read("templates/reseller/accounts/tickets.html")

    assert "status_presentation_badge" in admin_components
    assert "color_classes" not in admin_components
    assert "status_presentation_badge" in customer_list
    assert "status_presentation_badge" in customer_detail
    assert "status_presentation_badge" in reseller_list


def test_support_status_color_configuration_is_retired() -> None:
    settings_service = _read("app/services/support_ticket_settings.py")
    settings_route = _read("app/web/admin/system.py")
    settings_template = _read("templates/admin/system/ticket_settings.html")
    portal_service = _read("app/services/crm_portal.py")

    combined = "\n".join(
        [settings_service, settings_route, settings_template, portal_service]
    )
    assert "TICKET_STATUS_DISPLAY" not in combined
    assert "TICKET_STATUS_COLORS" not in combined
    assert "STATUS_COLORS_KEY" not in combined
    assert "status_color_options" not in combined
    assert "status_color_statuses" not in combined


def test_support_status_surfaces_do_not_advertise_legacy_resolved_status() -> None:
    settings_template = _read("templates/admin/system/ticket_settings.html")
    confirmation_template = _read("templates/public/ticket_confirm.html")
    mobile_ticket = _read("mobile/lib/src/models/ticket.dart")

    assert "<code>resolved</code>" not in settings_template
    assert ">Resolved {{ ticket.resolved_at" not in confirmation_template
    assert "status != 'resolved'" not in mobile_ticket


def test_customer_mobile_ticket_surfaces_render_server_presentation() -> None:
    status_chip = _read("mobile/lib/src/widgets/status_chip.dart")
    tickets = _read("mobile/lib/src/features/support/tickets_screen.dart")
    ticket_detail = _read("mobile/lib/src/features/support/ticket_detail_screen.dart")

    assert "StatusChip.forTicket" not in status_chip
    assert "StatusChip.fromPresentation(t.statusPresentation)" in tickets
    assert "StatusChip.fromPresentation(t.statusPresentation)" in ticket_detail
