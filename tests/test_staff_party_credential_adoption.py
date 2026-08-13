from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy.exc import SQLAlchemyError

from app.models.audit import AuditEvent
from app.models.party import PartyType
from app.models.subscriber import UserType
from app.models.system_user import SystemUser
from app.services import party as party_service
from app.services import staff_party_adoption
from app.services.owner_commands import CommandContext
from scripts.migration import execute_staff_party_credential_adoption as adoption


def _item(
    *,
    action: adoption.StaffPartyAdoptionAction = (
        adoption.StaffPartyAdoptionAction.bind_principal_and_project
    ),
) -> adoption.StaffPartyAdoptionItem:
    common: dict[str, object] = {
        "decision_id": uuid4(),
        "system_user_id": uuid4(),
        "person_party_id": uuid4(),
        "credential_id": uuid4(),
        "authentication_binding_id": uuid4(),
        "evidence_sha256": "a" * 64,
    }
    if action is adoption.StaffPartyAdoptionAction.bind_principal_and_project:
        return adoption.StaffPrincipalAndCredentialAdoptionItem(
            **common,
        )
    return adoption.StaffCredentialProjectionItem(**common)


def _plan(
    *items: adoption.StaffPartyAdoptionItem,
) -> adoption.StaffPartyAdoptionPlan:
    return adoption.build_plan(
        items=items or (_item(),),
        planned_at=datetime(2026, 8, 12, 8, 0, tzinfo=UTC),
    )


def _approval(
    plan: adoption.StaffPartyAdoptionPlan,
    *,
    plan_file_sha256: str = "b" * 64,
    approved_at: datetime = datetime(2026, 8, 12, 8, 5, tzinfo=UTC),
    expires_at: datetime = datetime(2026, 8, 12, 9, 5, tzinfo=UTC),
) -> adoption.StaffPartyAdoptionApproval:
    return adoption.StaffPartyAdoptionApproval(
        approval_id=uuid4(),
        plan_digest=plan.plan_digest,
        plan_file_sha256=plan_file_sha256,
        approved_by_user_id=uuid4(),
        approved_at=approved_at,
        expires_at=expires_at,
        reason_sha256="c" * 64,
        maximum_principal_bindings=plan.principal_binding_count,
        maximum_credential_projections=plan.credential_projection_count,
    )


def test_plan_digest_is_order_independent_and_contract_is_fully_typed() -> None:
    first = _item()
    second = _item(action=adoption.StaffPartyAdoptionAction.project_only)

    forward = _plan(first, second)
    reverse = _plan(second, first)

    assert forward.plan_digest == reverse.plan_digest
    assert forward.items == tuple(sorted((first, second), key=lambda item: item.key))
    assert forward.principal_binding_count == 1
    assert forward.credential_projection_count == 2
    assert adoption.public_contract_type_errors() == ()


def test_public_contract_type_guard_detects_missing_annotations(monkeypatch) -> None:
    def untyped_contract(value):
        return value

    monkeypatch.setattr(
        adoption,
        "_PUBLIC_CONTRACT_TYPES",
        (),
    )
    monkeypatch.setattr(
        adoption,
        "_PUBLIC_CONTRACT_FUNCTIONS",
        (untyped_contract,),
    )

    assert adoption.public_contract_type_errors() == (
        "untyped_contract.return: missing",
        "untyped_contract.value: missing",
    )


def test_plan_parser_refuses_unknown_or_inferred_identity_fields() -> None:
    payload = {
        "contract_version": 1,
        "planned_at": "2026-08-12T08:00:00+00:00",
        "plan_digest": "d" * 64,
        "items": [
            {
                "decision_id": str(uuid4()),
                "action": "project_only",
                "system_user_id": str(uuid4()),
                "person_party_id": str(uuid4()),
                "credential_id": str(uuid4()),
                "authentication_binding_id": str(uuid4()),
                "evidence_sha256": "e" * 64,
                "email": "must-not-be-an-identity-input@example.test",
            }
        ],
    }

    with pytest.raises(adoption.StaffPartyAdoptionRefused) as raised:
        adoption.parse_plan_payload(payload)

    assert raised.value.code is adoption.StaffPartyAdoptionRefusalCode.invalid_plan


def test_approval_is_exact_digest_bound_count_bound_and_expiring() -> None:
    plan = _plan()
    approval = _approval(plan)
    plan_file_sha256 = approval.plan_file_sha256

    adoption.validate_approval(
        plan=plan,
        approval=approval,
        plan_file_sha256=plan_file_sha256,
        executed_at=datetime(2026, 8, 12, 8, 30, tzinfo=UTC),
    )

    with pytest.raises(adoption.StaffPartyAdoptionRefused) as expired:
        adoption.validate_approval(
            plan=plan,
            approval=approval,
            plan_file_sha256=plan_file_sha256,
            executed_at=datetime(2026, 8, 12, 9, 6, tzinfo=UTC),
        )
    assert expired.value.code is adoption.StaffPartyAdoptionRefusalCode.expired_approval

    with pytest.raises(adoption.StaffPartyAdoptionRefused) as changed_file:
        adoption.validate_approval(
            plan=plan,
            approval=approval,
            plan_file_sha256="f" * 64,
            executed_at=datetime(2026, 8, 12, 8, 30, tzinfo=UTC),
        )
    assert (
        changed_file.value.code
        is adoption.StaffPartyAdoptionRefusalCode.approval_mismatch
    )


def test_execute_delegates_each_phase_to_typed_owner_commands(monkeypatch) -> None:
    bind_item = _item()
    project_item = _item(action=adoption.StaffPartyAdoptionAction.project_only)
    plan = _plan(bind_item, project_item)
    approval = _approval(plan)
    calls: list[tuple[str, UUID, UUID]] = []

    def bind_staff(_db: object, command: object) -> object:
        assert isinstance(command, adoption.BindExistingStaffPartyCommand)
        calls.append(("staff", command.system_user_id, command.person_party_id))
        return adoption.ExistingStaffPartyBindingOutcome(
            system_user_id=command.system_user_id,
            person_party_id=command.person_party_id,
            bound_at=datetime(2026, 8, 12, 8, 10, tzinfo=UTC),
            replayed=False,
        )

    def bind_credential(_db: object, command: object) -> object:
        assert isinstance(command, adoption.CredentialPartyBinding)
        assert (
            command.expected_principal_kind
            is adoption.CredentialPrincipalKind.system_user
        )
        expected_item = next(
            item for item in plan.items if item.credential_id == command.credential_id
        )
        assert command.expected_principal_id == expected_item.system_user_id
        calls.append(("credential", command.credential_id, command.party_id))
        return adoption.CredentialPartyBindingOutcome(
            credential_id=command.credential_id,
            party_id=command.party_id,
            authentication_binding_id=command.authentication_binding_id,
            tenant_id=command.tenant_id,
            bound_at=datetime(2026, 8, 12, 8, 10, tzinfo=UTC),
            replayed=False,
        )

    monkeypatch.setattr(
        adoption.staff_party_adoption, "bind_existing_staff_party", bind_staff
    )
    monkeypatch.setattr(
        adoption.credential_party_binding,
        "bind_credential_party",
        bind_credential,
    )

    outcome = adoption.execute_approved_plan(
        object(),
        plan=plan,
        approval=approval,
        plan_file_sha256=approval.plan_file_sha256,
        executed_at=datetime(2026, 8, 12, 8, 30, tzinfo=UTC),
    )

    expected_calls: list[tuple[str, UUID, UUID]] = []
    for item in plan.items:
        if isinstance(item, adoption.StaffPrincipalAndCredentialAdoptionItem):
            expected_calls.append(("staff", item.system_user_id, item.person_party_id))
        expected_calls.append(("credential", item.credential_id, item.person_party_id))
    assert calls == expected_calls
    assert outcome == adoption.StaffPartyAdoptionOutcome(
        plan_digest=plan.plan_digest,
        principal_bindings_applied=1,
        principal_binding_replays=0,
        credential_projections_applied=2,
        credential_projection_replays=0,
    )


def test_existing_staff_binding_is_atomic_audited_and_exactly_replayable(
    db_session,
) -> None:
    party = party_service.create_party(
        db_session,
        party_type=PartyType.person,
        display_name="Private Reviewed Staff",
    )
    staff = SystemUser(
        first_name="Private",
        last_name="Staff",
        email=f"staff-{uuid4().hex}@example.test",
        user_type=UserType.system_user,
        is_active=True,
    )
    db_session.add(staff)
    db_session.flush()
    staff_id = staff.id
    party_id = party.id
    db_session.commit()
    approver_id = uuid4()
    context = CommandContext.system(
        actor=f"user:{approver_id}",
        scope=staff_party_adoption.COMMAND_SCOPE,
        reason="approved UUID-only staff Party adoption test",
    )
    command = adoption.BindExistingStaffPartyCommand(
        context=context,
        system_user_id=staff_id,
        person_party_id=party_id,
        binding_source="spa:v1:" + "1" * 64,
        binding_reason="decision=" + str(uuid4()) + ";evidence_sha256=" + "2" * 64,
    )

    first = staff_party_adoption.bind_existing_staff_party(db_session, command)
    replay = staff_party_adoption.bind_existing_staff_party(db_session, command)

    assert first.replayed is False
    assert replay.replayed is True
    assert replay.bound_at == first.bound_at
    persisted = db_session.get(SystemUser, staff_id)
    assert persisted is not None
    assert persisted.person_party_id == party_id
    assert (
        db_session.query(AuditEvent)
        .filter(AuditEvent.action == "party.staff_principal_adopted")
        .count()
        == 1
    )
    db_session.commit()

    changed = adoption.BindExistingStaffPartyCommand(
        context=context,
        system_user_id=staff_id,
        person_party_id=party_id,
        binding_source=command.binding_source,
        binding_reason="different approval evidence",
    )
    with pytest.raises(staff_party_adoption.StaffPartyAdoptionError) as refused:
        staff_party_adoption.bind_existing_staff_party(db_session, changed)
    assert refused.value.code == "party.staff_principal_adoption.party_binding_refused"


def test_plan_digest_changes_when_exact_mapping_changes() -> None:
    item = _item()
    assert isinstance(item, adoption.StaffPrincipalAndCredentialAdoptionItem)
    original = _plan(item)
    changed = adoption.StaffPrincipalAndCredentialAdoptionItem(
        decision_id=item.decision_id,
        system_user_id=item.system_user_id,
        person_party_id=uuid4(),
        credential_id=item.credential_id,
        authentication_binding_id=item.authentication_binding_id,
        evidence_sha256=item.evidence_sha256,
    )

    assert _plan(changed).plan_digest != original.plan_digest
    assert len(original.plan_digest) == hashlib.sha256().digest_size * 2


def test_projection_items_compose_multiple_bindings_for_one_party() -> None:
    party_id = uuid4()
    system_user_id = uuid4()
    local = adoption.StaffCredentialProjectionItem(
        decision_id=uuid4(),
        system_user_id=system_user_id,
        person_party_id=party_id,
        credential_id=uuid4(),
        authentication_binding_id=uuid4(),
        evidence_sha256="3" * 64,
    )
    radius = adoption.StaffCredentialProjectionItem(
        decision_id=uuid4(),
        system_user_id=system_user_id,
        person_party_id=party_id,
        credential_id=uuid4(),
        authentication_binding_id=uuid4(),
        evidence_sha256="4" * 64,
    )

    plan = _plan(local, radius)

    assert plan.credential_projection_count == 2
    assert plan.principal_binding_count == 0


def test_plan_refuses_duplicate_party_binding_tuple() -> None:
    party_id = uuid4()
    binding_id = uuid4()
    first = adoption.StaffCredentialProjectionItem(
        decision_id=uuid4(),
        system_user_id=uuid4(),
        person_party_id=party_id,
        credential_id=uuid4(),
        authentication_binding_id=binding_id,
        evidence_sha256="5" * 64,
    )
    second = adoption.StaffCredentialProjectionItem(
        decision_id=uuid4(),
        system_user_id=uuid4(),
        person_party_id=party_id,
        credential_id=uuid4(),
        authentication_binding_id=binding_id,
        evidence_sha256="6" * 64,
    )

    with pytest.raises(adoption.StaffPartyAdoptionRefused) as raised:
        _plan(first, second)

    assert raised.value.code is adoption.StaffPartyAdoptionRefusalCode.invalid_plan


def test_database_failure_is_a_typed_pii_free_refusal(monkeypatch) -> None:
    plan = _plan(_item(action=adoption.StaffPartyAdoptionAction.project_only))
    approval = _approval(plan)

    def fail(_db: object, _command: object) -> object:
        raise SQLAlchemyError("private database detail")

    monkeypatch.setattr(
        adoption.credential_party_binding,
        "bind_credential_party",
        fail,
    )

    with pytest.raises(adoption.StaffPartyAdoptionRefused) as raised:
        adoption.execute_approved_plan(
            object(),
            plan=plan,
            approval=approval,
            plan_file_sha256=approval.plan_file_sha256,
            executed_at=datetime(2026, 8, 12, 8, 30, tzinfo=UTC),
        )

    assert raised.value.code is adoption.StaffPartyAdoptionRefusalCode.database_failure
    assert "private database detail" not in raised.value.message


def test_approval_window_cannot_exceed_twenty_four_hours() -> None:
    plan = _plan()
    approval = _approval(
        plan,
        expires_at=datetime(2026, 8, 13, 8, 5, tzinfo=UTC) + timedelta(seconds=1),
    )

    with pytest.raises(adoption.StaffPartyAdoptionRefused) as raised:
        adoption.validate_approval(
            plan=plan,
            approval=approval,
            plan_file_sha256=approval.plan_file_sha256,
            executed_at=datetime(2026, 8, 12, 8, 30, tzinfo=UTC),
        )

    assert raised.value.code is adoption.StaffPartyAdoptionRefusalCode.invalid_approval
