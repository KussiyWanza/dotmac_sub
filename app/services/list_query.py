"""Canonical contracts for server-owned list/query projections.

List owners declare their searchable, filterable, and sortable capabilities in a
``ListDefinition``.  Web routes pass raw request values to that definition and
templates consume the resulting ``ListQuery`` and ``PageMeta`` instead of
reconstructing query-string or pagination rules independently.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Literal, cast
from urllib.parse import urlencode

SortDirection = Literal["asc", "desc"]


@dataclass(frozen=True, slots=True)
class ListFieldDefinition:
    """One declared list capability.

    A field may represent a stored column or a server-owned virtual capability,
    such as a search term that spans several stored columns.
    """

    key: str
    label: str
    searchable: bool = False
    filterable: bool = False
    sortable: bool = False


@dataclass(frozen=True, slots=True)
class ListDefinition:
    """Authoritative query capabilities and defaults for one list resource."""

    key: str
    fields: tuple[ListFieldDefinition, ...]
    default_sort: str
    default_sort_dir: SortDirection = "desc"
    default_per_page: int = 25
    per_page_options: tuple[int, ...] = (10, 25, 50, 100)

    def __post_init__(self) -> None:
        if not self.key.strip():
            raise ValueError("List definition key is required")

        field_keys = tuple(field.key for field in self.fields)
        if len(set(field_keys)) != len(field_keys):
            raise ValueError(f"Duplicate fields in list definition: {self.key}")
        if self.default_sort not in self.sortable_keys:
            raise ValueError(
                f"Default sort is not sortable for {self.key}: {self.default_sort}"
            )
        if self.default_per_page not in self.per_page_options:
            raise ValueError(
                f"Default page size is not allowed for {self.key}: "
                f"{self.default_per_page}"
            )
        if not self.per_page_options or any(size < 1 for size in self.per_page_options):
            raise ValueError(f"Page sizes must be positive for {self.key}")

    @property
    def searchable_keys(self) -> tuple[str, ...]:
        return tuple(field.key for field in self.fields if field.searchable)

    @property
    def filterable_keys(self) -> tuple[str, ...]:
        return tuple(field.key for field in self.fields if field.filterable)

    @property
    def sortable_keys(self) -> tuple[str, ...]:
        return tuple(field.key for field in self.fields if field.sortable)

    def build_query(
        self,
        *,
        search: str | None,
        filters: Mapping[str, object | None],
        sort_by: str | None = None,
        sort_dir: str | None = None,
        page: int = 1,
        per_page: int | None = None,
    ) -> ListQuery:
        if page < 1:
            raise ValueError("page must be at least 1")

        effective_per_page = per_page or self.default_per_page
        if effective_per_page not in self.per_page_options:
            allowed = ", ".join(str(size) for size in self.per_page_options)
            raise ValueError(f"per_page must be one of: {allowed}")

        effective_sort = str(sort_by or self.default_sort).strip()
        if effective_sort not in self.sortable_keys:
            raise ValueError(f"Unsupported sort field for {self.key}: {effective_sort}")

        effective_direction = str(sort_dir or self.default_sort_dir).strip().lower()
        if effective_direction not in {"asc", "desc"}:
            raise ValueError("sort_dir must be asc or desc")

        unknown_filters = set(filters) - set(self.filterable_keys)
        if unknown_filters:
            names = ", ".join(sorted(unknown_filters))
            raise ValueError(f"Unsupported filters for {self.key}: {names}")

        normalized_filters = tuple(
            (key, normalized)
            for key in self.filterable_keys
            if (normalized := str(filters.get(key) or "").strip())
        )
        normalized_search = str(search or "").strip() or None

        return ListQuery(
            definition=self,
            search=normalized_search,
            filters=normalized_filters,
            sort_by=effective_sort,
            sort_dir=cast(SortDirection, effective_direction),
            page=page,
            per_page=effective_per_page,
        )


@dataclass(frozen=True, slots=True)
class ListQuery:
    """Normalized, URL-serializable state for one list request."""

    definition: ListDefinition
    search: str | None
    filters: tuple[tuple[str, str], ...]
    sort_by: str
    sort_dir: SortDirection
    page: int
    per_page: int

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.per_page

    def filter_value(self, key: str) -> str | None:
        return dict(self.filters).get(key)

    def with_page(self, page: int) -> ListQuery:
        if page < 1:
            raise ValueError("page must be at least 1")
        return replace(self, page=page)

    def with_sort(self, sort_by: str, sort_dir: SortDirection) -> ListQuery:
        if sort_by not in self.definition.sortable_keys:
            raise ValueError(
                f"Unsupported sort field for {self.definition.key}: {sort_by}"
            )
        return replace(self, sort_by=sort_by, sort_dir=sort_dir, page=1)

    def with_filters(self, overrides: Mapping[str, object | None]) -> ListQuery:
        """Replace declared filters and reset pagination to the first page."""

        unknown_filters = set(overrides) - set(self.definition.filterable_keys)
        if unknown_filters:
            names = ", ".join(sorted(unknown_filters))
            raise ValueError(f"Unsupported filters for {self.definition.key}: {names}")
        values = dict(self.filters)
        for key, value in overrides.items():
            normalized = str(value or "").strip()
            if normalized:
                values[key] = normalized
            else:
                values.pop(key, None)
        filters = tuple(
            (key, values[key])
            for key in self.definition.filterable_keys
            if key in values
        )
        return replace(self, filters=filters, page=1)

    def with_per_page(self, per_page: int) -> ListQuery:
        """Change the declared page size and reset pagination."""

        if per_page not in self.definition.per_page_options:
            allowed = ", ".join(str(size) for size in self.definition.per_page_options)
            raise ValueError(f"per_page must be one of: {allowed}")
        return replace(self, per_page=per_page, page=1)

    def params(
        self,
        *,
        page: int | None = None,
        sort_by: str | None = None,
        sort_dir: SortDirection | None = None,
        filters: Mapping[str, object | None] | None = None,
        per_page: int | None = None,
    ) -> tuple[tuple[str, str], ...]:
        effective = self
        if filters is not None:
            effective = effective.with_filters(filters)
        if sort_by is not None or sort_dir is not None:
            effective = effective.with_sort(
                sort_by or effective.sort_by,
                sort_dir or effective.sort_dir,
            )
        if per_page is not None:
            effective = effective.with_per_page(per_page)
        if page is not None:
            effective = effective.with_page(page)

        params: list[tuple[str, str]] = []
        if effective.search:
            params.append(("search", effective.search))
        params.extend(effective.filters)
        params.extend(
            (
                ("sort", effective.sort_by),
                ("dir", effective.sort_dir),
                ("page", str(effective.page)),
                ("per_page", str(effective.per_page)),
            )
        )
        return tuple(params)

    def url(
        self,
        base_url: str,
        *,
        page: int | None = None,
        sort_by: str | None = None,
        sort_dir: SortDirection | None = None,
        filters: Mapping[str, object | None] | None = None,
        per_page: int | None = None,
    ) -> str:
        return f"{base_url}?{urlencode(self.params(page=page, sort_by=sort_by, sort_dir=sort_dir, filters=filters, per_page=per_page))}"


def request_needs_canonicalization(
    list_query: ListQuery,
    *,
    search: str | None = None,
    filters: Mapping[str, object | None] | None = None,
    sort_by: str | None = None,
    sort_dir: str | None = None,
    page: int = 1,
    per_page: int | None = None,
) -> bool:
    """Return whether raw request state differs from the owner's projection.

    List owners use this after stale values and out-of-range pages have been
    normalized. Routes can then redirect to one stable, deep-linkable URL
    instead of rendering rows for one page while exposing another page in the
    query contract.
    """

    if page != list_query.page:
        return True
    if per_page is not None and per_page != list_query.per_page:
        return True
    if sort_by is not None and sort_by != list_query.sort_by:
        return True
    if sort_dir is not None and sort_dir != list_query.sort_dir:
        return True
    if search is not None:
        normalized_search = str(search).strip() or None
        if normalized_search != list_query.search:
            return True
    for key, raw_value in (filters or {}).items():
        if raw_value is None:
            continue
        normalized = str(raw_value).strip() or None
        if normalized != list_query.filter_value(key):
            return True
    return False


@dataclass(frozen=True, slots=True)
class PageMeta:
    """Canonical page metadata derived after filtering and before projection."""

    page: int
    per_page: int
    total_items: int
    total_pages: int
    start_item: int
    end_item: int
    has_previous: bool
    has_next: bool
    total_is_exact: bool = True

    @classmethod
    def from_query(
        cls,
        query: ListQuery,
        total_items: int,
        *,
        total_is_exact: bool = True,
    ) -> PageMeta:
        safe_total = max(0, int(total_items))
        total_pages = max(1, (safe_total + query.per_page - 1) // query.per_page)
        page = min(query.page, total_pages)
        start_item = (page - 1) * query.per_page + 1 if safe_total else 0
        end_item = min(page * query.per_page, safe_total) if safe_total else 0
        return cls(
            page=page,
            per_page=query.per_page,
            total_items=safe_total,
            total_pages=total_pages,
            start_item=start_item,
            end_item=end_item,
            has_previous=page > 1,
            has_next=page < total_pages,
            total_is_exact=total_is_exact,
        )

    @property
    def navigation(self) -> tuple[int | None, ...]:
        """Compact page-number sequence; ``None`` represents one ellipsis."""

        visible = {
            1,
            self.total_pages,
            self.page - 1,
            self.page,
            self.page + 1,
        }
        ordered = sorted(page for page in visible if 1 <= page <= self.total_pages)
        navigation: list[int | None] = []
        previous: int | None = None
        for page in ordered:
            if previous is not None and page - previous > 1:
                navigation.append(None)
            navigation.append(page)
            previous = page
        return tuple(navigation)
