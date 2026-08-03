from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_credit_count_and_rows_selector_precede_table_while_pagination_follows():
    template = (PROJECT_ROOT / "templates/admin/billing/credits.html").read_text(
        encoding="utf-8"
    )

    table_start = template.index("<table")
    assert template.index('aria-label="Credit notes per page"') < table_start
    assert template.index("Showing {{") < table_start
    assert template.index('aria-label="Credit note pagination"') > table_start
