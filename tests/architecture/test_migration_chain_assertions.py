"""Chain tests must assert single-headedness, never a literal head revision.

``assert script.get_heads() == ["<revision>"]`` encodes "the chain never forks"
as "the head is exactly this revision", so **every** migration added afterwards
fails the test even though nothing is wrong. On 2026-08-06 one such line was
re-pinned seven times in a day across three files, and each re-pin cost the
author a CI round trip for no defect.

Assert what the test actually means:

    heads = script.get_heads()
    assert len(heads) == 1
    assert module.revision in {
        item.revision
        for item in script.iterate_revisions(heads[0], module.revision, inclusive=True)
    }

That proves the chain is single-headed *and* that this revision is in the head's
ancestry, which is the real invariant, and it keeps proving it as the chain grows.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TESTS_ROOT = REPO_ROOT / "tests"

#: ``get_heads()`` compared against anything, rather than measured. Catches
#: ``== ["x"]``, ``== ('x',)`` and ``!=`` alike.
PINNED_HEAD = re.compile(r"get_heads\(\)\s*[=!]=")

SELF = Path(__file__).name


def _offenders() -> list[str]:
    hits: list[str] = []
    for path in sorted(TESTS_ROOT.rglob("*.py")):
        if path.name == SELF:
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):  # pragma: no cover - unreadable file
            continue
        for lineno, line in enumerate(source.splitlines(), start=1):
            if PINNED_HEAD.search(line):
                hits.append(f"{path.relative_to(REPO_ROOT)}:{lineno}: {line.strip()}")
    return hits


def test_no_test_pins_the_alembic_head_to_a_literal_revision() -> None:
    offenders = _offenders()
    assert not offenders, (
        "These tests compare get_heads() to a literal, so every migration added "
        "after them fails for no defect:\n  "
        + "\n  ".join(offenders)
        + "\n\nAssert single-headedness plus ancestry instead:\n"
        "    heads = script.get_heads()\n"
        "    assert len(heads) == 1\n"
        "    assert module.revision in {\n"
        "        item.revision\n"
        "        for item in script.iterate_revisions(\n"
        "            heads[0], module.revision, inclusive=True\n"
        "        )\n"
        "    }"
    )
