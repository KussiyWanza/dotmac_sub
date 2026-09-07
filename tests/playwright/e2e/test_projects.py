from __future__ import annotations

from uuid import uuid4

from playwright.sync_api import expect


def test_admin_project_customer_typeahead_selects_and_clears(admin_page, settings):
    customer_id = str(uuid4())
    requests: list[str] = []

    def fulfill_customer_search(route):
        requests.append(route.request.url)
        route.fulfill(
            status=200,
            content_type="application/json",
            body=(
                '{"items":[{"id":"'
                + customer_id
                + '","label":"Acme Fiber · ACC-420 · ops@acme.test",'
                '"email":"ops@acme.test","account_number":"ACC-420",'
                '"subscriber_number":"SUB-420"}],"count":1,"limit":20,'
                '"offset":0}'
            ),
        )

    admin_page.route("**/admin/projects/customers/search?**", fulfill_customer_search)
    admin_page.goto(
        f"{settings.base_url}/admin/projects/new",
        wait_until="domcontentloaded",
    )

    customer_search = admin_page.get_by_label("Customer", exact=True)
    customer_search.fill("Acme Fiber")
    expect(
        admin_page.get_by_role("option", name="Acme Fiber · ACC-420 · ops@acme.test")
    ).to_be_visible()
    assert len(requests) == 1

    admin_page.get_by_role(
        "option", name="Acme Fiber · ACC-420 · ops@acme.test"
    ).click()
    selected_id = admin_page.locator('input[name="subscriber_id"]')
    expect(selected_id).to_have_value(customer_id)

    admin_page.get_by_role("button", name="Clear").click()
    expect(customer_search).to_have_value("")
    expect(selected_id).to_have_value("")

    customer_search.fill("Unselected customer")
    validation = customer_search.evaluate(
        """input => {
            const allowed = input.form.dispatchEvent(
                new Event("submit", {bubbles: true, cancelable: true})
            );
            return {allowed, message: input.validationMessage};
        }"""
    )
    assert validation == {
        "allowed": False,
        "message": (
            "Select a customer from the search results or clear the search field."
        ),
    }


def test_admin_project_template_dependency_journey(admin_page, settings):
    template_name = f"E2E project template {uuid4().hex[:8]}"

    admin_page.goto(
        f"{settings.base_url}/admin/projects/templates",
        wait_until="domcontentloaded",
    )
    admin_page.get_by_role("link", name="New Template").click()
    admin_page.get_by_label("Name").fill(template_name)
    admin_page.get_by_label("Description").fill(
        "Disposable browser regression template"
    )
    admin_page.get_by_role("button", name="Create Template").click()
    expect(admin_page).to_have_url("**/admin/projects/templates/*")

    admin_page.get_by_role("link", name="Edit Tasks").click()
    admin_page.get_by_role("button", name="Add Task").click()
    admin_page.get_by_role("button", name="Add Task").click()

    titles = admin_page.get_by_label("Title")
    titles.nth(0).fill("Survey")
    titles.nth(1).fill("Install")
    admin_page.get_by_label("Depends On (earlier tasks)").nth(1).select_option(
        label="Survey"
    )
    admin_page.get_by_role("button", name="Save Tasks").click()

    expect(admin_page.get_by_text("Survey", exact=True)).to_be_visible()
    expect(admin_page.get_by_text("Install", exact=True)).to_be_visible()

    admin_page.get_by_role("link", name="Edit Tasks").click()
    admin_page.get_by_role("button", name="Move up").nth(1).click()
    expect(
        admin_page.get_by_text(
            "A dependency was removed because tasks may depend only on earlier tasks."
        )
    ).to_be_visible()
    admin_page.get_by_role("button", name="Save Tasks").click()

    admin_page.on("dialog", lambda dialog: dialog.accept())
    admin_page.get_by_role("button", name="Delete Template").click()
    expect(admin_page).to_have_url("**/admin/projects/templates")
