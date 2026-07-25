"""Buttons that submitted the wrong form.

Nested forms are invalid HTML: a browser drops the inner `<form>` start tag, so
its submit button belongs to the *outer* form. Four templates shipped that way,
and the button always did something — just not the thing it was labelled.

* the three GIS edit pages — "Delete" submitted the edit form and **saved** the
  record;
* the usage-charges table — each row's "Post" submitted the bulk-post form and
  posted **every selected charge** instead of that row.

The fix relocates each inner form to be a sibling and points the button at it
with the HTML5 `form` attribute, which preserves the rendered layout exactly.

Found by the nesting guard added with the CSRF sweep, and previously carried in
its `KNOWN_NESTED_FORMS` allowlist. That allowlist is now empty and removed.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

AFFECTED = [
    "templates/admin/gis/area_form.html",
    "templates/admin/gis/layer_form.html",
    "templates/admin/gis/location_form.html",
    "templates/admin/catalog/usage/charges.html",
]


def _form_depth(markup: str) -> tuple[int, int]:
    depth = max_depth = 0
    for token in re.findall(r"<form\b|</form\s*>", markup, re.I):
        if token.lower() == "<form":
            depth += 1
            max_depth = max(max_depth, depth)
        else:
            depth -= 1
    return max_depth, depth


@pytest.mark.parametrize("template", AFFECTED)
def test_no_form_is_nested_inside_another(template):
    max_depth, unclosed = _form_depth(Path(template).read_text())
    assert max_depth <= 1, f"{template}: nested <form> (depth {max_depth})"
    assert unclosed == 0, f"{template}: {unclosed} unclosed <form>"


@pytest.mark.parametrize(
    ("template", "form_id", "action_fragment"),
    [
        ("templates/admin/gis/area_form.html", "delete-area-form", "/areas/"),
        ("templates/admin/gis/layer_form.html", "delete-layer-form", "/layers/"),
        (
            "templates/admin/gis/location_form.html",
            "delete-location-form",
            "/locations/",
        ),
    ],
)
def test_the_delete_button_targets_the_delete_form(template, form_id, action_fragment):
    """The button must reach the delete endpoint, not the edit one."""
    markup = Path(template).read_text()
    assert f'form="{form_id}"' in markup
    assert f'id="{form_id}"' in markup
    assert f"{action_fragment}" in markup
    assert "/delete" in markup


@pytest.mark.parametrize("template", AFFECTED)
def test_every_relocated_form_still_carries_a_csrf_token(template):
    """Relocating a form must not drop the token that makes it submittable."""
    markup = Path(template).read_text()
    for match in re.finditer(r"<form\b[^>]*>(.*?)</form\s*>", markup, re.S | re.I):
        tag, body = match.group(0), match.group(1)
        if not re.search(r'method\s*=\s*["\']?post', tag, re.I):
            continue
        assert "_csrf_token" in body or "csrf_input.html" in body, (
            f"{template}: relocated POST form lost its CSRF token"
        )


def test_gis_delete_confirms_before_submitting():
    """These now really deactivate the record, so the confirm must survive."""
    for template in AFFECTED[:3]:
        assert "confirm(" in Path(template).read_text()


def test_each_charge_row_posts_only_itself():
    markup = Path("templates/admin/catalog/usage/charges.html").read_text()
    # One form per staged charge, keyed by charge id, outside the bulk form.
    assert 'id="post-charge-{{ charge.id }}"' in markup
    assert 'form="post-charge-{{ charge.id }}"' in markup
    bulk_close = markup.index("/charges/bulk-post")
    per_row = markup.index('id="post-charge-{{ charge.id }}"')
    assert per_row > bulk_close, "per-charge forms must sit outside the bulk form"


def test_the_nested_form_allowlist_is_gone():
    """The guard now has nothing to excuse."""
    guard = Path("tests/architecture/test_form_csrf_coverage.py").read_text()
    assert "KNOWN_NESTED_FORMS" not in guard
