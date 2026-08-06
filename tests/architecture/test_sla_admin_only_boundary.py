"""SLA scoring evidence remains restricted to the admin application."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _source(path: str) -> str:
    return (ROOT / path).read_text()


def test_review_route_is_admin_only_and_permission_gated():
    routes = _source("app/web/admin/customers.py")

    assert '"/{subscriber_id}/subscriptions/{subscription_id}/sla-review"' in routes
    assert 'dependencies=[Depends(require_permission("customer:read"))]' in routes
    assert '"admin/customers/sla_review.html"' in routes


def test_customer_portal_and_me_api_cannot_expose_sla_evidence():
    forbidden = (
        "sla_admin_review",
        "sla-review",
        "review_admin_period",
        "SlaPeriodScoreRevision",
        "sla_period_score_revisions",
    )
    paths = [
        *sorted((ROOT / "app/web/customer").rglob("*.py")),
        *sorted((ROOT / "templates/customer").rglob("*.html")),
        ROOT / "app/api/me.py",
    ]

    violations: list[str] = []
    for path in paths:
        source = path.read_text()
        for token in forbidden:
            if token in source:
                violations.append(f"{path.relative_to(ROOT)}: {token}")
    assert violations == []


def test_candidate_selector_is_structurally_inert():
    settings = _source("app/services/settings_spec.py")
    owner = _source("app/services/sla_admin_review.py")

    selector = settings.split('key="sla_admin_display_authority"', 1)[1].split("),", 1)[
        0
    ]
    assert 'default="legacy_availability"' in selector
    assert 'allowed={"legacy_availability"}' in selector
    assert "candidate_display_not_armed" in owner
    assert "authority is not SlaAdminDisplayAuthority.legacy_availability" in owner


def test_ordinary_customer_card_renders_only_the_selected_projection():
    panel = _source("templates/admin/customers/_service_impact_panel.html")

    assert "card.service_level.availability_percent" in panel
    assert "candidate.measured_availability_percent" not in panel
    assert "legacy.availability_percent" not in panel
    assert "Review SLA candidate" in panel
