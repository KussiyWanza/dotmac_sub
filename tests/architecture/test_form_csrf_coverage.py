"""Every native form POST on a CSRF-protected path must carry a token.

`app/main.py` protects `/admin/`, `/web/`, `/portal/`, `/reseller/`, `/vendor/`
and `/auth/`. For a state-changing method the middleware accepts either an
`X-CSRF-Token` header or a `_csrf_token` form field, and a form-encoded POST
without the field is rejected. The middleware failed closed correctly — the
damage was operational: the affected admin actions simply never worked.

Two categories are exempt, and both exemptions are asserted here rather than
assumed:

* forms driven by ``hx-post``/``hx-put``/``hx-patch``/``hx-delete`` — the
  ``htmx:configRequest`` hook in ``templates/base.html`` attaches the header to
  every htmx request. ``test_htmx_exemption_is_backed_by_base_template`` fails
  if that hook is ever removed, which would silently widen the exemption into a
  hole;
* forms whose submit is prevented (``@submit.prevent``) — they never issue a
  native POST.

`/public/` is not a protected prefix, so its forms legitimately need no token.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = ROOT / "templates"

FORM_OPEN = re.compile(r"<form\b", re.I)
FORM_CLOSE = re.compile(r"</form\s*>", re.I)
IS_POST = re.compile(r'method\s*=\s*["\']?post', re.I)
HTMX_VERB = re.compile(r"\bhx-(post|put|patch|delete)\b", re.I)
SUBMIT_PREVENTED = re.compile(r"(@submit\.prevent|x-on:submit\.prevent)", re.I)
TOKEN_MARKERS = ("_csrf_token", "csrf_input.html")

UNPROTECTED_PREFIXES = ("templates/public/",)


# --- scanner ------------------------------------------------------------


def untokenised_post_forms(markup: str) -> list[str]:
    """Native POST forms in ``markup`` that carry no CSRF token."""
    offenders: list[str] = []
    index = 0
    while True:
        match = FORM_OPEN.search(markup, index)
        if not match:
            return offenders
        start = match.start()
        tag_end = markup.find(">", start)
        if tag_end == -1:
            return offenders
        tag = markup[start : tag_end + 1]
        close = FORM_CLOSE.search(markup, tag_end)
        body = markup[tag_end + 1 : close.start() if close else len(markup)]
        index = close.end() if close else tag_end + 1

        if not IS_POST.search(tag):
            continue
        if HTMX_VERB.search(tag) or SUBMIT_PREVENTED.search(tag):
            continue
        if any(marker in body for marker in TOKEN_MARKERS):
            continue
        action = re.search(r'action\s*=\s*["\']([^"\']+)', tag)
        offenders.append(action.group(1) if action else tag[:80])


def misplaced_tokens(markup: str) -> int:
    """Count tokens emitted outside any form, where they are inert."""
    depth = 0
    stray = 0
    for token in re.findall(r"<form\b|</form\s*>|csrf_input\.html", markup, re.I):
        lowered = token.lower()
        if lowered == "<form":
            depth += 1
        elif lowered.startswith("</form"):
            depth = max(0, depth - 1)
        elif depth <= 0:
            stray += 1
    return stray


def form_tag_balance(markup: str) -> tuple[int, int]:
    """(max nesting depth, unclosed forms) for literal ``<form>`` tags."""
    depth = max_depth = 0
    for token in re.findall(r"<form\b|</form\s*>", markup, re.I):
        if token.lower() == "<form":
            depth += 1
            max_depth = max(max_depth, depth)
        else:
            depth -= 1
            if depth < 0:  # stray close; treat as balanced-from-zero
                depth = 0
    return max_depth, depth


def _unprotected(path: Path) -> bool:
    return path.relative_to(ROOT).as_posix().startswith(UNPROTECTED_PREFIXES)


# --- scanner fixtures ---------------------------------------------------

MULTILINE_FORM = """
<form method="post" action="/admin/thing">
    <input name="a">
    <button type="submit">Go</button>
</form>
"""

ONE_LINE_FORM = (
    '<form method="post" action="/admin/x"><button type="submit">x</button></form>'
)

TOKEN_OUTSIDE_FORM = (
    '<form method="post" action="/admin/x"><button>x</button></form>\n'
    '{% include "components/forms/csrf_input.html" %}'
)

TOKEN_INSIDE_ONE_LINE = (
    '<form method="post" action="/admin/x">'
    '{% include "components/forms/csrf_input.html" %}'
    "<button>x</button></form>"
)

HTMX_FORM = '<form hx-post="/admin/x"><button>x</button></form>'
HTMX_FORM_WITH_METHOD = (
    '<form method="post" hx-post="/admin/x"><button>x</button></form>'
)
PREVENTED_FORM = '<form method="post" @submit.prevent="go()"><button>x</button></form>'
GET_FORM = '<form method="get" action="/admin/x"><button>x</button></form>'


@pytest.mark.parametrize(
    ("markup", "expected"),
    [
        pytest.param(MULTILINE_FORM, 1, id="multiline-form-untokenised"),
        pytest.param(ONE_LINE_FORM, 1, id="one-line-form-untokenised"),
        pytest.param(TOKEN_INSIDE_ONE_LINE, 0, id="one-line-form-tokenised"),
        # The token sits after </form>, so the form is still unprotected. This
        # is the case a count-based assertion accepts and this scanner must not.
        pytest.param(TOKEN_OUTSIDE_FORM, 1, id="token-outside-form-does-not-count"),
        pytest.param(HTMX_FORM, 0, id="htmx-verb-exempt"),
        pytest.param(HTMX_FORM_WITH_METHOD, 0, id="htmx-verb-with-method-exempt"),
        pytest.param(PREVENTED_FORM, 0, id="submit-prevented-exempt"),
        pytest.param(GET_FORM, 0, id="get-form-irrelevant"),
    ],
)
def test_scanner_classifies_each_form_shape(markup, expected):
    assert len(untokenised_post_forms(markup)) == expected


def test_misplaced_token_detector_finds_inert_tokens():
    assert misplaced_tokens(TOKEN_OUTSIDE_FORM) == 1
    assert misplaced_tokens(TOKEN_INSIDE_ONE_LINE) == 0


def test_balance_detector_reports_nesting_and_unclosed_forms():
    assert form_tag_balance(MULTILINE_FORM) == (1, 0)
    assert form_tag_balance('<form method="post">') == (1, 1)
    assert form_tag_balance("<form><form></form></form>") == (2, 0)


# --- repository guards --------------------------------------------------


def test_no_native_post_form_is_missing_a_csrf_token():
    offenders: dict[str, list[str]] = {}
    for path in sorted(TEMPLATES.rglob("*.html")):
        if _unprotected(path):
            continue
        missing = untokenised_post_forms(path.read_text(errors="ignore"))
        if missing:
            offenders[path.relative_to(ROOT).as_posix()] = missing

    assert not offenders, (
        "form POSTs without a CSRF token (these are rejected):\n"
        + "\n".join(
            f"  {name}: {', '.join(actions)}"
            for name, actions in sorted(offenders.items())
        )
    )


def test_tokens_are_placed_inside_their_form():
    stray = {
        path.relative_to(ROOT).as_posix(): count
        for path in sorted(TEMPLATES.rglob("*.html"))
        if (count := misplaced_tokens(path.read_text(errors="ignore")))
    }
    assert not stray, f"csrf_input included outside a form (inert): {stray}"


# Pre-existing nested-form defects, each a delete form inside an edit or bulk
# form. Nested forms are invalid HTML: the browser drops the inner one, so the
# delete button submits the OUTER form. On the GIS pages that means "Delete"
# saves the record instead of deleting it.
#
# They are listed rather than fixed here because they already carry CSRF tokens
# — they are not part of this change — and repairing them makes genuinely
# destructive actions start working, which needs its own review. This list must
# only ever shrink.
KNOWN_NESTED_FORMS = {
    "templates/admin/catalog/usage/charges.html",
    "templates/admin/gis/area_form.html",
    "templates/admin/gis/layer_form.html",
    "templates/admin/gis/location_form.html",
}


def test_form_tags_are_balanced_and_never_nested_within_a_template():
    """A form split across a macro or include boundary defeats the scanner.

    The scanner reasons about literal markup, so a `<form>` opened in one
    template and closed in another would let an untokenised POST through
    unseen. Requiring every template to open and close its own forms, and never
    nest them, keeps the scanner's view equal to the browser's.
    """
    problems: dict[str, str] = {}
    for path in sorted(TEMPLATES.rglob("*.html")):
        name = path.relative_to(ROOT).as_posix()
        max_depth, unclosed = form_tag_balance(path.read_text(errors="ignore"))
        if unclosed:
            problems[name] = f"{unclosed} unclosed <form> (split across templates?)"
        elif max_depth > 1 and name not in KNOWN_NESTED_FORMS:
            problems[name] = f"nested <form> (depth {max_depth})"
    assert not problems, "form structure defeats CSRF scanning:\n" + "\n".join(
        f"  {name}: {issue}" for name, issue in sorted(problems.items())
    )


def test_known_nested_form_list_only_shrinks():
    """Every allowlisted template must still actually be nested.

    If one is repaired the entry has to go, so the exemption cannot outlive the
    defect it documents.
    """
    stale = set()
    for name in KNOWN_NESTED_FORMS:
        path = ROOT / name
        if not path.exists():
            stale.add(f"{name} (missing)")
            continue
        max_depth, _ = form_tag_balance(path.read_text(errors="ignore"))
        if max_depth <= 1:
            stale.add(f"{name} (no longer nested)")
    assert not stale, f"remove from KNOWN_NESTED_FORMS: {sorted(stale)}"


def test_htmx_exemption_is_backed_by_base_template():
    """htmx forms are exempt only because base.html attaches the header.

    If that hook is removed the exemption silently becomes a hole, so this
    pins it.
    """
    base = (TEMPLATES / "base.html").read_text()
    assert "htmx:configRequest" in base
    assert "X-CSRF-Token" in base
    hook = base.split("htmx:configRequest", 1)[1][:400]
    assert "X-CSRF-Token" in hook, "the header must be set inside the hook"
