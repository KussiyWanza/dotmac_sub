from __future__ import annotations

import os

import pytest
from playwright.sync_api import Page, expect


def _vendor_project_path() -> str:
    value = os.getenv("E2E_VENDOR_PROJECT_PATH")
    if not value:
        pytest.skip("Set E2E_VENDOR_PROJECT_PATH to smoke-test a vendor project map.")
    return value if value.startswith("/") else f"/{value}"


def test_vendor_project_operations_map_controls_render(
    vendor_page: Page,
    settings,
) -> None:
    vendor_page.goto(
        f"{settings.base_url}{_vendor_project_path()}",
        wait_until="domcontentloaded",
    )

    expect(vendor_page.locator("#route-author-map")).to_be_visible()
    expect(vendor_page.locator("#route-author-filters")).to_be_visible()
    expect(vendor_page.locator("[data-route-layer-filter]")).to_have_count(3)
    expect(vendor_page.locator("[data-route-status-filter]")).to_have_count(6)
    expect(vendor_page.locator("[data-route-poi-filter]")).to_have_count(5)
    expect(vendor_page.locator("#route-author-poi-radius")).to_be_visible()
    expect(vendor_page.locator("#route-author-filter-summary")).to_be_visible()
    expect(vendor_page.locator("#asbuilt-map")).to_be_visible()
