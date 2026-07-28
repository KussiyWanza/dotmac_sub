from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.services.service_team_party_cutover import IdentityDecisionKind
from scripts.migration.plan_service_team_party_cutover import (
    PlanBuildError,
    ReviewedDecision,
    build_plan,
)


def _source_rows():
    now = datetime.now(UTC)
    team_id = uuid4()
    person_id = uuid4()
    user_id = uuid4()
    membership_id = uuid4()
    crm_team = {
        "id": team_id,
        "name": "Support",
        "team_type": "support",
        "region": "Abuja",
        "manager_person_id": person_id,
        "is_active": True,
        "created_at": now,
        "updated_at": now,
    }
    sub_team = dict(crm_team)
    membership = {
        "id": membership_id,
        "team_id": team_id,
        "person_id": person_id,
        "role": "lead",
        "is_active": True,
        "created_at": now,
    }
    person = {
        "id": person_id,
        "first_name": "Ada",
        "last_name": "Operator",
        "display_name": "Ada Operator",
        "email": "internal@example.com",
        "is_active": True,
    }
    user = {
        "id": user_id,
        "is_active": True,
        "person_party_id": None,
    }
    decision = ReviewedDecision(
        crm_person_id=person_id,
        decision=IdentityDecisionKind.bind,
        system_user_id=user_id,
        decision_id=uuid4(),
        reason_sha256="c" * 64,
    )
    return {
        "now": now,
        "team": crm_team,
        "sub_team": sub_team,
        "membership": membership,
        "person_id": person_id,
        "person": person,
        "user_id": user_id,
        "user": user,
        "decision": decision,
    }


def test_plan_preserves_crm_identity_and_membership_ids() -> None:
    rows = _source_rows()

    plan = build_plan(
        crm_teams=[rows["team"]],
        crm_memberships=[rows["membership"]],
        crm_people={rows["person_id"]: rows["person"]},
        sub_teams=[rows["sub_team"]],
        sub_users={rows["user_id"]: rows["user"]},
        decisions={rows["person_id"]: rows["decision"]},
        decision_file_sha256="b" * 64,
        planned_at=rows["now"],
    )

    assert plan.identities[0].legacy_person_id == rows["person_id"]
    assert plan.identities[0].system_user_id == rows["user_id"]
    assert plan.memberships[0].membership_id == rows["membership"]["id"]
    assert plan.memberships[0].legacy_person_id == rows["person_id"]
    assert len(plan.plan_digest) == 64
    assert plan.file_payload()["plan_digest"] == plan.plan_digest


def test_plan_rejects_missing_reviewed_identity_decision() -> None:
    rows = _source_rows()

    with pytest.raises(PlanBuildError, match="exactly every referenced CRM Person"):
        build_plan(
            crm_teams=[rows["team"]],
            crm_memberships=[rows["membership"]],
            crm_people={rows["person_id"]: rows["person"]},
            sub_teams=[rows["sub_team"]],
            sub_users={rows["user_id"]: rows["user"]},
            decisions={},
            decision_file_sha256="b" * 64,
            planned_at=rows["now"],
        )


def test_plan_rejects_drifted_sub_team_copy() -> None:
    rows = _source_rows()
    rows["sub_team"]["manager_person_id"] = uuid4()

    with pytest.raises(PlanBuildError, match="not cutover-ready"):
        build_plan(
            crm_teams=[rows["team"]],
            crm_memberships=[rows["membership"]],
            crm_people={rows["person_id"]: rows["person"]},
            sub_teams=[rows["sub_team"]],
            sub_users={rows["user_id"]: rows["user"]},
            decisions={rows["person_id"]: rows["decision"]},
            decision_file_sha256="b" * 64,
            planned_at=rows["now"],
        )


def test_plan_requires_active_system_user_for_active_member() -> None:
    rows = _source_rows()
    rows["user"]["is_active"] = False

    with pytest.raises(PlanBuildError, match="require active SystemUsers"):
        build_plan(
            crm_teams=[rows["team"]],
            crm_memberships=[rows["membership"]],
            crm_people={rows["person_id"]: rows["person"]},
            sub_teams=[rows["sub_team"]],
            sub_users={rows["user_id"]: rows["user"]},
            decisions={rows["person_id"]: rows["decision"]},
            decision_file_sha256="b" * 64,
            planned_at=rows["now"],
        )
