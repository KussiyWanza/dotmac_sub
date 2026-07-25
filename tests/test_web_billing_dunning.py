"""Tests for admin billing dunning projections."""

from uuid import uuid4

from app.models.collections import DunningCaseStatus
from app.schemas.collections import DunningCaseCreate
from app.services import collections as collections_service
from app.services import web_billing_dunning
from app.services.collections import _core as dunning_owner


def test_dunning_bulk_preview_reports_exact_eligible_and_missing_scope(
    db_session,
    subscriber_account,
):
    case = collections_service.dunning_cases.create(
        db_session,
        DunningCaseCreate(
            account_id=subscriber_account.id,
            status=DunningCaseStatus.open,
        ),
    )
    missing_id = uuid4()

    state = web_billing_dunning.build_bulk_preview_data(
        db_session,
        case_ids_csv=f"{case.id},{missing_id}",
        action=dunning_owner.DunningStaffAction.pause,
    )

    preview = state["bulk_preview"]
    assert preview.selected_count == 2
    assert preview.eligible_case_ids == (case.id,)
    assert preview.skipped_count == 1
    assert state["bulk_action_form"].allowed is True
    missing_impact = next(
        impact
        for impact in state["bulk_impact_rows"]
        if impact.case_id == str(missing_id)
    )
    assert missing_impact.reason == "Dunning case was not found."
    db_session.refresh(case)
    assert case.status is DunningCaseStatus.open


def test_dunning_listing_omits_selection_for_read_only_staff(
    db_session,
):
    state = web_billing_dunning.build_listing_data(
        db_session,
        page=1,
        per_page=50,
        status=None,
        customer_ref=None,
        can_write=False,
    )

    assert state["dunning_bulk_action_contract"]["selection_enabled"] is False
    assert state["dunning_bulk_action_contract"]["actions"] == []
