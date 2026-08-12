from __future__ import annotations

import re
from urllib.parse import parse_qs

import pytest
from playwright.sync_api import Page, expect


def _first_ont_detail_path(admin_page: Page, base_url: str) -> str:
    admin_page.goto(f"{base_url}/admin/network/onts", wait_until="domcontentloaded")
    links = admin_page.locator("a[href*='/admin/network/onts/']")
    for index in range(links.count()):
        href = links.nth(index).get_attribute("href")
        if not href:
            continue
        path = href if href.startswith("/") else re.sub(r"^https?://[^/]+", "", href)
        if re.search(
            r"/admin/network/onts/[0-9a-fA-F]{8}-[0-9a-fA-F-]{27}(?:[/?#]|$)",
            path,
        ):
            return path

    pytest.skip("No ONT record detail link available")


def _route_success(admin_page: Page, pattern: str):
    def _handler(route):
        route.fulfill(
            status=200,
            headers={"Content-Type": "text/html"},
            body='<div class="text-sm text-emerald-700">Applied</div>',
        )

    admin_page.route(pattern, _handler)


def test_ont_apply_device_config_save_push_and_actions(admin_page: Page, settings):
    """Device config exposes and submits the main ONT config workflows."""
    detail_path = _first_ont_detail_path(admin_page, settings.base_url)
    admin_page.goto(
        f"{settings.base_url}{detail_path.split('?')[0]}?tab=device-config",
        wait_until="domcontentloaded",
    )

    panel = admin_page.locator("#apply-device-config-panel")
    expect(panel).to_be_visible()
    for label in (
        "WAN",
        "LAN / DHCP",
        "WiFi",
        "Web Credentials",
        "Connection Request Credentials",
    ):
        expect(panel.get_by_text(label, exact=True)).to_be_visible()
    for label in (
        "WAN remote access on",
        "MGMT remote access on",
        "HTTP management on",
        "Apply LAN Port",
    ):
        expect(panel.get_by_text(label)).to_be_visible()

    push_checkbox = admin_page.locator("input[name='push_to_device'][value='true']")
    expect(push_checkbox).to_be_visible()

    _route_success(admin_page, "**/admin/network/onts/*/configure")
    _route_success(admin_page, "**/admin/network/onts/*/wan-remote-access")
    _route_success(admin_page, "**/admin/network/onts/*/lan-port")

    push_checkbox.check()
    with admin_page.expect_request("**/admin/network/onts/*/configure") as request_info:
        admin_page.get_by_role("button", name="Update Configuration").click()
    configure_body = parse_qs(request_info.value.post_data or "")
    assert configure_body["push_to_device"] == ["true"]

    panel.locator(
        "form[hx-post$='/wan-remote-access'] select[name='enabled']"
    ).select_option("true")
    with admin_page.expect_request(
        "**/admin/network/onts/*/wan-remote-access"
    ) as request_info:
        panel.get_by_role("button", name="Apply WAN Remote").click()
    wan_remote_body = parse_qs(request_info.value.post_data or "")
    assert wan_remote_body["enabled"] == ["true"]

    lan_port_form = panel.locator("form[hx-post$='/lan-port']")
    lan_port_form.locator("input[name='port']").fill("2")
    lan_port_form.locator("select[name='enabled']").select_option("false")
    with admin_page.expect_request("**/admin/network/onts/*/lan-port") as request_info:
        panel.get_by_role("button", name="Apply LAN Port").click()
    lan_port_body = parse_qs(request_info.value.post_data or "")
    assert lan_port_body["port"] == ["2"]
    assert lan_port_body["enabled"] == ["false"]

    web_credentials = panel.locator("form[hx-post$='/web-credentials']")
    expect(web_credentials.locator("input[name='username']")).to_be_visible()
    web_password = web_credentials.locator("input[name='password']")
    expect(web_password).to_be_visible()
    assert web_password.get_attribute("type") == "password"


@pytest.mark.parametrize("viewport", ((1440, 900), (390, 844)))
def test_ont_configure_htmx_exposes_queued_operation_on_desktop_and_mobile(
    admin_page: Page,
    settings,
    viewport: tuple[int, int],
):
    """The supported Configure form swaps tracked progress at both breakpoints."""

    admin_page.set_viewport_size({"width": viewport[0], "height": viewport[1]})
    detail_path = _first_ont_detail_path(admin_page, settings.base_url)
    admin_page.goto(
        f"{settings.base_url}{detail_path.split('?')[0]}?tab=configuration",
        wait_until="domcontentloaded",
    )
    configure = admin_page.locator("#configure-content")
    expect(configure).to_be_visible()
    form = configure.locator("form[hx-post$='/configure']")
    expect(form).to_be_visible()
    expect(form.locator("input[name='idempotency_key']")).to_have_count(1)
    expect(form.locator("input[name='pppoe_password']")).to_have_count(0)
    expect(form.locator("input[name='pppoe_username']")).to_have_count(0)

    operation_id = "11111111-1111-1111-1111-111111111111"

    def _queued(route):
        route.fulfill(
            status=200,
            headers={"Content-Type": "text/html"},
            body=(
                '<section aria-label="Current ONT configuration lifecycle">'
                '<div role="status">Configuration queued</div>'
                f"<div>Operation {operation_id}</div>"
                "<div>Queued revision 2 - readback not verified</div>"
                "</section>"
            ),
        )

    admin_page.route("**/admin/network/onts/*/configure", _queued)
    with admin_page.expect_request("**/admin/network/onts/*/configure"):
        form.get_by_role("button", name="Apply Mgmt").click()

    expect(configure.get_by_text("Configuration queued")).to_be_visible()
    expect(configure.get_by_text(f"Operation {operation_id}")).to_be_visible()
    expect(configure.get_by_text("readback not verified", exact=False)).to_be_visible()


def test_ont_return_to_inventory_ui_posts_and_redirects(admin_page: Page, settings):
    """Return-to-inventory UI posts through HTMX and follows the inventory redirect."""
    detail_path = _first_ont_detail_path(admin_page, settings.base_url)
    admin_page.goto(
        f"{settings.base_url}{detail_path.split('?')[0]}",
        wait_until="domcontentloaded",
    )

    def _handler(route):
        route.fulfill(
            status=200,
            headers={
                "HX-Redirect": "/admin/network/onts?view=unconfigured",
                "HX-Trigger": (
                    '{"showToast":{"type":"success",'
                    '"message":"ONT returned to inventory"}}'
                ),
            },
            body="",
        )

    admin_page.route("**/admin/network/onts/*/return-to-inventory", _handler)
    admin_page.on("dialog", lambda dialog: dialog.accept())

    button = admin_page.get_by_role("button", name="Return to Inventory").first
    expect(button).to_be_visible()
    with admin_page.expect_request(
        "**/admin/network/onts/*/return-to-inventory"
    ) as request_info:
        button.click()

    request = request_info.value
    assert request.method == "POST"
    assert request.headers.get("hx-request") == "true"
    admin_page.wait_for_url(
        "**/admin/network/onts?view=unconfigured", wait_until="domcontentloaded"
    )
