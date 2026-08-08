"""Refuse a migration whose parent is not the base branch's current head.

A migration's ``down_revision`` is chosen when a branch is created and validated
when it merges. With parallel branches that gap is days, so the head named at
authoring time is routinely stale by merge time — and a stale parent forks the
chain. Between 2026-08-06 and 2026-08-07 that produced five numbers claimed by
two or three pull requests each, seven hand renumbers, one orphaned stacked
child surfacing as ``KeyError`` at revision-map load, and one stacked merge
duplicating its parent's migrations.

``scripts/new_migration.py`` reads the head at *authoring* time, which narrows
the window without closing it. This gate closes it: it compares the branch
against its merge base and refuses when the new revisions do not chain onto the
base branch's head. The author rebases once, deterministically, instead of the
collision being discovered after both branches have merged.

See ``docs/adr/0008-migration-sequence-ownership.md``. This gate is the single
named owner of "the deployed schema has one coherent order"; behaviour tests
must not re-derive it.

Usage:
    python scripts/architecture/migration_sequence_gate.py
    python scripts/architecture/migration_sequence_gate.py --base origin/main
    python scripts/architecture/migration_sequence_gate.py --report-only
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

VERSIONS_DIR = "alembic/versions"
REVISION_RE = re.compile(r'^revision(?:\s*:[^=]+)?\s*=\s*["\']([^"\']+)["\']', re.M)
#: ``down_revision`` is a string, ``None``, or — for a merge revision — a tuple
#: of parents. Reading only the string form makes every merge revision look
#: unparented, which reports its real parents as spurious heads. This repo has
#: several (``139_merge_…``, ``173_merge_…``, ``193_merge_…``).
DOWN_RE = re.compile(
    r"^down_revision(?:\s*:[^=]+)?\s*=\s*(None|\([^)]*\)|\[[^\]]*\]|[\"'][^\"']+[\"'])",
    re.M | re.S,
)
_QUOTED = re.compile(r"[\"']([^\"']+)[\"']")


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], capture_output=True, text=True, check=True
    ).stdout


def _parse(source: str) -> tuple[str | None, tuple[str, ...]]:
    """Return ``(revision, parents)``; a merge revision has several parents."""
    rev = REVISION_RE.search(source)
    down = DOWN_RE.search(source)
    parents: tuple[str, ...] = ()
    if down:
        raw = down.group(1)
        if raw != "None":
            parents = tuple(_QUOTED.findall(raw))
    return (rev.group(1) if rev else None, parents)


def _revisions_at(ref: str) -> dict[str, tuple[str, ...]]:
    """revision -> parents for every migration present at ``ref``."""
    listing = _git("ls-tree", "-r", "--name-only", ref, "--", VERSIONS_DIR)
    revisions: dict[str, tuple[str, ...]] = {}
    for path in listing.splitlines():
        if not path.endswith(".py") or path.endswith("__init__.py"):
            continue
        rev, parents = _parse(_git("show", f"{ref}:{path}"))
        if rev:
            revisions[rev] = parents
    return revisions


def _revisions_in_tree(root: Path) -> dict[str, tuple[str, ...]]:
    revisions: dict[str, tuple[str, ...]] = {}
    for path in sorted((root / VERSIONS_DIR).glob("*.py")):
        if path.name == "__init__.py":
            continue
        rev, parents = _parse(path.read_text(encoding="utf-8"))
        if rev:
            revisions[rev] = parents
    return revisions


def _heads(revisions: dict[str, tuple[str, ...]]) -> set[str]:
    parents = {p for parents in revisions.values() for p in parents}
    return set(revisions) - parents


def check(base: str, root: Path) -> list[str]:
    """Return human-readable failures; empty means the branch is admissible."""
    try:
        merge_base = _git("merge-base", base, "HEAD").strip()
    except subprocess.CalledProcessError:
        return [f"Cannot resolve merge base against {base!r}."]

    base_revisions = _revisions_at(merge_base)
    branch_revisions = _revisions_in_tree(root)
    added = {
        rev: parents
        for rev, parents in branch_revisions.items()
        if rev not in base_revisions
    }
    if not added:
        return []

    current = _revisions_at(base)
    current_heads = _heads(current)
    failures: list[str] = []

    if len(current_heads) != 1:
        failures.append(
            f"{base} is not single-headed ({sorted(current_heads)}). "
            "Repair the base branch before adding a migration."
        )
        return failures

    head = current_heads.pop()

    # Exactly one added revision may chain onto the base head; the rest must
    # chain onto each other, so the branch contributes one linear segment.
    roots = [
        rev for rev, parents in added.items() if not any(p in added for p in parents)
    ]
    for rev in roots:
        down = added[rev][0] if added[rev] else None
        if down != head:
            failures.append(
                f"{rev} has down_revision {down!r} but {base} is now at {head!r}.\n"
                f"    Rebase onto {base} and repoint it — merging as-is forks the "
                f"chain, which alembic cannot upgrade past."
            )

    if len(roots) > 1:
        failures.append(
            f"This branch adds {len(roots)} independent migration roots "
            f"({sorted(roots)}). A branch must contribute one linear segment."
        )

    combined = {**current, **added}
    resulting = _heads(combined)
    if len(resulting) > 1:
        failures.append(
            f"Merging would leave {len(resulting)} heads: {sorted(resulting)}."
        )

    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="origin/dev", help="base branch ref")
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="print findings and exit 0 (shadow phase, see ADR 0008)",
    )
    args = parser.parse_args(argv)

    root = Path(_git("rev-parse", "--show-toplevel").strip())
    failures = check(args.base, root)

    if not failures:
        print(f"migration sequence gate: OK against {args.base}")
        return 0

    print(f"migration sequence gate: {len(failures)} finding(s)\n")
    for failure in failures:
        print(f"  - {failure}")
    print(
        "\nSee docs/adr/0008-migration-sequence-ownership.md. "
        "`make new-migration slug=...` allocates from the current head."
    )

    if args.report_only:
        print("\n(report-only: not failing the build — ADR 0008 shadow phase)")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
