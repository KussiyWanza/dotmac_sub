"""Every literal ``extends``/``include`` target must exist.

Jinja resolves ``{% extends %}`` at RENDER time, not import time, so a template
naming a base that does not exist raises ``TemplateNotFound`` as a 500 on the
first real request and is invisible to every import-time or route-registration
check.

Regression: four admin templates extended ``admin/base.html``, which has never
existed in this repo. `/admin/vendors/operations`, the two vendor review
confirmations, and the provisioning service-change reconciliation page all
returned HTTP 500 in production until a browser smoke crawl hit one of them.
"""

from __future__ import annotations

import re
from pathlib import Path

TEMPLATES_DIR = Path(__file__).resolve().parents[2] / "templates"

# Literal single- or double-quoted targets only; a computed target
# ({% extends layout_var %}) cannot be resolved statically and is skipped.
_DIRECTIVE = re.compile(
    r"""\{%-?\s*(extends|include)\s+(['"])(?P<target>[^'"]+)\2""",
)


def _iter_directives():
    for template in sorted(TEMPLATES_DIR.rglob("*.html")):
        text = template.read_text(encoding="utf-8")
        for match in _DIRECTIVE.finditer(text):
            yield template, match.group(1), match.group("target")


def test_every_literal_template_target_exists() -> None:
    missing = [
        f"{template.relative_to(TEMPLATES_DIR)} {kind} '{target}', which does not exist"
        for template, kind, target in _iter_directives()
        if not (TEMPLATES_DIR / target).is_file()
    ]

    assert not missing, "Templates reference non-existent targets:\n  " + "\n  ".join(
        sorted(missing)
    )


def test_the_guard_can_actually_see_templates() -> None:
    """A silent glob failure would make the check above vacuously pass."""
    directives = list(_iter_directives())
    assert len(directives) > 100, f"only found {len(directives)} directives"
