from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from app.models.audit import AuditEvent
from app.models.auth import Session as AuthSession
from app.models.auth import SessionStatus
from app.services import staff_session_party_adoption as owner
from app.services.owner_commands import CommandContext
from scripts.migration import execute_staff_session_party_projection as adoption
from tests.staff_identity_fixtures import add_bound_staff_user

PLANNED_AT = datetime(2026, 8, 16, 18, 0, tzinfo=UTC)


def _item() -> adoption.StaffSessionPartyProjectionItem:
    return adoption.StaffSessionPartyProjectionItem(
        decision_id=uuid4(),
        session_id=uuid4(),
        system_user_id=uuid4(),
        person_party_id=uuid4(),
        evidence_sha256="a" * 64,
    )


def _plan(
    *items: adoption.StaffSessionPartyProjectionItem,
) -> adoption.StaffSessionPartyProjectionPlan:
    return adoption.build_plan(items=items or (_item(),), planned_at=PLANNED_AT)


def _approval(
    plan: adoption.StaffSessionPartyProjectionPlan,
    *,
    plan_file_sha256: str = "b" * 64,
) -> adoption.StaffSessionPartyProjectionApproval:
    return adoption.StaffSessionPartyProjectionApproval(
        approval_id=uuid4(),
        plan_digest=plan.plan_digest,
        plan_file_sha256=plan_file_sha256,
        approved_by_user_id=uuid4(),
        approved_at=PLANNED_AT + timedelta(minutes=5),
        expires_at=PLANNED_AT + timedelta(minutes=35),
        reason_sha256="c" * 64,
        maximum_session_projections=len(plan.items),
    )


def _legacy_session(
    db_session,
    *,
    system_user_id: UUID,
    status: SessionStatus = SessionStatus.active,
    revoked_at: datetime | None = None,
) -> AuthSession:
    session = AuthSession(
        system_user_id=system_user_id,
        party_id=None,
        status=status,
        revoked_at=revoked_at,
        token_hash=f"session-projection-{uuid4().hex}",
        expires_at=PLANNED_AT + timedelta(hours=1),
    )
    db_session.add(session)
    db_session.flush()
    return session


def test_plan_is_deterministic_uuid_only_and_fully_typed() -> None:
    first = _item()
    second = _item()

    forward = _plan(first, second)
    reverse = _plan(second, first)

    assert forward.plan_digest == reverse.plan_digest
    assert forward.items == tuple(sorted((first, second), key=lambda item: item.key))
    assert adoption.public_contract_type_errors() == ()


def test_public_contract_guard_detects_an_untyped_function(monkeypatch) -> None:
    def untyped_contract(value):
        return value

    monkeypatch.setattr(adoption, "_PUBLIC_CONTRACT_TYPES", ())
    monkeypatch.setattr(adoption, "_PUBLIC_CONTRACT_FUNCTIONS", (untyped_contract,))

    assert adoption.public_contract_type_errors() == (
        "untyped_contract.return: missing",
        "untyped_contract.value: missing",
    )


def test_plan_parser_refuses_inferred_identity_fields() -> None:
    item = _item()
    plan = _plan(item)
    payload = plan.model_dump(mode="json")
    payload["items"][0]["email"] = "never-an-identity-input@example.test"

    with pytest.raises(adoption.StaffSessionProjectionRefused) as raised:
        adoption.parse_plan_payload(payload)

    assert raised.value.code is adoption.StaffSessionProjectionRefusalCode.invalid_plan


def test_approval_is_exact_file_digest_count_and_expiry_bound() -> None:
    plan = _plan()
    approval = _approval(plan)

    adoption.validate_approval(
        plan=plan,
        approval=approval,
        plan_file_sha256=approval.plan_file_sha256,
        executed_at=PLANNED_AT + timedelta(minutes=20),
    )

    for changed_file, executed_at, expected in (
        (
            "d" * 64,
            PLANNED_AT + timedelta(minutes=20),
            adoption.StaffSessionProjectionRefusalCode.approval_mismatch,
        ),
        (
            approval.plan_file_sha256,
            PLANNED_AT + timedelta(minutes=36),
            adoption.StaffSessionProjectionRefusalCode.expired_approval,
        ),
    ):
        with pytest.raises(adoption.StaffSessionProjectionRefused) as raised:
            adoption.validate_approval(
                plan=plan,
                approval=approval,
                plan_file_sha256=changed_file,
                executed_at=executed_at,
            )
        assert raised.value.code is expected


def test_database_plan_selects_only_active_unrevoked_exact_fk_rows(
    db_session,
) -> None:
    user, person = add_bound_staff_user(db_session)
    eligible = _legacy_session(db_session, system_user_id=user.id)
    _legacy_session(
        db_session,
        system_user_id=user.id,
        status=SessionStatus.revoked,
        revoked_at=PLANNED_AT - timedelta(hours=1),
    )
    already_projected = _legacy_session(db_session, system_user_id=user.id)
    already_projected.party_id = person.id
    db_session.flush()

    report = adoption.build_projection_report(db_session)
    plan = adoption.build_plan_from_database(
        db_session,
        planned_at=PLANNED_AT,
    )

    assert report.active_unrevoked_staff_sessions == 2
    assert report.active_unrevoked_projected == 1
    assert report.active_unrevoked_remaining == 1
    assert report.active_unrevoked_unbound == 0
    assert report.projection_disagreements == 0
    assert report.is_ratchet_ready is False
    assert len(plan.items) == 1
    assert plan.items[0].session_id == eligible.id
    assert plan.items[0].system_user_id == user.id
    assert plan.items[0].person_party_id == person.id


def test_database_plan_refuses_an_active_unbound_principal(db_session) -> None:
    user, _person = add_bound_staff_user(db_session)
    user.person_party_id = None
    user.party_bound_at = None
    user.party_binding_source = None
    user.party_binding_reason = None
    _legacy_session(db_session, system_user_id=user.id)
    db_session.flush()

    with pytest.raises(adoption.StaffSessionProjectionRefused) as raised:
        adoption.build_plan_from_database(db_session, planned_at=PLANNED_AT)

    assert raised.value.code is adoption.StaffSessionProjectionRefusalCode.changed_input


def test_owner_projects_audits_and_replays_exactly(db_session) -> None:
    user, person = add_bound_staff_user(db_session)
    approver, approver_person = add_bound_staff_user(db_session)
    session = _legacy_session(db_session, system_user_id=user.id)
    session_id = session.id
    user_id = user.id
    party_id = person.id
    approver_id = approver.id
    approver_party_id = approver_person.id
    db_session.commit()
    command = owner.ProjectStaffSessionPartyCommand(
        context=CommandContext.system(
            actor=f"user:{approver_id}",
            scope=owner.COMMAND_SCOPE,
            reason="approved exact staff session projection",
        ),
        session_id=session_id,
        expected_system_user_id=user_id,
        person_party_id=party_id,
        decision_id=uuid4(),
        plan_digest="1" * 64,
        evidence_sha256="2" * 64,
        approval_id=uuid4(),
        approval_sha256="3" * 64,
    )

    first = owner.project_staff_session_party(db_session, command)
    replay = owner.project_staff_session_party(db_session, command)

    assert first.replayed is False
    assert replay.replayed is True
    assert replay.session_id == first.session_id
    assert db_session.get(AuthSession, session_id).party_id == party_id
    audit = (
        db_session.query(AuditEvent)
        .filter(AuditEvent.action == "party.staff_session_projected")
        .one()
    )
    assert audit.actor_id == str(approver_id)
    assert audit.actor_party_id == approver_party_id
    assert audit.metadata_["person_party_id"] == str(party_id)


def test_owner_refuses_changed_or_ineligible_rows_without_mutation(db_session) -> None:
    user, person = add_bound_staff_user(db_session)
    approver, _approver_person = add_bound_staff_user(db_session)
    session = _legacy_session(db_session, system_user_id=user.id)
    session.status = SessionStatus.revoked
    session.revoked_at = PLANNED_AT
    session_id = session.id
    user_id = user.id
    party_id = person.id
    approver_id = approver.id
    db_session.commit()
    command = owner.ProjectStaffSessionPartyCommand(
        context=CommandContext.system(
            actor=f"user:{approver_id}",
            scope=owner.COMMAND_SCOPE,
            reason="approved exact staff session projection",
        ),
        session_id=session_id,
        expected_system_user_id=user_id,
        person_party_id=party_id,
        decision_id=uuid4(),
        plan_digest="4" * 64,
        evidence_sha256="5" * 64,
        approval_id=uuid4(),
        approval_sha256="6" * 64,
    )

    with pytest.raises(owner.StaffSessionPartyAdoptionError) as raised:
        owner.project_staff_session_party(db_session, command)

    assert raised.value.code == "party.staff_session_projection.session_ineligible"
    assert db_session.get(AuthSession, session_id).party_id is None


def test_executor_delegates_each_item_to_the_typed_owner(monkeypatch) -> None:
    plan = _plan(_item(), _item())
    approval = _approval(plan)
    calls: list[owner.ProjectStaffSessionPartyCommand] = []

    def project(
        _db: object,
        command: owner.ProjectStaffSessionPartyCommand,
    ) -> owner.StaffSessionPartyProjectionOutcome:
        calls.append(command)
        return owner.StaffSessionPartyProjectionOutcome(
            session_id=command.session_id,
            system_user_id=command.expected_system_user_id,
            party_id=command.person_party_id,
            replayed=False,
        )

    monkeypatch.setattr(adoption.owner, "project_staff_session_party", project)

    outcome = adoption.execute_approved_plan(
        object(),
        plan=plan,
        approval=approval,
        plan_file_sha256=approval.plan_file_sha256,
        executed_at=PLANNED_AT + timedelta(minutes=20),
    )

    assert [call.session_id for call in calls] == [
        item.session_id for item in plan.items
    ]
    assert all(call.context.scope == owner.COMMAND_SCOPE for call in calls)
    assert outcome == adoption.StaffSessionPartyProjectionExecutionOutcome(
        plan_digest=plan.plan_digest,
        projections_applied=2,
        projection_replays=0,
    )
