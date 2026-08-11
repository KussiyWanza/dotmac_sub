"""Sub's composition boundary for the shared Dotmac UI contract.

``dotmac-ui`` owns the stable asset paths, the token roles and the theme
selector. Sub owns its ISP palette, its module accents and its page
composition. Keeping the seam here stops routes, templates and the static
mount from each inventing a slightly different integration — which is how the
fleet ended up with the forked design system this adoption exists to retire
(``docs/inventories/ui-surface-inventory.md`` in ``dotmac_starter_mt``).

**Sub needs no theme bootstrap.** ``dotmac_ui.contract.DARK_THEME_SELECTORS``
already emits the dark token values under ``.dark`` as well as under the
package's own attribute, precisely because Sub toggles a ``dark`` class on
``<html>``. Sub's existing pre-paint script in ``templates/base.html`` therefore
drives the shared tokens with no template change. Do not add a second theme
bootstrap; two scripts writing two different theme hooks is the drift this
package was published to end.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from dotmac_ui.assets import ASSET_NAMESPACE, static_dir, stylesheet_url
from dotmac_ui.contract import THEME_ATTRIBUTE, UI_CONTRACT_VERSION

#: Mounted ahead of Sub's catch-all ``/static`` so the installed artifact is
#: served directly. No copy of the compiled CSS is checked into this repository
#: — the package is the one writer of its own asset.
UI_ASSET_MOUNT: Final[str] = f"/static/{ASSET_NAMESPACE}"
UI_ASSET_DIRECTORY: Final[Path] = static_dir() / ASSET_NAMESPACE

#: Carries the package's content digest, so it is cache-safe as published. Do
#: not append a Sub cache-busting query string to it.
UI_STYLESHEET_URL: Final[str] = stylesheet_url()

#: Exposed for a future theme switcher and for the architecture test that
#: proves Sub has not invented its own attribute name.
UI_THEME_ATTRIBUTE: Final[str] = THEME_ATTRIBUTE


def template_globals() -> dict[str, str | int]:
    """Values every full-page template needs to consume the UI contract."""
    return {
        "dotmac_ui_contract_version": UI_CONTRACT_VERSION,
        "dotmac_ui_stylesheet_url": UI_STYLESHEET_URL,
        "dotmac_ui_theme_attribute": UI_THEME_ATTRIBUTE,
    }


__all__ = [
    "UI_ASSET_DIRECTORY",
    "UI_ASSET_MOUNT",
    "UI_STYLESHEET_URL",
    "UI_THEME_ATTRIBUTE",
    "template_globals",
]
