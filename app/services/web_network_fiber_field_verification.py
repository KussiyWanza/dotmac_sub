"""Typed web projection for the fiber field-verification worklist."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.services.list_query import (
    ListDefinition,
    ListFieldDefinition,
    ListQuery,
    PageMeta,
)
from app.services.network.fiber_topology_field_worklist import (
    FiberTopologyFieldWorklistReport,
    reconcile_fiber_field_worklist,
)

FIBER_FIELD_WORKLIST_LIST_DEFINITION = ListDefinition(
    key="fiber-field-verification-identities",
    fields=(
        ListFieldDefinition(
            key="evidence_priority",
            label="Evidence priority",
            sortable=True,
        ),
    ),
    default_sort="evidence_priority",
    default_sort_dir="asc",
    default_per_page=25,
    per_page_options=(25, 50, 100),
)


@dataclass(frozen=True, slots=True)
class FiberFieldWorklistPageQuery:
    """Normalized presentation-only pagination for the exhaustive report."""

    list_query: ListQuery


@dataclass(frozen=True, slots=True)
class FiberFieldWorkOrderView:
    work_order_public_id: str


@dataclass(frozen=True, slots=True)
class FiberFieldVerificationView:
    current_observation_count: int
    superseded_observation_count: int
    scope_states: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class FiberFieldWorklistRowView:
    asset_type: str
    blocker_codes: tuple[str, ...]
    content_sha256: str
    current_work_orders: tuple[FiberFieldWorkOrderView, ...]
    display_name: str | None
    external_id: str | None
    field_verification: FiberFieldVerificationView
    next_evidence_step: str
    priority: str
    priority_rank: int
    row_sha256: str
    source_profile: str
    source_system: str
    staged_feature_id: str
    superseded_work_orders: tuple[FiberFieldWorkOrderView, ...]
    verification_state: str


@dataclass(frozen=True, slots=True)
class FiberFieldWorklistSummary:
    report_sha256: str
    staged_feature_count: int
    source_batch_count: int
    needs_follow_up_count: int
    current_agreement_count: int
    rows_with_current_work_orders: int
    rows_with_superseded_work_orders: int
    state_counts: tuple[tuple[str, int], ...]
    priority_counts: tuple[tuple[str, int], ...]


@dataclass(frozen=True, slots=True)
class FiberFieldWorklistPage:
    """One typed HTML page derived from the unchanged complete report."""

    worklist: FiberFieldWorklistSummary
    rows: tuple[FiberFieldWorklistRowView, ...]
    list_query: ListQuery
    page_meta: PageMeta


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _integer(value: object) -> int:
    return int(str(value or 0))


def _work_orders(value: object) -> tuple[FiberFieldWorkOrderView, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(
        FiberFieldWorkOrderView(
            work_order_public_id=str(row.get("work_order_public_id") or ""),
        )
        for item in value
        if (row := _mapping(item)).get("work_order_public_id")
    )


def _row_view(row: Mapping[str, object]) -> FiberFieldWorklistRowView:
    evidence = _mapping(row.get("field_verification"))
    scope_states = _mapping(evidence.get("scope_states"))
    blocker_codes = row.get("blocker_codes")
    return FiberFieldWorklistRowView(
        asset_type=str(row.get("asset_type") or ""),
        blocker_codes=(
            tuple(str(code) for code in blocker_codes)
            if isinstance(blocker_codes, (list, tuple))
            else ()
        ),
        content_sha256=str(row.get("content_sha256") or ""),
        current_work_orders=_work_orders(row.get("current_work_orders")),
        display_name=_optional_text(row.get("display_name")),
        external_id=_optional_text(row.get("external_id")),
        field_verification=FiberFieldVerificationView(
            current_observation_count=_integer(
                evidence.get("current_observation_count") or 0
            ),
            superseded_observation_count=_integer(
                evidence.get("superseded_observation_count") or 0
            ),
            scope_states=tuple(
                (str(scope), str(state)) for scope, state in scope_states.items()
            ),
        ),
        next_evidence_step=str(row.get("next_evidence_step") or ""),
        priority=str(row.get("priority") or ""),
        priority_rank=_integer(row.get("priority_rank") or 0),
        row_sha256=str(row.get("row_sha256") or ""),
        source_profile=str(row.get("source_profile") or ""),
        source_system=str(row.get("source_system") or ""),
        staged_feature_id=str(row.get("staged_feature_id") or ""),
        superseded_work_orders=_work_orders(row.get("superseded_work_orders")),
        verification_state=str(row.get("verification_state") or ""),
    )


def _summary(report: FiberTopologyFieldWorklistReport) -> FiberFieldWorklistSummary:
    return FiberFieldWorklistSummary(
        report_sha256=report.report_sha256,
        staged_feature_count=report.staged_feature_count,
        source_batch_count=report.source_batch_count,
        needs_follow_up_count=report.needs_follow_up_count,
        current_agreement_count=report.current_agreement_count,
        rows_with_current_work_orders=report.rows_with_current_work_orders,
        rows_with_superseded_work_orders=report.rows_with_superseded_work_orders,
        state_counts=tuple(report.state_counts.items()),
        priority_counts=tuple(report.priority_counts.items()),
    )


def build_fiber_field_worklist_page_query(
    *,
    page: int,
    per_page: int | None,
) -> FiberFieldWorklistPageQuery:
    """Normalize raw web pagination without changing report semantics."""

    definition = FIBER_FIELD_WORKLIST_LIST_DEFINITION
    normalized_page = max(1, page)
    normalized_per_page = (
        per_page
        if per_page in definition.per_page_options
        else definition.default_per_page
    )
    return FiberFieldWorklistPageQuery(
        list_query=definition.build_query(
            search=None,
            filters={},
            sort_by=definition.default_sort,
            sort_dir=definition.default_sort_dir,
            page=normalized_page,
            per_page=normalized_per_page,
        )
    )


def get_fiber_field_worklist_page(
    *,
    db: Session,
    query: FiberFieldWorklistPageQuery,
) -> FiberFieldWorklistPage:
    """Slice the exhaustive report only after its counts and digest are built."""

    worklist = reconcile_fiber_field_worklist(db)
    page_meta = PageMeta.from_query(
        query.list_query,
        total_items=worklist.staged_feature_count,
    )
    list_query = query.list_query.with_page(page_meta.page)
    start = list_query.offset
    rows = tuple(
        _row_view(row) for row in worklist.rows[start : start + list_query.per_page]
    )
    return FiberFieldWorklistPage(
        worklist=_summary(worklist),
        rows=rows,
        list_query=list_query,
        page_meta=page_meta,
    )


__all__ = [
    "FIBER_FIELD_WORKLIST_LIST_DEFINITION",
    "FiberFieldVerificationView",
    "FiberFieldWorkOrderView",
    "FiberFieldWorklistPage",
    "FiberFieldWorklistPageQuery",
    "FiberFieldWorklistRowView",
    "FiberFieldWorklistSummary",
    "build_fiber_field_worklist_page_query",
    "get_fiber_field_worklist_page",
]
