import hashlib
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from starlette.requests import Request

from app.models.auth import AuthProvider, SessionStatus, UserCredential
from app.models.auth import Session as AuthSession
from app.models.system_user import SystemUser
from app.services import staff_party_authentication as resolver
from app.services.auth_flow import AuthFlow, hash_password
from app.services.staff_party_authentication import (
    StaffProjectionError,
    StaffProjectionRefusal,
)
from tests.staff_identity_fixtures import add_bound_staff_user, project_staff_login


def test_staff_projection_error_allows_runtime_traceback_state() -> None:
    """Domain errors must remain usable by context managers and web adapters."""

    credential_id = uuid4()
    error = StaffProjectionError(
        StaffProjectionRefusal.projection_missing,
        credential_id,
    )

    error.__traceback__ = None

    assert error.refusal is StaffProjectionRefusal.projection_missing
    assert error.credential_id == credential_id
    assert str(error) == f"staff_projection_missing for credential {credential_id}"


# ---------------------------------------------------------------------------
# Behaviour of the cutover, against real model state.
#
# These deliberately do NOT mock the resolver. A mocked call proves the caller
# said the right words; only real rows prove which key actually resolved.
# ---------------------------------------------------------------------------


def _bound_login(db_session, email: str = "cutover@example.test"):
    """A fully projected staff login: principal, Party, credential."""
    user, person = add_bound_staff_user(db_session, email=email)
    credential = UserCredential(
        system_user_id=user.id,
        provider=AuthProvider.local,
        username=email,
        password_hash=hash_password("secret"),
        is_active=True,
    )
    project_staff_login(db_session, user=user, credential=credential)
    db_session.commit()
    return user, person, credential


def _request(*, device_id: str | None = None) -> Request:
    headers = [(b"user-agent", b"staff-party-cutover-test")]
    if device_id is not None:
        headers.append((b"x-device-id", device_id.encode()))
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/auth/refresh",
            "headers": headers,
            "client": ("127.0.0.1", 12345),
        }
    )


def _staff_session(
    db_session,
    *,
    refresh_token: str,
    system_user_id,
    party_id,
    device_id: str | None = None,
) -> AuthSession:
    session = AuthSession(
        system_user_id=system_user_id,
        party_id=party_id,
        status=SessionStatus.active,
        token_hash=hashlib.sha256(refresh_token.encode()).hexdigest(),
        device_id=device_id,
        created_at=datetime.now(UTC),
        last_seen_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    db_session.add(session)
    db_session.commit()
    return session


def _unproject_credential(credential) -> None:
    """Clear the WHOLE credential projection tuple.

    `ck_user_credentials_party_binding_projection` is all-present-or-all-absent,
    so nulling `party_id` alone is not an unprojected credential — it is an
    illegal row the database refuses. An unprojected credential is one that
    carries none of the evidence.
    """

    credential.party_id = None
    credential.authentication_binding_id = None
    credential.tenant_id = None
    credential.party_bound_at = None
    credential.party_binding_source = None
    credential.party_binding_reason = None


def _unbind_principal(user) -> None:
    """Clear the WHOLE principal binding tuple, for the same reason."""

    user.person_party_id = None
    user.party_bound_at = None
    user.party_binding_source = None
    user.party_binding_reason = None


def test_login_resolves_by_party_not_by_the_legacy_key(db_session) -> None:
    """The primitive answers from the Party, and the answer is the principal."""

    user, person, credential = _bound_login(db_session, "party-keyed@example.test")

    resolved = resolver.resolve_staff_principal(db_session, credential)

    assert resolved.id == user.id
    assert resolved.person_party_id == person.id


def test_resolution_refuses_a_credential_with_no_projection(db_session) -> None:
    """Missing projection fails closed — it does not fall back to system_user_id."""

    user, _person, credential = _bound_login(db_session, "unprojected@example.test")
    _unproject_credential(credential)
    db_session.commit()

    with pytest.raises(resolver.StaffProjectionError) as exc:
        resolver.resolve_staff_principal(db_session, credential)

    assert exc.value.refusal is resolver.StaffProjectionRefusal.projection_missing
    # The legacy key is still sitting right there and was NOT used.
    assert credential.system_user_id == user.id


def test_resolution_refuses_a_conflicting_bound_pair(db_session) -> None:
    """Party and staff context disagree: never guess which one is right."""

    _user, _person, credential = _bound_login(db_session, "conflict@example.test")
    other, _other_person = add_bound_staff_user(db_session, email="other@example.test")
    credential.system_user_id = other.id
    db_session.commit()

    with pytest.raises(resolver.StaffProjectionError) as exc:
        resolver.resolve_staff_principal(db_session, credential)

    assert exc.value.refusal is resolver.StaffProjectionRefusal.projection_conflict


def test_resolution_refuses_when_the_party_owns_no_principal(db_session) -> None:
    _user, person, credential = _bound_login(db_session, "orphaned@example.test")
    for principal in (
        db_session.execute(
            select(SystemUser).where(SystemUser.person_party_id == person.id)
        )
        .scalars()
        .all()
    ):
        _unbind_principal(principal)
    db_session.commit()

    with pytest.raises(resolver.StaffProjectionError) as exc:
        resolver.resolve_staff_principal(db_session, credential)

    assert exc.value.refusal is resolver.StaffProjectionRefusal.party_has_no_principal


def test_an_ambiguous_party_is_unreachable_not_merely_unhandled(db_session) -> None:
    """The refusal branch exists for a state the catalog forbids.

    `uq_system_users_person_party_id` makes a Party owning two principals
    impossible, so this cannot be exercised by constructing one — the insert is
    refused before the resolver is ever called. That IS the proof: reaching the
    resolver's `party_owns_multiple_principals` branch in production would mean
    the constraint had been lost, not that the population drifted.

    Asserting the constraint bites is therefore the honest test. A test that
    tried to build the state and expected a resolver refusal would be asserting
    something the database never allows.
    """

    _user, person, _credential = _bound_login(db_session, "ambiguous@example.test")
    twin, _twin_person = add_bound_staff_user(db_session, email="twin@example.test")

    twin.person_party_id = person.id
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


def test_a_populated_session_pair_resolves_by_party(db_session) -> None:
    """The bound pair: identity resolves, staff context is compared."""

    user, person, _credential = _bound_login(db_session, "session-pair@example.test")

    resolved = resolver.resolve_staff_principal_by_party(db_session, person.id, user.id)

    assert resolved.id == user.id


def test_a_mismatched_session_pair_fails_closed(db_session) -> None:
    """Paired specificity: the populated field is enforced, not merely tolerated.

    Without this, a reader that ignores `session.party_id` entirely still passes
    the matching case — which is exactly how the gap survived review once.
    """

    _user, person, _credential = _bound_login(db_session, "mismatch@example.test")
    impostor, _impostor_person = add_bound_staff_user(
        db_session, email="impostor@example.test"
    )
    db_session.commit()

    with pytest.raises(resolver.StaffProjectionError) as exc:
        resolver.resolve_staff_principal_by_party(db_session, person.id, impostor.id)

    assert exc.value.refusal is resolver.StaffProjectionRefusal.projection_conflict


def test_refresh_resolves_a_populated_session_from_party(
    db_session, monkeypatch
) -> None:
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    user, person, _credential = _bound_login(db_session, "refresh-party@example.test")
    refresh_token = "refresh-party-token"
    session = _staff_session(
        db_session,
        refresh_token=refresh_token,
        system_user_id=user.id,
        party_id=person.id,
    )

    result = AuthFlow.refresh(db_session, refresh_token, _request())

    db_session.refresh(session)
    assert result["refresh_token"] != refresh_token
    assert session.party_id == person.id
    assert session.system_user_id == user.id


def test_refresh_refuses_a_mismatched_pair_before_rotating(
    db_session, monkeypatch
) -> None:
    """The Party decision must happen before any successful-refresh mutation."""

    monkeypatch.setenv("JWT_SECRET", "test-secret")
    _user, person, _credential = _bound_login(
        db_session, "refresh-mismatch@example.test"
    )
    other, _other_party = add_bound_staff_user(
        db_session, email="refresh-other@example.test"
    )
    refresh_token = "refresh-mismatch-token"
    session = _staff_session(
        db_session,
        refresh_token=refresh_token,
        system_user_id=other.id,
        party_id=person.id,
    )
    before = (
        session.token_hash,
        session.previous_token_hash,
        session.token_rotated_at,
        session.last_seen_at,
    )

    with pytest.raises(HTTPException) as exc:
        AuthFlow.refresh(db_session, refresh_token, _request())

    assert exc.value.status_code == 401
    db_session.expire_all()
    persisted = db_session.get(AuthSession, session.id)
    assert persisted is not None
    assert (
        persisted.token_hash,
        persisted.previous_token_hash,
        persisted.token_rotated_at,
        persisted.last_seen_at,
    ) == before


def test_session_resolution_refuses_a_missing_party_even_with_a_legacy_key(
    db_session,
) -> None:
    """Deploy 2 has no assertion-first compatibility branch."""

    user, _person, _credential = _bound_login(db_session, "strict-session@example.test")

    with pytest.raises(resolver.StaffProjectionError) as exc:
        resolver.resolve_staff_session_principal(
            db_session,
            party_id=None,
            system_user_id=user.id,
            reference=uuid4(),
        )

    assert exc.value.refusal is resolver.StaffProjectionRefusal.projection_missing


def test_new_staff_session_is_minted_from_a_typed_party_binding(
    db_session, monkeypatch
) -> None:
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    user, person, _credential = _bound_login(db_session, "mint-party@example.test")
    binding = resolver.StaffSessionBinding(
        party_id=person.id,
        system_user_id=user.id,
    )

    AuthFlow._issue_tokens(
        db_session,
        "system_user",
        str(user.id),
        _request(),
        staff_binding=binding,
    )

    session = (
        db_session.execute(
            select(AuthSession).where(AuthSession.system_user_id == user.id)
        )
        .scalars()
        .one()
    )
    assert session.party_id == person.id
    assert session.system_user_id == user.id


def test_session_mint_refuses_a_conflicting_binding_before_device_revocation(
    db_session, monkeypatch
) -> None:
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    user, person, _credential = _bound_login(db_session, "mint-owner@example.test")
    other, _other_party = add_bound_staff_user(
        db_session, email="mint-other@example.test"
    )
    existing = _staff_session(
        db_session,
        refresh_token="existing-device-token",
        system_user_id=other.id,
        party_id=other.person_party_id,
        device_id="known-device",
    )
    conflicting = resolver.StaffSessionBinding(
        party_id=person.id,
        system_user_id=other.id,
    )

    with pytest.raises(resolver.StaffProjectionError) as exc:
        AuthFlow._issue_tokens(
            db_session,
            "system_user",
            str(other.id),
            _request(device_id="known-device"),
            staff_binding=conflicting,
        )

    assert exc.value.refusal is resolver.StaffProjectionRefusal.projection_conflict
    db_session.expire_all()
    persisted = db_session.get(AuthSession, existing.id)
    assert persisted is not None
    assert persisted.status is SessionStatus.active
    assert persisted.revoked_at is None


def test_a_refused_projection_mints_no_session_and_mutates_nothing(db_session) -> None:
    """The consequence, not just the refusal.

    A refused staff login must leave no session, no rotated token, and no
    successful-login mutation on the credential.
    """

    user, _person, credential = _bound_login(db_session, "no-mint@example.test")
    _unproject_credential(credential)
    credential.failed_login_attempts = 3
    db_session.commit()
    sessions_before = (
        db_session.execute(
            select(AuthSession).where(AuthSession.system_user_id == user.id)
        )
        .scalars()
        .all()
    )

    with pytest.raises(resolver.StaffProjectionError):
        resolver.resolve_staff_principal(db_session, credential)

    db_session.expire_all()
    persisted = db_session.get(UserCredential, credential.id)
    sessions_after = (
        db_session.execute(
            select(AuthSession).where(AuthSession.system_user_id == user.id)
        )
        .scalars()
        .all()
    )

    assert len(sessions_after) == len(sessions_before)
    assert persisted.last_login_at is None
    assert persisted.failed_login_attempts == 3


def test_lockout_stays_on_the_credential_not_the_party(db_session) -> None:
    """Lockout is credential state; the cutover moved identity, not context."""

    _user, _person, credential = _bound_login(db_session, "lockout@example.test")
    from datetime import UTC, datetime, timedelta

    credential.locked_until = datetime.now(UTC) + timedelta(minutes=10)
    db_session.commit()
    db_session.expire_all()

    persisted = db_session.get(UserCredential, credential.id)
    assert persisted.locked_until is not None


def test_mfa_stays_bound_to_the_resolved_staff_context(db_session) -> None:
    """MFA keys on `system_user_id`, and that is still the resolved principal."""

    user, person, credential = _bound_login(db_session, "mfa@example.test")

    resolved = resolver.resolve_staff_principal(db_session, credential)

    # MFA lookups key on the resolved principal, so Party-keyed resolution
    # carries them without their queries changing.
    assert resolved.id == user.id
    assert resolved.person_party_id == person.id
