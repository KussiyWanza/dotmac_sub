"""Staff authentication shadow parity: the evidence the read cutover waits on.

Each blocking cohort has to be provable on its own, because each needs a
different disposition. A missing projection is debt that batches clear; a Party
owning two SystemUsers, a principal holding two active credentials, or a
credential disagreeing with its principal are all cases where a Party-keyed read
returns a *different* answer rather than no answer.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from app.models.auth import (
    AuthenticationBinding,
    MFAMethod,
    MFAMethodType,
    SessionStatus,
    UserCredential,
)
from app.models.auth import Session as AuthSession
from app.models.party import Party, PartyType
from app.models.system_user import SystemUser
from app.services import staff_authentication_shadow as shadow
from app.services.operator_tenant import OPERATOR_TENANT_ID


def _party(db_session: Session) -> Party:
    # `display_name` is NOT NULL on `parties`. The value is irrelevant to every
    # assertion here — the report is aggregate and never reads it — but it has
    # to be present for the row to exist at all.
    party = Party(
        party_type=PartyType.person.value,
        display_name=f"Shadow Parity {uuid.uuid4().hex[:8]}",
    )
    db_session.add(party)
    db_session.flush()
    return party


def _staff(db_session: Session, *, party: Party | None = None) -> SystemUser:
    user = SystemUser(
        first_name="Shadow",
        last_name="Staff",
        email=f"shadow-{uuid.uuid4().hex}@dotmac.io",
        is_active=True,
        person_party_id=party.id if party is not None else None,
    )
    db_session.add(user)
    db_session.flush()
    return user


def _binding(db_session: Session) -> AuthenticationBinding:
    binding = AuthenticationBinding(
        binding_key=f"local.{uuid.uuid4().hex[:8]}",
        mechanism_code="local",
        name="Local password",
        is_active=True,
    )
    db_session.add(binding)
    db_session.flush()
    return binding


def _credential(
    db_session: Session,
    *,
    staff: SystemUser,
    party: Party | None = None,
    binding: AuthenticationBinding | None = None,
    is_active: bool = True,
    locked_until: datetime | None = None,
) -> UserCredential:
    projected = party is not None and binding is not None
    credential = UserCredential(
        system_user_id=staff.id,
        provider="local",
        username=f"shadow-{uuid.uuid4().hex[:10]}",
        password_hash="x" * 20,
        is_active=is_active,
        locked_until=locked_until,
        party_id=party.id if projected else None,
        authentication_binding_id=binding.id if projected else None,
        tenant_id=OPERATOR_TENANT_ID if projected else None,
        party_bound_at=datetime.now(UTC) if projected else None,
        party_binding_source="test" if projected else None,
        party_binding_reason="shadow parity fixture" if projected else None,
    )
    db_session.add(credential)
    db_session.flush()
    return credential


def _live_session(db_session: Session, staff: SystemUser) -> AuthSession:
    session = AuthSession(
        system_user_id=staff.id,
        status=SessionStatus.active,
        token_hash=f"shadow-{uuid.uuid4().hex}",
        expires_at=datetime.now(UTC) + timedelta(hours=4),
    )
    db_session.add(session)
    db_session.flush()
    return session


def test_a_fully_projected_population_is_cutover_safe(db_session: Session) -> None:
    binding = _binding(db_session)
    for _ in range(3):
        party = _party(db_session)
        staff = _staff(db_session, party=party)
        _credential(db_session, staff=staff, party=party, binding=binding)
    db_session.commit()

    report = shadow.staff_authentication_parity_report(db_session)

    assert report.credentials == 3
    assert report.projection_complete == 3
    assert report.projection_remaining == 0
    assert report.blocking_reasons == ()
    assert report.is_read_cutover_safe is True


def test_unprojected_credentials_are_debt_not_corruption(
    db_session: Session,
) -> None:
    party = _party(db_session)
    staff = _staff(db_session, party=party)
    _credential(db_session, staff=staff)
    db_session.commit()

    report = shadow.staff_authentication_parity_report(db_session)

    assert report.projection_remaining == 1
    assert report.party_disagreements == 0
    assert report.blocking_reasons == (shadow.BLOCKING_PROJECTION_INCOMPLETE,)


def test_an_unbound_principal_is_counted_separately(db_session: Session) -> None:
    staff = _staff(db_session)
    _credential(db_session, staff=staff)
    db_session.commit()

    report = shadow.staff_authentication_parity_report(db_session)

    assert report.principal_unbound == 1
    assert shadow.BLOCKING_PRINCIPAL_UNBOUND in report.blocking_reasons
    assert report.is_read_cutover_safe is False


def test_a_credential_disagreeing_with_its_principal_blocks(
    db_session: Session,
) -> None:
    """Neither answer can be trusted — this is corruption, not debt."""

    binding = _binding(db_session)
    principal_party = _party(db_session)
    other_party = _party(db_session)
    staff = _staff(db_session, party=principal_party)
    _credential(db_session, staff=staff, party=other_party, binding=binding)
    db_session.commit()

    report = shadow.staff_authentication_parity_report(db_session)

    assert report.party_disagreements == 1
    assert shadow.BLOCKING_PARTY_DISAGREEMENT in report.blocking_reasons
    assert report.is_read_cutover_safe is False


def test_a_party_owning_two_principals_blocks_and_taints_mfa_and_sessions(
    db_session: Session,
) -> None:
    """A Party-keyed read would union two principals' MFA methods and sessions."""

    binding = _binding(db_session)
    party = _party(db_session)
    first = _staff(db_session, party=party)
    second = _staff(db_session, party=party)
    _credential(db_session, staff=first, party=party, binding=binding)
    db_session.add(MFAMethod(system_user_id=first.id, method_type=MFAMethodType.totp))
    db_session.add(MFAMethod(system_user_id=second.id, method_type=MFAMethodType.totp))
    _live_session(db_session, first)
    _live_session(db_session, second)
    db_session.commit()

    report = shadow.staff_authentication_parity_report(db_session)

    assert report.parties_with_multiple_principals == 1
    assert report.mfa_methods_on_ambiguous_parties == 2
    assert report.live_sessions_on_ambiguous_parties == 2
    assert shadow.BLOCKING_AMBIGUOUS_PARTY_PRINCIPALS in report.blocking_reasons


def test_a_principal_with_two_active_credentials_makes_lockout_ambiguous(
    db_session: Session,
) -> None:
    """Lockout lives on the credential row, so a Party-keyed read has to choose."""

    binding = _binding(db_session)
    party = _party(db_session)
    staff = _staff(db_session, party=party)
    _credential(
        db_session,
        staff=staff,
        party=party,
        binding=binding,
        locked_until=datetime.now(UTC) + timedelta(minutes=30),
    )
    _credential(db_session, staff=staff)
    db_session.commit()

    report = shadow.staff_authentication_parity_report(db_session)

    assert report.principals_with_multiple_active_credentials == 1
    assert report.locked_credentials == 1
    assert report.locked_credentials_on_multi_credential_principals == 1
    assert shadow.BLOCKING_MULTI_CREDENTIAL_PRINCIPALS in report.blocking_reasons


def test_an_expired_lock_is_not_counted_as_locked(db_session: Session) -> None:
    binding = _binding(db_session)
    party = _party(db_session)
    staff = _staff(db_session, party=party)
    _credential(
        db_session,
        staff=staff,
        party=party,
        binding=binding,
        locked_until=datetime.now(UTC) - timedelta(minutes=5),
    )
    db_session.commit()

    report = shadow.staff_authentication_parity_report(db_session)

    assert report.locked_credentials == 0


def test_inactive_credentials_are_out_of_scope(db_session: Session) -> None:
    """A disabled credential authenticates nobody; counting it would inflate debt."""

    party = _party(db_session)
    staff = _staff(db_session, party=party)
    _credential(db_session, staff=staff, is_active=False)
    db_session.commit()

    report = shadow.staff_authentication_parity_report(db_session)

    assert report.credentials == 0


def test_revoked_and_expired_sessions_are_not_live(db_session: Session) -> None:
    binding = _binding(db_session)
    party = _party(db_session)
    staff = _staff(db_session, party=party)
    _credential(db_session, staff=staff, party=party, binding=binding)
    revoked = _live_session(db_session, staff)
    revoked.status = SessionStatus.revoked
    revoked.revoked_at = datetime.now(UTC)
    expired = _live_session(db_session, staff)
    expired.expires_at = datetime.now(UTC) - timedelta(hours=1)
    db_session.commit()

    report = shadow.staff_authentication_parity_report(db_session)

    assert report.live_sessions == 0


def test_the_report_carries_no_identifiers(db_session: Session) -> None:
    """It has to be safe to paste into a review of a production restore."""

    binding = _binding(db_session)
    party = _party(db_session)
    staff = _staff(db_session, party=party)
    _credential(db_session, staff=staff, party=party, binding=binding)
    db_session.commit()

    payload = shadow.staff_authentication_parity_report(db_session).as_dict()

    for value in payload.values():
        assert isinstance(value, (int, bool, list))
    assert all(isinstance(reason, str) for reason in payload["blocking_reasons"])
    rendered = repr(payload)
    assert str(staff.id) not in rendered
    assert str(party.id) not in rendered
    assert staff.email not in rendered


@pytest.mark.parametrize(
    "reason",
    (
        shadow.BLOCKING_PARTY_DISAGREEMENT,
        shadow.BLOCKING_PRINCIPAL_UNBOUND,
        shadow.BLOCKING_AMBIGUOUS_PARTY_PRINCIPALS,
        shadow.BLOCKING_MULTI_CREDENTIAL_PRINCIPALS,
        shadow.BLOCKING_PROJECTION_INCOMPLETE,
    ),
)
def test_every_blocking_code_is_reachable_and_stable(reason: str) -> None:
    """These codes end up in a runbook and a gate; they must not drift silently."""

    assert reason == reason.lower()
    assert " " not in reason
