"""Render tests for the reusable list_query macros."""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from app.services.list_query import (
    ListDefinition,
    ListFieldDefinition,
    ListQuery,
    PageMeta,
)

TEMPLATES = Path(__file__).resolve().parents[1] / "templates"

DEFINITION = ListDefinition(
    key="referrals",
    fields=(
        ListFieldDefinition("created_at", "Created", sortable=True),
        ListFieldDefinition("status", "Status", filterable=True, sortable=True),
    ),
    default_sort="created_at",
)


def _query(*, sort_by: str, sort_dir: str, page: int = 3) -> ListQuery:
    return DEFINITION.build_query(
        search=None,
        filters={"status": "pending"},
        sort_by=sort_by,
        sort_dir=sort_dir,
        page=page,
        per_page=25,
    )


def _environment() -> Environment:
    return Environment(loader=FileSystemLoader(str(TEMPLATES)), autoescape=True)


def _render_sort(sort_by: str, sort_dir: str, key: str) -> str:
    tmpl = _environment().from_string(
        "{% from 'components/ui/list_macros.html' import sort_header %}"
        "{{ sort_header(lq, '/admin/referrals', key, 'Created', entity='referrals') }}"
    )
    return tmpl.render(lq=_query(sort_by=sort_by, sort_dir=sort_dir), key=key)


def test_active_descending_column_toggles_to_ascending_and_marks_aria_sort():
    html = _render_sort("created_at", "desc", "created_at")
    assert 'aria-sort="descending"' in html
    assert (
        "/admin/referrals?status=pending&amp;sort=created_at&amp;dir=asc"
        "&amp;page=1&amp;per_page=25"
    ) in html
    assert "Sort referrals by created ascending" in html
    assert "group-focus-visible:opacity-100" not in html  # active icon stays visible


def test_inactive_column_is_aria_sort_none_and_links_ascending():
    html = _render_sort("created_at", "desc", "status")
    assert 'aria-sort="none"' in html
    assert (
        "/admin/referrals?status=pending&amp;sort=status&amp;dir=asc"
        "&amp;page=1&amp;per_page=25"
    ) in html
    assert "group-focus-visible:opacity-100" in html


def test_list_pagination_preserves_query_state_and_announces_results():
    query = _query(sort_by="created_at", sort_dir="desc", page=2)
    page_meta = PageMeta.from_query(query, 80)
    tmpl = _environment().from_string(
        "{% from 'components/ui/list_macros.html' import list_pagination %}"
        "{{ list_pagination(lq, page_meta, '/admin/referrals', entity='referrals') }}"
    )

    html = tmpl.render(lq=query, page_meta=page_meta)

    assert 'role="status" aria-live="polite"' in html
    assert "Showing 26 to 50 of 80 referrals" in html
    assert 'name="status" value="pending"' in html
    assert 'name="sort" value="created_at"' in html
    assert 'name="dir" value="desc"' in html
    assert 'name="per_page"' in html
    assert (
        "/admin/referrals?status=pending&amp;sort=created_at&amp;dir=desc"
        "&amp;page=3&amp;per_page=25"
    ) in html


def test_list_pagination_can_return_to_an_anchored_section():
    query = _query(sort_by="created_at", sort_dir="desc", page=2)
    page_meta = PageMeta.from_query(query, 1240)
    tmpl = _environment().from_string(
        "{% from 'components/ui/list_macros.html' import list_pagination %}"
        "{{ list_pagination(lq, page_meta, '/admin/referrals', "
        "entity='identities', anchor='latest-identities', compact_range=true) }}"
    )

    html = tmpl.render(lq=query, page_meta=page_meta)

    assert "Showing 26&ndash;50 of 1,240 identities" in html
    assert 'action="/admin/referrals#latest-identities"' in html
    assert "page=3&amp;per_page=25#latest-identities" in html
