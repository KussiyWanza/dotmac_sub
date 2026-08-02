"""Posting groups have exactly one constructing writer (ADR 0007 Phase 3).

Every producer participates through the typed staging API inside its own
owner command; nothing else may construct posting rows, so a parallel
subledger writer cannot appear silently.
"""

from __future__ import annotations

from pathlib import Path

_APP = Path(__file__).resolve().parents[2] / "app"
_OWNER = _APP / "services" / "billing" / "customer_subledger.py"


def _offenders(needles: tuple[str, ...], *, skip_models: bool = False) -> list[str]:
    hits: list[str] = []
    models = _APP / "models" / "customer_subledger.py"
    # The SoT registry cites table names as descriptive authoritative-input
    # text; it is documentation, not a write form.
    registry = _APP / "services" / "sot_relationships.py"
    for path in _APP.rglob("*.py"):
        if path == _OWNER or path == registry or (skip_models and path == models):
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("class "):
                continue
            if any(needle in stripped for needle in needles):
                hits.append(str(path.relative_to(_APP)))
                break
    return sorted(set(hits))


def test_posting_rows_are_constructed_only_by_the_subledger_owner() -> None:
    # Direct constructors, aliased imports, and SQLAlchemy write forms all
    # count as parallel writers.
    assert (
        _offenders(
            (
                "CustomerPostingGroup(",
                "CustomerPositionEffect(",
                "CustomerPostingGroup as ",
                "CustomerPositionEffect as ",
                "insert(CustomerPostingGroup",
                "insert(CustomerPositionEffect",
                "bulk_insert_mappings(CustomerPostingGroup",
                "bulk_insert_mappings(CustomerPositionEffect",
            )
        )
        == []
    ), (
        "posting rows are constructed only inside "
        "app/services/billing/customer_subledger.py; stage through the "
        "typed participant API instead."
    )
    # Raw table-name writes (INSERT INTO / text SQL) outside the owner and
    # the model definition.
    assert (
        _offenders(
            ("customer_posting_groups", "customer_position_effects"),
            skip_models=True,
        )
        == []
    ), "raw table access to posting tables outside the subledger owner."
