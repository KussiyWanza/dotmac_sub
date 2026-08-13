"""Browser contract tests for the isolated dashboard attendance control."""

from pathlib import Path

import pytest
from playwright.sync_api import Page, Route, expect

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "static/js/admin-attendance.js"


@pytest.fixture()
def attendance_page(browser):
    """Isolate mocked attendance state within the shared E2E browser."""

    context = browser.new_context()
    page = context.new_page()
    try:
        yield page
    finally:
        page.close()
        context.close()


def _page_html(action: str = "check-in") -> str:
    label = "Check In" if action == "check-in" else "Check Out"
    return f"""
    <html><head><meta name="csrf-token" content="csrf-test"></head><body>
      <main id="main-dashboard">Dashboard remains available</main>
      <section id="attendance-widget">
        <p data-attendance-error role="alert"></p>
        <button data-attendance-action="{action}">{label}</button>
      </section>
    </body></html>
    """


def _completed_partial(*, checked_out: bool = False) -> str:
    if checked_out:
        return """<section id="attendance-widget"><p>Checked in: 08:04</p><p>Checked out: 17:11</p><p data-attendance-error></p></section>"""
    return """<section id="attendance-widget"><p>Checked in: 08:04</p><p data-attendance-error></p><button data-attendance-action="check-out">Check Out</button></section>"""


def _open(page: Page, html: str) -> None:
    page.route(
        "https://selfcare.test/admin/dashboard",
        lambda route: route.fulfill(status=200, content_type="text/html", body=html),
    )
    page.goto("https://selfcare.test/admin/dashboard")
    page.add_script_tag(path=str(SCRIPT))


def test_check_in_uses_browser_location_and_updates_authoritative_state(
    attendance_page: Page,
):
    page = attendance_page
    payloads: list[dict] = []
    page.add_init_script(
        """navigator.geolocation.getCurrentPosition = (ok) => ok({coords: {latitude: 9.0765, longitude: 7.3986, accuracy: 12.5}, timestamp: Date.now()});"""
    )

    def punch(route: Route) -> None:
        payloads.append(route.request.post_data_json)
        assert route.request.headers["x-csrf-token"] == "csrf-test"
        assert route.request.headers["idempotency-key"]
        route.fulfill(status=200, content_type="text/html", body=_completed_partial())

    page.route("**/admin/dashboard/attendance/check-in", punch)
    _open(page, _page_html())

    page.get_by_role("button", name="Check In").click()

    expect(page.get_by_role("button", name="Check Out")).to_be_visible()
    assert payloads[0]["latitude"] == 9.0765
    assert payloads[0]["longitude"] == 7.3986
    assert payloads[0]["accuracy_m"] == 12.5


def test_location_denial_never_issues_punch(attendance_page: Page):
    page = attendance_page
    mutations = 0
    page.add_init_script(
        """navigator.geolocation.getCurrentPosition = (_ok, fail) => fail({code: 1});"""
    )

    def punch(route: Route) -> None:
        nonlocal mutations
        mutations += 1
        route.abort()

    page.route("**/admin/dashboard/attendance/check-in", punch)
    _open(page, _page_html())
    page.get_by_role("button", name="Check In").click()

    expect(page.get_by_role("alert")).to_contain_text("Location access is required")
    assert mutations == 0
    expect(page.locator("#main-dashboard")).to_be_visible()


def test_check_out_captures_fresh_location_and_finishes_widget(attendance_page: Page):
    page = attendance_page
    payloads: list[dict] = []
    page.add_init_script(
        """navigator.geolocation.getCurrentPosition = (ok) => ok({coords: {latitude: 9.08, longitude: 7.40, accuracy: 8}, timestamp: Date.now()});"""
    )

    def punch(route: Route) -> None:
        payloads.append(route.request.post_data_json)
        route.fulfill(
            status=200,
            content_type="text/html",
            body=_completed_partial(checked_out=True),
        )

    page.route("**/admin/dashboard/attendance/check-out", punch)
    _open(page, _page_html("check-out"))
    page.get_by_role("button", name="Check Out").click()

    expect(page.locator("#attendance-widget")).to_contain_text("Checked out: 17:11")
    expect(page.locator("#attendance-widget button")).to_have_count(0)
    assert payloads[0]["accuracy_m"] == 8
