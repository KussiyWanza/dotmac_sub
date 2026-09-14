"""Real-browser regression coverage for application dialogs over Leaflet maps."""

from pathlib import Path

from playwright.sync_api import Browser

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def test_biodata_dialog_paints_above_leaflet_panes_and_controls(
    browser: Browser,
) -> None:
    page = browser.new_page(viewport={"width": 900, "height": 700})
    page.set_content(
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
    page.add_style_tag(path=str(PROJECT_ROOT / "static/css/main.css"))
    page.add_style_tag(path=str(PROJECT_ROOT / "static/css/design-system.css"))
    page.add_style_tag(path=str(PROJECT_ROOT / "static/vendor/leaflet/leaflet.css"))

    result = page.evaluate(
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
    page.close()

    assert result == {"mapIsolation": "isolate", "topLayerIsDialog": True}
