"""Allocate an alembic revision from the current head instead of guessing it.

Migration numbers picked by hand at branch time go stale within hours on a repo
that merges this often, and every stale number fails in a different way:

1. Two branches claim the same number, so the chain forks into two heads and
   ``alembic upgrade`` refuses to run.
2. A renumber orphans any branch stacked on the old id, which surfaces as
   ``KeyError: '<old_revision>'`` when the revision map loads -- not as a merge
   conflict, so git merges it happily.
3. Merging a parent branch into a stacked child duplicates the parent's
   migrations under both the old and new numbers.

Reading the head at authoring time removes the guess. It does not remove the
race -- another branch can still land first -- but it makes the number correct
when written and wrong only when someone else merges, which is visible.

Usage:
    poetry run python scripts/new_migration.py add_widget_table
    poetry run python scripts/new_migration.py add_widget_table --message "Add widgets"
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

REPO_ROOT = Path(__file__).resolve().parent.parent
VERSIONS = REPO_ROOT / "alembic" / "versions"
SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)*$")
NUMBERED = re.compile(r"^(\d+)_")

TEMPLATE = '''"""{message}

Revision ID: {revision}
Revises: {down_revision}
Create Date: {created}
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "{revision}"
down_revision: str | None = "{down_revision}"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    raise NotImplementedError("write the upgrade")


def downgrade() -> None:
    raise NotImplementedError("write the downgrade")
'''


def _script_directory() -> ScriptDirectory:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    return ScriptDirectory.from_config(config)


def _resolve_head(script: ScriptDirectory) -> str:
    heads = script.get_heads()
    if len(heads) != 1:
        raise SystemExit(
            "Refusing to allocate: the chain is not single-headed.\n"
            f"  heads: {sorted(heads)}\n"
            "Repair the fork first — a new migration on a forked chain cannot "
            "be applied."
        )
    return heads[0]


def _next_number() -> int:
    numbers = [
        int(match.group(1))
        for path in VERSIONS.glob("*.py")
        if (match := NUMBERED.match(path.name))
    ]
    if not numbers:
        raise SystemExit(f"No numbered migrations found in {VERSIONS}")
    return max(numbers) + 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("slug", help="lower_snake_case name, e.g. add_widget_table")
    parser.add_argument(
        "--message",
        help="docstring summary; defaults to the slug with spaces",
    )
    args = parser.parse_args(argv)

    if not SLUG_PATTERN.fullmatch(args.slug):
        raise SystemExit(
            f"Invalid slug {args.slug!r}: use lower_snake_case, e.g. add_widget_table"
        )

    script = _script_directory()
    down_revision = _resolve_head(script)
    revision = f"{_next_number()}_{args.slug}"

    target = VERSIONS / f"{revision}.py"
    if target.exists():
        raise SystemExit(f"Refusing to overwrite existing {target}")

    target.write_text(
        TEMPLATE.format(
            message=args.message or args.slug.replace("_", " "),
            revision=revision,
            down_revision=down_revision,
            created=datetime.now(UTC).date().isoformat(),
        )
    )

    print(f"created {target.relative_to(REPO_ROOT)}")
    print(f"  revision:      {revision}")
    print(f"  down_revision: {down_revision}")
    print(
        "\nRe-run this check immediately before pushing: if another migration "
        "merged\nin the meantime, renumber rather than branching the chain."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
