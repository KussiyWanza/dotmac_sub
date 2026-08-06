"""Admin-only SLA shadow review and inert display-selector contracts."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.models.network_monitoring import AvailabilitySnapshot
from app.models.sla import SlaPeriodScoreRevision
from app.services import customer_service_level as service_level
from app.services import settings_spec, web_customer_sla
from app.services.sla_admin_review import (
    SlaAdminDisplayAuthority,
    SlaAdminReviewError,
    SlaAdminReviewQuery,
    SlaDiscrepancyKind,
)
from app.services.topology import customer_availability as legacy_owner
from app.web.admin.customers import templates

START = datetime(2026, 6, 30, 23, 0, tzinfo=UTC)  # July in Africa/Lagos
END = datetime(2026, 7, 31, 23, 0, tzinfo=UTC)
EVALUATED_AT = END + timedelta(days=2)
PERIOD_SECONDS = int((END - START).total_seconds())


def _query(subscription, **changes) -> SlaAdminReviewQuery:
    values = {
        "subscriber_id": subscription.subscriber_id,
        "subscription_id": subscription.id,
        "period_start": START,
        "period_end": END,
        "evaluated_at": EVALUATED_AT,
    }
    values.update(changes)
    return SlaAdminReviewQuery(**values)


def _score(
    db,
    subscription,
    *,
    percent: Decimal | None = Decimal("99.5000"),
    complete: bool = True,
    issues: tuple[str, ...] = (),
) -> SlaPeriodScoreRevision:
    row = SlaPeriodScoreRevision(
        subscription_id=subscription.id,
        period_start=START,
        period_end=END,
        evaluated_at=END,
        revision=1,
        supersedes_id=None,
        eligible_seconds=PERIOD_SECONDS,
        unavailable_seconds=13392,
        excluded_seconds=0,
        unknown_seconds=0 if complete else 3600,
        verdict="passing" if complete else "unavailable",
        evidence_complete=complete,
        completeness_issues=list(issues),
        availability_lower_bound_percent=percent,
        availability_upper_bound_percent=(percent if complete else Decimal("100.0000")),
        policy_segments=[],
        policy_version_ids=[],
        outage_interval_ids=[],
        lifecycle_evidence_ids=[],
        evidence_digest=f"sha256:{uuid.uuid4().hex * 2}",
        recorded_by="operator:pytest",
        command_id=uuid.uuid4(),
        command_idempotency_key=f"sla-review-{uuid.uuid4()}",
        correlation_id=uuid.uuid4(),
    )
    db.add(row)
    db.flush()
    return row


def _legacy_report(*, downtime: int = 13392, observed_days: int = 31):
    element = legacy_owner.ServingElement(
        element_type="pop_site",
        element_id=uuid.uuid4(),
        label="POP test",
        role="Base station",
    )
    return legacy_owner.CustomerAvailability(
        period_days=31,
        period_start=START,
        period_end=END,
        period_seconds=PERIOD_SECONDS,
        serving_elements=[element],
        infrastructure_downtime_seconds=downtime,
        infrastructure_observed_days=observed_days,
    )


@pytest.fixture(autouse=True)
def _legacy_display_is_inert(monkeypatch):
    monkeypatch.setattr(
        settings_spec,
        "resolve_string",
        lambda db, domain, key: SlaAdminDisplayAuthority.legacy_availability.value,
    )


def test_selector_spec_is_one_valued_and_candidate_cannot_be_armed(
    db_session, monkeypatch
):
    from app.models.domain_settings import SettingDomain

    spec = settings_spec.get_spec(
        SettingDomain.subscriber, "sla_admin_display_authority"
    )
    assert spec is not None
    assert spec.default == "legacy_availability"
    assert spec.allowed == {"legacy_availability"}

    monkeypatch.setattr(
        settings_spec,
        "resolve_string",
        lambda db, domain, key: "customer_service_level",
    )
    with pytest.raises(SlaAdminReviewError) as caught:
        service_level.resolve_admin_display_authority(db_session)
    assert caught.value.code.endswith("candidate_display_not_armed")


def test_exact_closed_period_match_is_classified_without_a_tolerance(
    db_session, subscription, monkeypatch
):
    _score(db_session, subscription)
    monkeypatch.setattr(
        legacy_owner, "customer_availability", lambda *args, **kwargs: _legacy_report()
    )

    review = service_level.review_admin_period(db_session, _query(subscription))

    assert review.discrepancy is SlaDiscrepancyKind.exact_match
    assert review.delta_percent == Decimal("0.000")
    assert review.candidate is not None
    assert review.candidate.measured_availability_percent == Decimal("99.5000")
    assert review.display.authority is SlaAdminDisplayAuthority.legacy_availability
    assert review.ready_for_cutover is False
    assert review.cutover_blockers == ("candidate_display_not_armed",)


def test_any_nonzero_delta_remains_unreviewed(db_session, subscription, monkeypatch):
    _score(db_session, subscription, percent=Decimal("99.4000"))
    monkeypatch.setattr(
        legacy_owner, "customer_availability", lambda *args, **kwargs: _legacy_report()
    )

    review = service_level.review_admin_period(db_session, _query(subscription))

    assert review.discrepancy is SlaDiscrepancyKind.unreviewed_difference
    assert review.delta_percent == Decimal("-0.100")
    assert "unreviewed_difference" in review.cutover_blockers


def test_sub_thousandth_delta_is_not_rounded_into_an_exact_match(
    db_session, subscription, monkeypatch
):
    _score(db_session, subscription, percent=Decimal("99.5001"))
    monkeypatch.setattr(
        legacy_owner, "customer_availability", lambda *args, **kwargs: _legacy_report()
    )

    review = service_level.review_admin_period(db_session, _query(subscription))

    assert review.discrepancy is SlaDiscrepancyKind.unreviewed_difference
    assert review.delta_percent == Decimal("0.0001")


def test_incomplete_candidate_exposes_bounds_and_never_compares_a_percentage(
    db_session, subscription, monkeypatch
):
    _score(
        db_session,
        subscription,
        percent=Decimal("99.1000"),
        complete=False,
        issues=("monitoring_coverage_incomplete",),
    )
    monkeypatch.setattr(
        legacy_owner, "customer_availability", lambda *args, **kwargs: _legacy_report()
    )

    review = service_level.review_admin_period(db_session, _query(subscription))

    assert review.discrepancy is SlaDiscrepancyKind.candidate_incomplete
    assert review.delta_percent is None
    assert review.candidate is not None
    assert review.candidate.measured_availability_percent is None
    assert review.candidate.completeness_issues == ("monitoring_coverage_incomplete",)


def test_missing_candidate_is_not_mislabeled_as_legacy_parity(
    db_session, subscription, monkeypatch
):
    monkeypatch.setattr(
        legacy_owner, "customer_availability", lambda *args, **kwargs: _legacy_report()
    )

    review = service_level.review_admin_period(db_session, _query(subscription))

    assert review.discrepancy is SlaDiscrepancyKind.missing_candidate_score
    assert review.candidate is None
    assert review.delta_percent is None


def test_resolved_path_without_snapshots_is_legacy_unavailable(
    db_session, subscription, monkeypatch
):
    _score(db_session, subscription)
    report = _legacy_report(observed_days=0)
    assert report.has_infrastructure_coverage is False
    monkeypatch.setattr(
        legacy_owner, "customer_availability", lambda *args, **kwargs: report
    )

    review = service_level.review_admin_period(db_session, _query(subscription))

    assert review.discrepancy is SlaDiscrepancyKind.legacy_unavailable
    assert review.legacy.availability_percent is None
    assert review.delta_percent is None


def test_partial_legacy_period_is_unavailable(db_session, subscription, monkeypatch):
    _score(db_session, subscription)
    report = _legacy_report(observed_days=30)
    monkeypatch.setattr(
        legacy_owner, "customer_availability", lambda *args, **kwargs: report
    )

    review = service_level.review_admin_period(db_session, _query(subscription))

    assert review.discrepancy is SlaDiscrepancyKind.legacy_unavailable
    assert review.legacy.observed_days == 30
    assert review.legacy.expected_days == 31
    assert review.delta_percent is None


def test_legacy_period_end_is_exclusive(db_session, subscription, monkeypatch):
    element = legacy_owner.ServingElement(
        element_type="pop_site",
        element_id=uuid.uuid4(),
        label="POP boundary",
        role="Base station",
    )
    monkeypatch.setattr(
        legacy_owner,
        "_serving_elements",
        lambda db, sub: ([element], None),
    )
    db_session.add(
        AvailabilitySnapshot(
            element_type=element.element_type,
            element_id=element.element_id,
            snapshot_date=END,
            uptime_percent=95.0,
            downtime_seconds=3600,
            window_seconds=86400,
            incident_count=1,
        )
    )
    db_session.flush()

    report = legacy_owner.customer_availability(
        db_session, subscription, days=31, now=END
    )

    assert report.infrastructure_downtime_seconds == 0
    assert report.infrastructure_observed_days == 0
    assert report.has_infrastructure_coverage is False


def test_review_fails_closed_outside_the_customer_subscription_scope(
    db_session, subscription
):
    with pytest.raises(SlaAdminReviewError) as caught:
        service_level.review_admin_period(
            db_session,
            _query(subscription, subscriber_id=uuid.uuid4()),
        )
    assert caught.value.code.endswith("unknown_review_subscription")


def test_review_refuses_open_or_non_calendar_periods(db_session, subscription):
    with pytest.raises(SlaAdminReviewError) as open_period:
        service_level.review_admin_period(
            db_session,
            _query(subscription, period_end=EVALUATED_AT + timedelta(days=1)),
        )
    assert open_period.value.code.endswith("review_period_not_closed")

    with pytest.raises(SlaAdminReviewError) as partial_period:
        service_level.review_admin_period(
            db_session,
            _query(subscription, period_start=START + timedelta(days=1)),
        )
    assert partial_period.value.code.endswith("invalid_review_period")


def test_web_projection_defaults_to_the_latest_closed_lagos_month(
    db_session, subscription, monkeypatch
):
    _score(db_session, subscription)
    monkeypatch.setattr(
        legacy_owner, "customer_availability", lambda *args, **kwargs: _legacy_report()
    )

    page = web_customer_sla.build_sla_admin_review_page(
        db_session,
        subscriber_id=subscription.subscriber_id,
        subscription_id=subscription.id,
        period=None,
        now=datetime(2026, 8, 6, 12, 0, tzinfo=UTC),
    )

    assert page.selected_period == "2026-07"
    assert page.period_label == "July 2026"
    assert page.review.subscription_id == subscription.id
    assert page.customer_url.endswith(str(subscription.subscriber_id))

    request = SimpleNamespace(
        state=SimpleNamespace(
            csrf_token="pytest-csrf", auth={"permission_keys": {"customer:read"}}
        ),
        query_params={},
        headers={},
        cookies={},
        url=SimpleNamespace(path="/admin/customers/test/subscriptions/test/sla-review"),
        session={},
        client=None,
        scope={},
        url_for=lambda *args, **kwargs: "/",
    )
    html = templates.env.get_template("admin/customers/sla_review.html").render(
        request=request,
        active_page="customers",
        active_menu="customers",
        current_user={"name": "SLA reviewer", "email": "reviewer@example.test"},
        sidebar_stats={},
        sla_page=page,
        sla_humanize=web_customer_sla.humanize_seconds,
    )
    assert "Restricted staff evidence" in html
    assert "not a customer SLA surface" in html
    assert "Exact match" in html
    assert "99.5000%" in html
