"""Real-browser regression coverage for application dialogs over Leaflet maps."""

from pathlib import Path

import pytest
from playwright.async_api import async_playwright

PROJECT_ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.asyncio
async def test_biodata_dialog_paints_above_leaflet_panes_and_controls() -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        page = await browser.new_page(viewport={"width": 900, "height": 700})
        await page.set_content(
            """
        <div id="biodata-dialog"
             class="fixed inset-0 z-50 flex items-center justify-center"
             role="dialog" aria-modal="true"
             aria-labelledby="biodata-dialog-title">
          <section class="h-48 w-96 bg-white">
            <h2 id="biodata-dialog-title">Your biodata is incomplete</h2>
          </section>
        </div>
        <div id="customer-location-map" class="leaflet-container h-[28rem] w-full">
          <div class="leaflet-pane leaflet-marker-pane" data-map-layer></div>
          <div class="leaflet-top leaflet-left">
            <button type="button" class="leaflet-control" data-map-control>
              Zoom
            </button>
          </div>
        </div>
        """
        )
        await page.add_style_tag(path=str(PROJECT_ROOT / "static/css/main.css"))
        await page.add_style_tag(
            path=str(PROJECT_ROOT / "static/css/design-system.css")
        )
        await page.add_style_tag(
            path=str(PROJECT_ROOT / "static/vendor/leaflet/leaflet.css")
        )

        result = await page.evaluate(
            """() => {
          const map = document.querySelector('.leaflet-container');
          const box = document.querySelector('[data-map-control]').getBoundingClientRect();
          const topElement = document.elementFromPoint(
            box.x + box.width / 2,
            box.y + box.height / 2,
          );
          return {
            mapIsolation: getComputedStyle(map).isolation,
            topLayerIsDialog: Boolean(topElement.closest('[role="dialog"]')),
          };
        }"""
        )
        await browser.close()

    assert result == {"mapIsolation": "isolate", "topLayerIsDialog": True}
