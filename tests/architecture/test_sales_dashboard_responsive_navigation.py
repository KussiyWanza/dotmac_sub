from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_sales_sections_are_visible_without_dropdown_below_desktop():
    source = (ROOT / "templates" / "admin" / "sales" / "dashboard.html").read_text(
        encoding="utf-8"
    )

    assert 'aria-label="Sales sections"' in source
    assert 'class="flex flex-wrap items-center gap-2 lg:hidden"' in source
    assert 'class="relative hidden lg:block"' in source
    for label in ("Leads", "Quotes", "Sales Orders"):
        assert source.count(f">{label}</a>") == 2
