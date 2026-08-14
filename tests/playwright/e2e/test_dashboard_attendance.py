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


def _page_html(action: str = "check-in", *, include_csrf_meta: bool = True) -> str:
    label = "Check In" if action == "check-in" else "Check Out"
    csrf_meta = (
        '<meta name="csrf-token" content="csrf-test">' if include_csrf_meta else ""
    )
    return f"""
    <html><head>{csrf_meta}</head><body>
      <main id="main-dashboard">Dashboard remains available</main>
      <section id="attendance-widget">
        <p data-attendance-error role="alert"></p>
        <button data-attendance-action="{action}">{label}</button>
      </section>
    </body></html>
    """


def _completed_partial(*, checked_out: bool = False) -> str:
    if checked_out:
        return """<section id="attendance-widget"><time data-attendance-elapsed data-attendance-start="2026-08-09T08:04:00+00:00" data-attendance-end="2026-08-09T17:11:00+00:00">00:00:00</time><p data-attendance-error></p></section>"""
    return """<section id="attendance-widget"><time data-attendance-elapsed data-attendance-start="2026-08-09T08:04:00+00:00">00:00:00</time><p data-attendance-error></p><button data-attendance-action="check-out">Check Out</button></section>"""


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


def test_checked_in_timer_starts_from_erp_timestamp_after_widget_replacement(
    attendance_page: Page,
):
    page = attendance_page
    page.add_init_script(
        """
        Date.now = () => Date.parse("2026-08-09T08:05:31+00:00");
        navigator.geolocation.getCurrentPosition = (ok) => ok({
            coords: {latitude: 9.0765, longitude: 7.3986, accuracy: 12.5},
            timestamp: Date.now(),
        });
        """
    )
    page.route(
        "**/admin/dashboard/attendance/check-in",
        lambda route: route.fulfill(
            status=200, content_type="text/html", body=_completed_partial()
        ),
    )
    _open(page, _page_html())

    page.get_by_role("button", name="Check In").click()

    expect(page.locator("[data-attendance-elapsed]")).to_have_text("00:01:31")


def test_each_attendance_punch_uses_a_distinct_idempotency_key(
    attendance_page: Page,
):
    page = attendance_page
    idempotency_keys: list[str] = []
    page.add_init_script(
        "navigator.geolocation.getCurrentPosition = (ok) => ok({coords: {latitude: 9.08, longitude: 7.40, accuracy: 8}, timestamp: Date.now()});"
    )

    def record_key(route: Route, *, checked_out: bool) -> None:
        idempotency_keys.append(route.request.headers["idempotency-key"])
        route.fulfill(
            status=200,
            content_type="text/html",
            body=_completed_partial(checked_out=checked_out),
        )

    page.route(
        "**/admin/dashboard/attendance/check-in",
        lambda route: record_key(route, checked_out=False),
    )
    page.route(
        "**/admin/dashboard/attendance/check-out",
        lambda route: record_key(route, checked_out=True),
    )
    _open(page, _page_html())

    page.get_by_role("button", name="Check In").click()
    expect(page.get_by_role("button", name="Check Out")).to_be_visible()
    page.get_by_role("button", name="Check Out").click()

    expect(page.locator("#attendance-widget button")).to_have_count(0)
    assert len(idempotency_keys) == 2
    assert idempotency_keys[0] != idempotency_keys[1]


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


def test_checkout_uses_csrf_cookie_when_dashboard_has_no_meta_token(
    attendance_page: Page,
):
    page = attendance_page
    page.add_init_script("document.cookie = 'csrf_token=cookie-test; path=/';")

    def punch(route: Route) -> None:
        assert route.request.headers["x-csrf-token"] == "cookie-test"
        route.fulfill(
            status=200,
            content_type="text/html",
            body=_completed_partial(checked_out=True),
        )

    page.route("**/admin/dashboard/attendance/check-out", punch)
    _open(page, _page_html("check-out", include_csrf_meta=False))
    page.get_by_role("button", name="Check Out").click()

    expect(page.locator("#attendance-widget button")).to_have_count(0)


def test_confirmed_selfcare_rejection_is_not_reconciled_as_an_erp_timeout(
    attendance_page: Page,
):
    page = attendance_page
    state_reads = 0
    page.add_init_script(
        "navigator.geolocation.getCurrentPosition = (ok) => ok({coords: {latitude: 9.08, longitude: 7.40, accuracy: 8}, timestamp: Date.now()});"
    )

    def state(route: Route) -> None:
        nonlocal state_reads
        state_reads += 1
        route.fulfill(status=200, content_type="text/html", body=_completed_partial())

    page.route("**/admin/dashboard/attendance", state)
    page.route(
        "**/admin/dashboard/attendance/check-out",
        lambda route: route.fulfill(status=403),
    )
    _open(page, _page_html("check-out"))
    page.get_by_role("button", name="Check Out").click()

    expect(page.get_by_role("alert")).to_contain_text("security token expired")
    assert state_reads == 0


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

    expect(page.locator("[data-attendance-elapsed]")).to_have_text("09:07:00")
    expect(page.locator("#attendance-widget button")).to_have_count(0)
    assert payloads[0]["accuracy_m"] == 8
