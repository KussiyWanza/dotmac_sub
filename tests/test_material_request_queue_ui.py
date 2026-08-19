from pathlib import Path


def test_material_request_queue_filters_and_dark_table_are_readable():
    source = Path("templates/admin/material_requests/index.html").read_text(
        encoding="utf-8"
    )
    filter_form = source.split('<form method="get"', maxsplit=1)[1].split(
        "</form>", maxsplit=1
    )[0]
    filter_button = filter_form.split('<button type="submit"', maxsplit=1)[1].split(
        "</button>", maxsplit=1
    )[0]

    assert 'class="mb-5 flex items-end gap-2 sm:gap-3"' in source
    assert filter_form.count("min-w-0 flex-1") == 2
    assert "shrink-0" in filter_button
    assert "px-4 py-2.5" in filter_button
    assert "w-full" not in filter_button
    assert "dark:text-slate-200" in source
    assert "dark:text-white" in source
    assert "dark:text-slate-300" in source
    assert "dark:hover:bg-slate-800/50" in source
    assert '<tr class="hover:bg-slate-50">' not in source
