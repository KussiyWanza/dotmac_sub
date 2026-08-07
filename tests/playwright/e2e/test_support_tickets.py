"""Support-ticket admin interaction tests."""

from __future__ import annotations

from playwright.sync_api import Page, expect


def test_column_picker_closes_from_trigger_and_outside_click(
    admin_page: Page, settings
) -> None:
    """The column picker must not trap the operator in its open state."""

    admin_page.goto(f"{settings.base_url}/admin/support/tickets")

    trigger = admin_page.get_by_role("button", name="Columns")
    panel = admin_page.locator("#ticket-column-options")

    trigger.click()
    expect(panel).to_be_visible()
    expect(trigger).to_have_attribute("aria-expanded", "true")

    trigger.click()
    expect(panel).to_be_hidden()
    expect(trigger).to_have_attribute("aria-expanded", "false")

    trigger.click()
    expect(panel).to_be_visible()

    admin_page.locator("#ticket-filter-form").evaluate(
        "element => element.dataset.e2eBeforeApply = 'true'"
    )
    admin_page.get_by_role("button", name="Apply ticket filters").click()
    admin_page.wait_for_selector(
        "#ticket-filter-form[data-e2e-before-apply='true']",
        state="detached",
    )
    expect(panel).to_be_hidden()

    trigger.click()
    expect(panel).to_be_visible()

    admin_page.get_by_role("heading", name="Support Tickets").click()
    expect(panel).to_be_hidden()
    expect(trigger).to_have_attribute("aria-expanded", "false")

    trigger.evaluate(
        "element => { element.dataset.filterRefreshIdentity = 'preserved'; }"
    )
    admin_page.locator("#ticket-status-filter").select_option("not_closed")
    admin_page.wait_for_url("**status=not_closed**")

    expect(panel).to_be_hidden()
    expect(trigger).to_have_attribute("aria-expanded", "false")
    expect(trigger).to_have_attribute("data-filter-refresh-identity", "preserved")


def test_filter_feedback_reports_loading_and_keeps_results_on_failure(
    admin_page: Page, settings
) -> None:
    admin_page.goto(f"{settings.base_url}/admin/support/tickets")
    held_routes = []
    admin_page.route(
        "**/admin/support/tickets?**", lambda route: held_routes.append(route)
    )

    admin_page.locator("#ticket-status-filter").select_option("not_closed")
    expect(admin_page.get_by_text("Updating tickets…", exact=True)).to_be_visible()
    assert held_routes

    held_routes.pop().abort("failed")

    error = admin_page.get_by_role("alert")
    expect(error).to_contain_text(
        "Couldn’t update tickets. Your current results are still shown."
    )
    expect(error.get_by_role("button", name="Retry")).to_be_visible()
    expect(admin_page.locator("#tickets-table")).to_be_visible()
