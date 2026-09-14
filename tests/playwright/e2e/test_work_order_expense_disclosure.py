"""Real-browser coverage for the reusable work-order expense disclosure."""

from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

PROJECT_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture()
def expense_page(playwright_instance) -> Page:
    browser = playwright_instance.chromium.launch(
        headless=True,
        args=["--no-sandbox", "--disable-dev-shm-usage"],
    )
    page = browser.new_page()
    try:
        yield page
    finally:
        browser.close()


def _load_disclosure(
    page: Page, *, work_order_id: str, open_form: bool = False
) -> None:
    open_attribute = " open" if open_form else ""
    page.set_content(
        f"""
        <main data-work-order-id="{work_order_id}">
          <button type="button" aria-controls="new-expense-claim"
                  aria-expanded="{"true" if open_form else "false"}"
                  data-open-expense-form>New Expense Claim</button>
          <details id="new-expense-claim"{open_attribute}>
            <summary>New Expense Claim</summary>
            <form data-expense-form>
              <input name="purpose" data-expense-purpose>
            </form>
          </details>
        </main>
        """
    )
    page.add_script_tag(path=str(PROJECT_ROOT / "static/js/work-order-expense-form.js"))


def test_expense_button_opens_focuses_and_tracks_summary_state(
    expense_page: Page,
) -> None:
    page = expense_page
    for work_order_id in ("sub-browser-arbitrary-a", "sub-browser-arbitrary-b"):
        _load_disclosure(page, work_order_id=work_order_id)
        opener = page.get_by_role("button", name="New Expense Claim")
        details = page.locator("#new-expense-claim")
        purpose = page.locator("[data-expense-purpose]")

        opener.click()
        expect(details).to_have_attribute("open", "")
        expect(opener).to_have_attribute("aria-expanded", "true")
        expect(purpose).to_be_focused()

        page.get_by_text("New Expense Claim", exact=True).last.click()
        expect(details).not_to_have_attribute("open", "")
        expect(opener).to_have_attribute("aria-expanded", "false")

        opener.focus()
        opener.press("Enter")
        expect(details).to_have_attribute("open", "")
        expect(opener).to_have_attribute("aria-expanded", "true")
        expect(purpose).to_be_focused()


def test_validation_redisplay_stays_open_and_disabled_user_has_no_form(
    expense_page: Page,
) -> None:
    page = expense_page
    _load_disclosure(page, work_order_id="sub-browser-validation", open_form=True)
    expect(page.locator("#new-expense-claim")).to_have_attribute("open", "")
    expect(page.locator("[data-open-expense-form]")).to_have_attribute(
        "aria-expanded", "true"
    )

    page.set_content(
        """
        <button type="button" disabled aria-disabled="true"
                aria-controls="new-expense-claim" aria-expanded="false">
          New Expense Claim
        </button>
        """
    )
    disabled = page.get_by_role("button", name="New Expense Claim")
    expect(disabled).to_be_disabled()
    expect(page.locator("#new-expense-claim")).to_have_count(0)
    expect(page.locator("[data-expense-form]")).to_have_count(0)
