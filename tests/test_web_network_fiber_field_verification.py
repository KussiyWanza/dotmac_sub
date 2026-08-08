from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from jinja2 import Environment, FileSystemLoader
from sqlalchemy.orm import Session

from app.services import web_network_fiber_field_verification as service
from app.services.list_query import PageMeta
from app.services.network.fiber_topology_field_worklist import (
    FiberTopologyFieldWorklistReport,
)


def _deny_permissions(*_args: object, **_kwargs: object) -> bool:
    return False


def _report(total: int) -> FiberTopologyFieldWorklistReport:
    rows = tuple(
        {
            "staged_feature_id": f"feature-{index:04d}",
            "priority_rank": index,
        }
        for index in range(total)
    )
    return FiberTopologyFieldWorklistReport(
        report_sha256="a" * 64,
        staged_feature_count=total,
        source_batch_count=3,
        needs_follow_up_count=total,
        current_agreement_count=0,
        rows_with_current_work_orders=0,
        rows_with_superseded_work_orders=0,
        state_counts={"unobserved": total},
        priority_counts={"p4_unobserved": total},
        asset_type_counts={"fiber_access_point": total},
        source_system_counts={"dotmac_osp_kmz": total},
        source_profile_counts={"dotmac_osp_kmz/test": total},
        rows=rows,
    )


def test_page_projection_slices_after_building_the_complete_report(monkeypatch):
    report = _report(1240)
    monkeypatch.setattr(service, "reconcile_fiber_field_worklist", lambda _db: report)
    query = service.build_fiber_field_worklist_page_query(page=2, per_page=25)

    page = service.get_fiber_field_worklist_page(
        db=MagicMock(spec=Session),
        query=query,
    )

    assert page.worklist.report_sha256 == "a" * 64
    assert page.worklist.staged_feature_count == 1240
    assert len(page.rows) == 25
    assert page.rows[0].staged_feature_id == "feature-0025"
    assert page.rows[-1].staged_feature_id == "feature-0049"
    assert page.page_meta.start_item == 26
    assert page.page_meta.end_item == 50
    assert page.page_meta.total_items == 1240


def test_page_query_has_only_the_approved_sizes_and_clamps_an_empty_page(monkeypatch):
    report = _report(0)
    monkeypatch.setattr(service, "reconcile_fiber_field_worklist", lambda _db: report)
    query = service.build_fiber_field_worklist_page_query(page=99, per_page=500)

    page = service.get_fiber_field_worklist_page(
        db=MagicMock(spec=Session),
        query=query,
    )

    assert query.list_query.definition.per_page_options == (25, 50, 100)
    assert query.list_query.per_page == 25
    assert page.list_query.page == 1
    assert page.rows == ()


def test_field_worklist_template_renders_the_typed_page_projection():
    report = _report(1240)
    query = service.build_fiber_field_worklist_page_query(page=2, per_page=25)
    page_meta = PageMeta.from_query(query.list_query, 1240)
    row = service.FiberFieldWorklistRowView(
        asset_type="fiber_access_point",
        blocker_codes=(),
        content_sha256="b" * 64,
        current_work_orders=(),
        display_name="FAT-PAGE-26",
        external_id="FAT-PAGE-26",
        field_verification=service.FiberFieldVerificationView(
            current_observation_count=0,
            superseded_observation_count=0,
            scope_states=(),
        ),
        next_evidence_step="Collect the first exact-source observation.",
        priority="p4_unobserved",
        priority_rank=4,
        row_sha256="c" * 64,
        source_profile="test",
        source_system="dotmac_osp_kmz",
        staged_feature_id="feature-0025",
        superseded_work_orders=(),
        verification_state="unobserved",
    )
    summary = service.FiberFieldWorklistSummary(
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
    templates = Path(__file__).resolve().parents[1] / "templates"
    environment = Environment(
        loader=FileSystemLoader(str(templates)),
        autoescape=True,
    )
    environment.globals["can"] = _deny_permissions

    html = environment.get_template(
        "admin/network/fiber/field_verification_worklist.html"
    ).render(
        active_menu="fiber",
        active_page="fiber-field-verification",
        current_user=None,
        list_query=query.list_query,
        page_meta=page_meta,
        request=SimpleNamespace(
            state=SimpleNamespace(csrf_token=""),
            url=SimpleNamespace(path="/admin/network/fiber-field-verification"),
        ),
        sidebar_stats={},
        worklist=summary,
        worklist_rows=(row,),
    )

    assert 'id="latest-staged-source-identities"' in html
    assert "FAT-PAGE-26" in html
    assert "Showing 26&ndash;50 of 1,240 identities" in html
    assert "page=3&amp;per_page=25#latest-staged-source-identities" in html
