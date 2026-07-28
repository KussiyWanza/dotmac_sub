"""A named enum type must have exactly one creator across all migrations."""

from __future__ import annotations

import textwrap
from pathlib import Path

from scripts.architecture import sot_debt

_DUPLICATE = '''
"""A migration that creates a type and then lets a column create it again."""
import sqlalchemy as sa
from alembic import op

_STATE = sa.Enum("a", "b", name="widgetstate")


def upgrade() -> None:
    _STATE.create(op.get_bind(), checkfirst=True)
    op.create_table("widgets", sa.Column("state", _STATE, nullable=False))
'''

_SINGLE_OWNER = '''
"""The same migration with one owner: the column must not re-create the type."""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

_STATE = postgresql.ENUM("a", "b", name="widgetstate", create_type=False)


def upgrade() -> None:
    _STATE.create(op.get_bind(), checkfirst=True)
    op.create_table("widgets", sa.Column("state", _STATE, nullable=False))
'''


def _emitters(source: str, tmp_path: Path) -> list[str]:
    path = tmp_path / "999_example.py"
    path.write_text(textwrap.dedent(source), encoding="utf-8")
    return [
        f"{e.kind}:{e.enum_name}" for e in sot_debt._migration_enum_emitters_for(path)
    ]


def test_the_detector_still_recognises_the_pattern(tmp_path: Path) -> None:
    """Guard the guard: a scanner that silently stops matching proves nothing.

    Without this, a refactor that broke detection would turn both assertions
    below into vacuous passes and the baseline would freeze at whatever it
    happened to hold.
    """
    duplicate = _emitters(_DUPLICATE, tmp_path)
    assert duplicate.count("explicit:widgetstate") == 1
    assert duplicate.count("auto:widgetstate") == 1, (
        "the column declaration re-creates the type and must be counted"
    )

    single_owner = _emitters(_SINGLE_OWNER, tmp_path)
    assert single_owner == ["explicit:widgetstate"], (
        "create_type=False leaves exactly one owner and must not be flagged"
    )


def _format(entries: dict[tuple[str, str], int]) -> str:
    return "\n  ".join(
        f"{enum_name} {count} {path}"
        for (enum_name, path), count in sorted(entries.items())
    )


def test_no_new_or_expanded_duplicate_enum_creation() -> None:
    current = sot_debt.duplicated_migration_enums()
    baseline = sot_debt.read_count_baseline(sot_debt.MIGRATION_ENUM_BASELINE)
    expanded = {
        key: count for key, count in current.items() if count > baseline.get(key, 0)
    }

    assert not expanded, (
        "a named enum type can be created more than once. PostgreSQL rejects the "
        "second CREATE TYPE, and the fresh-database CI path cannot see it: 001 "
        "builds the schema from model metadata, so _safe_create_table skips the "
        "tables whose creation would emit it. Give the type one explicit checked "
        "owner and declare the columns with postgresql.ENUM(..., "
        "create_type=False); do not expand the baseline:\n  " + _format(expanded)
    )


def test_duplicate_enum_baseline_only_shrinks() -> None:
    current = sot_debt.duplicated_migration_enums()
    baseline = sot_debt.read_count_baseline(sot_debt.MIGRATION_ENUM_BASELINE)
    resolved = {
        key: baseline_count - current.get(key, 0)
        for key, baseline_count in baseline.items()
        if current.get(key, 0) < baseline_count
    }

    assert not resolved, (
        "duplicate enum-creation debt shrank; reduce or remove these baseline "
        "entries so the repair is permanent:\n  " + _format(resolved)
    )
