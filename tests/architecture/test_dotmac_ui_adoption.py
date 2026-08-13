"""Sub consumes the shared UI contract through its published surface only.

Sub is the *second* consumer of ``dotmac-ui`` (Academy was the first). What
this file protects is not the look of a page — it is the properties that make
a shared design system shared rather than a fourth copy of one:

- the pin is EXACT, from the named private index, so a token vocabulary change
  is a reviewed dependency bump and never a lockfile surprise;
- the compiled asset is SERVED from the installed distribution, so no copy of
  it can be committed here and drift;
- the package mount precedes Sub's catch-all ``/static`` mount, without which
  the namespaced path silently falls through to Sub's own static directory;
- Sub uses the package's theme hook rather than inventing an attribute name —
  the specific mistake ``dotmac_academy_app`` made four times in one pull
  request before the bootstrap was packaged;
- the shared stylesheet loads BEFORE Sub's own, so this adoption cannot repaint
  a page on its own. That ordering is the whole safety argument for landing it
  ahead of the token reconciliation (see docs/adr/0010).
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BASE_TEMPLATE = REPO_ROOT / "templates" / "base.html"
HEAD_PARTIAL = REPO_ROOT / "templates" / "_dotmac_ui_head.html"

EXPECTED_PIN = "0.1.0a3"
INDEX_NAME = "forgejo"


def _pyproject() -> dict:
    with (REPO_ROOT / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)


def test_dotmac_ui_is_pinned_exactly_and_from_the_private_index() -> None:
    project = _pyproject()

    declared = [
        dep
        for dep in project["project"]["dependencies"]
        if dep.split("[")[0].split("=")[0].strip() == "dotmac-ui"
    ]
    assert declared == [f"dotmac-ui=={EXPECTED_PIN}"], (
        "dotmac-ui must be pinned with == in [project].dependencies. A range "
        "would let an unreviewed token vocabulary change reach Sub through a "
        f"lock refresh. Found: {declared!r}"
    )

    source = project["tool"]["poetry"]["dependencies"]["dotmac-ui"]
    assert source["version"] == EXPECTED_PIN
    assert source["source"] == INDEX_NAME, (
        "dotmac-ui must resolve from the named private index, not PyPI, where "
        "the name is unclaimed."
    )


def test_the_compiled_asset_is_served_from_the_installed_distribution() -> None:
    """No copy of the package's CSS may be committed to this repository."""
    strays = [
        path
        for path in (REPO_ROOT / "static").rglob("dotmac-ui*.css")
        if path.is_file()
    ]
    assert strays == [], (
        "Found a committed copy of the dotmac-ui stylesheet: "
        f"{[str(p.relative_to(REPO_ROOT)) for p in strays]}. The package is the "
        "one writer of its own asset; Sub mounts the installed directory "
        "instead (see app/ui.py)."
    )


def test_the_package_mount_precedes_subs_catch_all_static_mount() -> None:
    main = (REPO_ROOT / "app" / "main.py").read_text(encoding="utf-8")

    package_mount = main.index("UI_ASSET_MOUNT,")
    catch_all = main.index('app.mount("/static", StaticFiles(directory="static")')
    assert package_mount < catch_all, (
        "The namespaced dotmac-ui mount must be registered BEFORE the catch-all "
        "/static mount, or Starlette resolves /static/dotmac-ui/... against "
        "Sub's own static directory and serves a 404 instead of the asset."
    )


def test_sub_does_not_invent_its_own_theme_attribute() -> None:
    from dotmac_ui.contract import THEME_ATTRIBUTE

    from app.ui import UI_THEME_ATTRIBUTE

    assert UI_THEME_ATTRIBUTE == THEME_ATTRIBUTE


def test_sub_adds_no_second_theme_bootstrap() -> None:
    """dotmac-ui emits dark values under `.dark`, which Sub already toggles.

    A second bootstrap writing the package's attribute would leave two hooks
    disagreeing after a reload — invisible until a user's choice is forgotten.
    """
    from dotmac_ui.contract import DARK_THEME_SELECTORS

    assert ".dark" in DARK_THEME_SELECTORS, (
        "Sub's adoption relies on dotmac-ui emitting dark token values under "
        "`.dark`. If the package drops that selector, Sub needs the packaged "
        "theme bootstrap and base.html's own script has to be retired with it."
    )

    partial = HEAD_PARTIAL.read_text(encoding="utf-8")
    assert "<script" not in partial, (
        "The head partial must stay a stylesheet link only; the theme hook is "
        "base.html's existing pre-paint script."
    )


def test_the_shared_stylesheet_loads_before_subs_own() -> None:
    base = BASE_TEMPLATE.read_text(encoding="utf-8")

    include = base.index('{% include "_dotmac_ui_head.html" %}')
    for own in ("/static/css/main.css", "/static/css/design-system.css"):
        assert include < base.index(own), (
            f"_dotmac_ui_head.html must be included before {own}. Sub's own "
            "stylesheets win by source order, which is what makes adopting the "
            "shared tokens a no-op visually until each role is reconciled "
            "deliberately (docs/adr/0010)."
        )


def test_the_head_partial_does_not_cache_bust_the_packaged_url() -> None:
    partial = HEAD_PARTIAL.read_text(encoding="utf-8")
    href = re.search(r'href="([^"]+)"', partial)
    assert href is not None
    assert href.group(1) == "{{ dotmac_ui_stylesheet_url }}", (
        "The package's URL already carries its content digest. Appending a Sub "
        "cache key would defeat the digest and pin a stale asset."
    )


@pytest.mark.parametrize(
    "name",
    [
        "dotmac_ui_stylesheet_url",
        "dotmac_ui_theme_attribute",
        "dotmac_ui_contract_version",
    ],
)
def test_full_page_templates_get_the_contract_globals(name: str) -> None:
    from app.ui import template_globals

    assert name in template_globals()
