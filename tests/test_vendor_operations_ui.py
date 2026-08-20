from pathlib import Path

TEMPLATE = Path("templates/admin/vendors/operations.html")


def test_vendor_operations_search_and_procurement_controls_are_visibly_spaced():
    template = TEMPLATE.read_text(encoding="utf-8")

    assert 'class="mt-4 flex flex-col gap-3 sm:flex-row"' in template
    assert 'id="vendor-operations-search"' in template
    search_input = template.split('id="vendor-operations-search"', maxsplit=1)[1]
    search_input = search_input.split(">", maxsplit=1)[0]
    assert "pl-3" in search_input
    assert 'class="grid gap-4 border-t border-slate-100' in template
    assert template.count("border border-slate-400") >= 4
    assert template.count("focus:border-teal-500") >= 4
    assert 'name="mode"' in template
    assert 'name="vendor_id"' in template
    assert 'name="bidding_close_at"' in template
    bidding_close_input = template.split('name="bidding_close_at"', maxsplit=1)[1]
    bidding_close_input = bidding_close_input.split(">", maxsplit=1)[0]
    assert "pr-10" in bidding_close_input
    calendar_icon = template.split("data-bidding-close-calendar-icon", maxsplit=1)[
        1
    ].split(">", maxsplit=1)[0]
    assert "pointer-events-none" in calendar_icon
    assert "dark:text-slate-300" in calendar_icon
    assert "dark:bg-slate-900" in calendar_icon


def test_draft_procurement_and_advance_cards_share_a_responsive_row():
    template = TEMPLATE.read_text(encoding="utf-8")

    assert 'class="grid items-start gap-6 lg:grid-cols-2"' in template
    assert template.count("Vendor advances awaiting review") == 1
    assert "{% if show_field_reviews or show_advance_reviews %}" in template


def test_draft_procurement_rows_expand_to_reveal_the_existing_form():
    template = TEMPLATE.read_text(encoding="utf-8")

    assert '<details name="draft-procurement-project"' in template
    assert "group-open:rotate-180" in template
    assert (
        'action="/admin/vendors/operations/projects/{{ project.id }}/procurement"'
        in template
    )
    assert (
        'href="/admin/projects/{{ project.project.number or project.project.id }}"'
        in template
    )
    assert 'onclick="event.stopPropagation()"' in template
    assert ">View project</a>" in template
