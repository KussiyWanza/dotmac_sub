"""Architecture guards for customer timeline ownership and UI attribution."""

from pathlib import Path

from app.services.sot_registry.registry import service_relationship

ROOT = Path(__file__).resolve().parents[2]


def _source(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_customer_detail_delegates_timeline_projection_to_registered_owner():
    detail_service = _source("app/services/web_customer_details.py")
    projection = _source("app/services/customer_timeline.py")
    owner = service_relationship("ui.customer_timeline_projection")

    assert "customer_timeline_service.build_customer_timeline(" in detail_service
    assert "def _build_activity_items(" not in detail_service
    assert "AuditEvent" not in detail_service
    assert "class CustomerTimelineItem(TypedDict):" in projection
    assert owner.module == "app.services.customer_timeline"


def test_customer_timeline_ui_shows_attribution_result_and_evidence_without_controls():
    template = _source("templates/admin/customers/detail.html")

    assert "data-customer-timeline-item" in template
    assert "data-timeline-actor-kind" in template
    assert "data-timeline-actor-label" in template
    assert "data-timeline-result" in template
    assert "Audit details" in template
    assert "Security" in template
    assert "Actor not recorded" not in template
    assert "timeline-filter" not in template
    assert "timeline-search" not in template
    assert "Load more" not in template
