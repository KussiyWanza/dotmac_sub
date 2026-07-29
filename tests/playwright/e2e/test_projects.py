from __future__ import annotations

from uuid import uuid4

from playwright.sync_api import expect


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
