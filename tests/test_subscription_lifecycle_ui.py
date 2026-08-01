"""Admin UI contract tests for canonical subscription lifecycle actions."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from jinja2 import Environment

from app.models.catalog import BillingMode, Subscription, SubscriptionStatus
from app.models.subscription_lifecycle_schedule import (
    SubscriptionLifecycleSchedule,
    SubscriptionLifecycleScheduleStatus,
)
from app.services.subscription_lifecycle import SubscriptionCommandKind
from app.services.web_catalog_subscription_workflows import (
    _scheduled_status_change_context,
    cancel_lifecycle_schedule_redirect,
    preview_lifecycle_command_response,
)


def _subscription(db_session, subscriber, catalog_offer, *, status):
    subscription = Subscription(
        subscriber_id=subscriber.id,
        offer_id=catalog_offer.id,
        status=status,
        billing_mode=BillingMode.postpaid,
        start_at=datetime(2026, 7, 1, tzinfo=UTC),
        next_billing_at=datetime(2026, 8, 1, tzinfo=UTC),
    )
    db_session.add(subscription)
    db_session.flush()
    return subscription


def test_admin_lifecycle_preview_serializes_current_proposed_and_impacts(
    db_session, subscriber, catalog_offer
):
    subscription = _subscription(
        db_session,
        subscriber,
        catalog_offer,
        status=SubscriptionStatus.pending,
    )

    payload, status_code = preview_lifecycle_command_response(
        db_session,
        subscription_id=str(subscription.id),
        kind=SubscriptionCommandKind.activate,
        actor_id="operator-1",
        reason="installation accepted",
    )

    assert status_code == 200
    assert payload["status"] == "previewed"
    assert payload["expected_head"]
    assert payload["eligible"] is True
    assert payload["requires_confirmation"] is True
    assert payload["current"]["status"] == "pending"
    assert payload["proposed"]["status"] == "active"
    assert payload["billing_impact"]["action"] == "start_or_resume_collection"
    assert payload["billing_impact"]["net_amount"] == "0.00"
    assert payload["access_impact"]["session_action"] == "authorize"
    json.dumps(payload)


def test_admin_lifecycle_preview_returns_structured_ineligible_state(
    db_session, subscriber, catalog_offer
):
    subscription = _subscription(
        db_session,
        subscriber,
        catalog_offer,
        status=SubscriptionStatus.active,
    )

    payload, status_code = preview_lifecycle_command_response(
        db_session,
        subscription_id=str(subscription.id),
        kind=SubscriptionCommandKind.activate,
        actor_id="operator-1",
    )

    assert status_code == 200
    assert payload["eligible"] is False
    assert payload["eligibility_reasons"] == ["status_active_not_eligible_for_activate"]


def test_admin_lifecycle_preview_maps_missing_funding_to_unavailable(
    db_session, monkeypatch
):
    from app.services import web_catalog_subscription_workflows as workflows
    from app.services.prepaid_funding_reconstruction import (
        PrepaidFundingBaselineMissingError,
    )

    def missing_funding(*_args, **_kwargs):
        raise PrepaidFundingBaselineMissingError("baseline missing")

    monkeypatch.setattr(
        workflows,
        "preview_subscription_command",
        missing_funding,
    )

    payload, status_code = preview_lifecycle_command_response(
        db_session,
        subscription_id="00000000-0000-0000-0000-000000000001",
        kind=SubscriptionCommandKind.change_plan,
        actor_id="operator-1",
        target_offer_id="00000000-0000-0000-0000-000000000002",
    )

    assert status_code == 409
    assert payload["status"] == "unavailable"
    assert payload["error_code"] == "financial_position_unavailable"


def test_subscription_detail_projects_pending_status_schedules(
    db_session, subscriber, catalog_offer
):
    subscription = _subscription(
        db_session,
        subscriber,
        catalog_offer,
        status=SubscriptionStatus.active,
    )
    schedule = SubscriptionLifecycleSchedule(
        subscription_id=subscription.id,
        command_kind="cancel",
        source="admin:test",
        effective_timing="scheduled",
        effective_at=datetime(2026, 8, 15, tzinfo=UTC),
        reason="contract ended",
        reviewed_head="a" * 64,
        command_fingerprint="b" * 64,
        status=SubscriptionLifecycleScheduleStatus.pending,
        next_attempt_at=datetime(2026, 8, 15, tzinfo=UTC),
    )
    db_session.add(schedule)
    db_session.flush()

    rows = _scheduled_status_change_context(db_session, str(subscription.id))

    assert rows == [
        {
            "id": str(schedule.id),
            "kind": "cancel",
            "status": "pending",
            "effective_at": schedule.effective_at,
            "reason": "contract ended",
            "cancelable": True,
        }
    ]


def test_cancel_lifecycle_schedule_redirect_reports_success(
    db_session, subscriber, catalog_offer
):
    subscription = _subscription(
        db_session,
        subscriber,
        catalog_offer,
        status=SubscriptionStatus.active,
    )
    schedule = SubscriptionLifecycleSchedule(
        subscription_id=subscription.id,
        command_kind="suspend",
        source="admin:test",
        effective_timing="scheduled",
        effective_at=datetime(2026, 8, 15, tzinfo=UTC),
        reviewed_head="a" * 64,
        command_fingerprint="b" * 64,
        status=SubscriptionLifecycleScheduleStatus.pending,
        next_attempt_at=datetime(2026, 8, 15, tzinfo=UTC),
    )
    db_session.add(schedule)
    db_session.commit()

    redirect = cancel_lifecycle_schedule_redirect(
        db_session,
        subscription_id=str(subscription.id),
        schedule_id=str(schedule.id),
        actor_id="operator-1",
    )

    assert redirect.endswith("?notice=Lifecycle+schedule+canceled")
    db_session.refresh(schedule)
    assert schedule.status == SubscriptionLifecycleScheduleStatus.canceled


def test_subscription_detail_uses_canonical_preview_and_execute_endpoints():
    source = Path("templates/admin/catalog/subscription_detail.html").read_text()
    Environment(autoescape=True).parse(source)

    assert "/lifecycle/preview" in source
    assert "/lifecycle/execute" in source
    assert "expected_head" in source
    assert "Idempotency-Key" in source
    assert "billing_impact" in source
    assert "access_impact" in source
    assert "eligibility_reasons" in source
    assert "/cancel-view" in source
    assert "new Date(this.effectiveAt).toISOString()" in source
    assert "'Idempotency-Key': this.idempotencyKey" in source
    assert "requestId !== this.previewRequestId" in source
    assert "openLifecycle('disable')" in source
    assert "('suspended', 'blocked', 'stopped', 'disabled')" in source
    assert "Correct Subscription" in source
    assert "action_form(correction_action)" in source
    assert "subscriptionCorrectionActions" not in source


def test_subscription_detail_does_not_use_legacy_single_action_paths():
    source = Path("templates/admin/catalog/subscription_detail.html").read_text()

    assert "/subscriptions/bulk/${action}" not in source
    assert "/subscriptions/bulk/change-plan" not in source
    assert "/change-plan-quote" not in source
    assert "confirm(" not in source
    assert "onsubmit=" not in source


def test_lifecycle_reason_does_not_invalidate_preview_before_confirmation():
    templates = (
        (
            Path("templates/admin/catalog/subscription_detail.html"),
            "lifecycle-reason",
            "lifecycle-effective-at",
        ),
        (
            Path("templates/admin/catalog/subscriptions.html"),
            "bulk-reason",
            "bulk-effective-at",
        ),
    )

    for path, reason_id, effective_at_id in templates:
        source = path.read_text()
        reason_control = source.split(f'id="{reason_id}"', 1)[1].split(">", 1)[0]
        effective_at_control = source.split(f'id="{effective_at_id}"', 1)[1].split(
            ">", 1
        )[0]
        preview_logic = source.split("async refreshPreview()", 1)[1].split(
            "async executeLifecycle()", 1
        )[0]

        assert 'x-model="reason"' in reason_control
        assert "refreshPreview()" not in reason_control
        assert '@input="refreshPreview()"' in effective_at_control
        assert '@change="refreshPreview()"' not in effective_at_control
        assert preview_logic.index(
            "this.previewLoading = false;"
        ) < preview_logic.index("if (!this.kind")
