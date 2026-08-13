from __future__ import annotations

import uuid
from datetime import UTC

import pytest

from app.models.audit import AuditEvent
from app.models.auth import (
    AuthenticationBinding,
    AuthenticationBindingIdentityError,
    AuthProvider,
    UserCredential,
)
from app.models.party import PartyType
from app.models.subscriber import Subscriber, UserType
from app.models.system_user import SystemUser
from app.services import party as party_service
from app.services.credential_party_binding import (
    CredentialBindingError,
    CredentialPartyBinding,
    CredentialPrincipalKind,
    bind_credential_party,
    credential_convergence_report,
    resolve_binding_for_mechanism,
)
from app.services.operator_tenant import OPERATOR_TENANT_ID, provision_operator_tenant
from app.services.owner_commands import CommandContext

SCOPE = "party:credential_authentication_projection"


def _context() -> CommandContext:
    return CommandContext.system(
        actor="pytest:credential-adoption",
        scope=SCOPE,
        reason="reviewed credential adoption test",
    )


def _binding(
    db_session,
    *,
    binding_key: str = "local.test",
    mechanism_code: str = "local",
) -> AuthenticationBinding:
    row = AuthenticationBinding(
        binding_key=binding_key,
        mechanism_code=mechanism_code,
        name=f"Test {mechanism_code}",
        is_active=True,
    )
    db_session.add(row)
    db_session.flush()
    return row


def _staff_credential(db_session):
    provision_operator_tenant(db_session)
    party = party_service.create_party(
        db_session,
        party_type=PartyType.person,
        display_name="Private Staff Person",
    )
    staff = SystemUser(
        first_name="Private",
        last_name="Staff",
        email=f"staff-{uuid.uuid4().hex}@example.test",
        user_type=UserType.system_user,
        is_active=True,
    )
    db_session.add(staff)
    db_session.flush()
    party_service.bind_system_user_principal(
        db_session,
        system_user_id=staff.id,
        person_party_id=party.id,
        source="reviewed_staff_worklist",
        reason="reviewed staff identity",
    )
    credential = UserCredential(
        system_user_id=staff.id,
        provider=AuthProvider.local,
        username=f"staff-{uuid.uuid4().hex}",
        password_hash="not-a-real-hash",
        is_active=True,
    )
    binding = _binding(db_session)
    db_session.add(credential)
    db_session.flush()
    credential_id = credential.id
    staff_id = staff.id
    party_id = party.id
    binding_id = binding.id
    db_session.commit()
    return credential_id, staff_id, party_id, binding_id


def _command(
    credential_id: uuid.UUID,
    expected_principal_id: uuid.UUID,
    party_id: uuid.UUID,
    binding_id: uuid.UUID,
    *,
    expected_principal_kind: CredentialPrincipalKind = (
        CredentialPrincipalKind.system_user
    ),
    reason: str = "reviewed exact credential projection",
) -> CredentialPartyBinding:
    return CredentialPartyBinding(
        context=_context(),
        credential_id=credential_id,
        expected_principal_kind=expected_principal_kind,
        expected_principal_id=expected_principal_id,
        party_id=party_id,
        authentication_binding_id=binding_id,
        tenant_id=OPERATOR_TENANT_ID,
        binding_source="reviewed_credential_worklist",
        binding_reason=reason,
    )


def test_complete_projection_commits_and_exact_retry_preserves_evidence(db_session):
    credential_id, staff_id, party_id, binding_id = _staff_credential(db_session)
    command = _command(credential_id, staff_id, party_id, binding_id)

    first = bind_credential_party(db_session, command)
    replay = bind_credential_party(db_session, command)

    assert first.replayed is False
    assert replay.replayed is True
    assert replay.bound_at == first.bound_at
    credential = db_session.get(UserCredential, credential_id)
    assert credential is not None
    assert (
        credential.party_id,
        credential.authentication_binding_id,
        credential.tenant_id,
    ) == (party_id, binding_id, OPERATOR_TENANT_ID)
    assert credential.party_bound_at is not None
    assert credential.party_bound_at.replace(tzinfo=UTC) == first.bound_at
    assert (
        db_session.query(AuditEvent)
        .filter(AuditEvent.action == "credential.party_authentication_projected")
        .count()
        == 1
    )


def test_changed_evidence_is_not_an_exact_retry(db_session):
    credential_id, staff_id, party_id, binding_id = _staff_credential(db_session)
    bind_credential_party(
        db_session, _command(credential_id, staff_id, party_id, binding_id)
    )

    with pytest.raises(CredentialBindingError) as raised:
        bind_credential_party(
            db_session,
            _command(
                credential_id,
                staff_id,
                party_id,
                binding_id,
                reason="different review evidence",
            ),
        )

    assert raised.value.code.endswith(".repoint_refused")


def test_typed_expected_principal_must_match_the_locked_credential(db_session):
    credential_id, _staff_id, party_id, binding_id = _staff_credential(db_session)

    with pytest.raises(CredentialBindingError) as raised:
        bind_credential_party(
            db_session,
            _command(credential_id, uuid.uuid4(), party_id, binding_id),
        )

    assert raised.value.code.endswith(".principal_mismatch")
    assert raised.value.details == {
        "expected_principal_kind": "system_user",
        "actual_principal_kind": "system_user",
    }


def test_person_and_declared_matching_mechanism_are_required(db_session):
    credential_id, staff_id, party_id, _ = _staff_credential(db_session)
    organization = party_service.create_party(
        db_session,
        party_type=PartyType.organization,
        display_name="Private Organization",
    )
    radius = _binding(db_session, binding_key="radius.test", mechanism_code="radius")
    invented = _binding(db_session, binding_key="sso.test", mechanism_code="sso")
    organization_id = organization.id
    radius_id = radius.id
    invented_id = invented.id
    db_session.commit()

    with pytest.raises(CredentialBindingError) as non_person:
        bind_credential_party(
            db_session,
            _command(credential_id, staff_id, organization_id, radius_id),
        )
    assert non_person.value.code.endswith(".person_required")

    with pytest.raises(CredentialBindingError) as mismatch:
        bind_credential_party(
            db_session, _command(credential_id, staff_id, party_id, radius_id)
        )
    assert mismatch.value.code.endswith(".mechanism_mismatch")

    with pytest.raises(CredentialBindingError) as undeclared:
        bind_credential_party(
            db_session, _command(credential_id, staff_id, party_id, invented_id)
        )
    assert undeclared.value.code.endswith(".undeclared_mechanism")


def test_mechanism_resolution_refuses_ambiguity(db_session):
    _staff_credential(db_session)
    db_session.add(
        AuthenticationBinding(
            binding_key="local.second",
            mechanism_code="local",
            name="Second local verifier",
            is_active=True,
        )
    )
    db_session.commit()

    with pytest.raises(CredentialBindingError) as raised:
        resolve_binding_for_mechanism(db_session, "local")

    assert raised.value.code.endswith(".ambiguous_mechanism_binding")


def test_installed_binding_identity_is_immutable(db_session):
    _staff_credential(db_session)
    binding = db_session.query(AuthenticationBinding).one()

    binding.mechanism_code = "radius"
    with pytest.raises(AuthenticationBindingIdentityError):
        db_session.flush()


def test_report_separates_principal_readiness_from_projection(db_session):
    credential_id, staff_id, party_id, binding_id = _staff_credential(db_session)

    before = credential_convergence_report(db_session)
    db_session.commit()
    bind_credential_party(
        db_session, _command(credential_id, staff_id, party_id, binding_id)
    )
    after = credential_convergence_report(db_session)

    staff_before = next(
        item
        for item in before.principal_cohorts
        if item.principal_kind == "system_user"
    )
    assert staff_before.principal_party_ready == 1
    assert staff_before.remaining == 0
    assert before.projection.projected == 0
    assert before.projection.remaining == 1
    assert after.projection.projected == 1
    assert after.projection.remaining == 0
    assert after.projection.is_ready_for_enforcement is True


def test_second_local_credential_for_same_party_is_refused(db_session):
    from app.services.subscriber import _default_reseller_id

    credential_id, staff_id, party_id, binding_id = _staff_credential(db_session)
    subscriber = Subscriber(
        first_name="Private",
        last_name="Subscriber",
        email=f"subscriber-{uuid.uuid4().hex}@example.test",
        reseller_id=_default_reseller_id(db_session),
    )
    db_session.add(subscriber)
    db_session.flush()
    party_service.bind_subscriber_account(
        db_session,
        subscriber_id=subscriber.id,
        party_id=party_id,
        source="reviewed_subscriber_worklist",
        reason="same human, separate customer account",
    )
    second = UserCredential(
        subscriber_id=subscriber.id,
        provider=AuthProvider.local,
        username=f"subscriber-{uuid.uuid4().hex}",
        password_hash="not-a-real-hash",
        is_active=True,
    )
    db_session.add(second)
    db_session.flush()
    second_id = second.id
    subscriber_id = subscriber.id
    db_session.commit()

    bind_credential_party(
        db_session, _command(credential_id, staff_id, party_id, binding_id)
    )
    with pytest.raises(CredentialBindingError) as raised:
        bind_credential_party(
            db_session,
            _command(
                second_id,
                subscriber_id,
                party_id,
                binding_id,
                expected_principal_kind=CredentialPrincipalKind.subscriber,
            ),
        )

    assert raised.value.code.endswith(".projection_collision")
