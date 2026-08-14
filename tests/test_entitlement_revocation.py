"""Security canaries: a reduced entitlement stops authorizing on the NEXT request.

Deactivating a role was never the exposure — `auth.rbac_catalog` refuses to
deactivate an assigned role, and authorization reads already filter inactive
roles out. The exposure is a principal who *loses* a role or a permission while
holding a signed JWT that still names it. Nothing can edit claims inside an
issued token, so the only mechanism that denies immediately is revoking the
authoritative session row that `auth_dependencies.require_user_auth` re-reads on
every request.

One test per property: atomic with the reduction, denies on the next request,
silent on widening/equivalent/no-op changes, rollback-safe, and cache failure
observable without preserving authorization.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from app.models.auth import Session as AuthSession
from app.models.auth import SessionStatus
from app.models.event_store import EventStore
from app.models.rbac import Permission, Role, RolePermission, SystemUserRole
from app.models.system_user import SystemUser
from app.services import (
    entitlement_revocation,
    session_hooks,
    system_user_assignments,
)
from app.services.owner_commands import CommandContext

REDUCTION_EVENT = "rbac.entitlement_reduction_revoked"


def _context() -> CommandContext:
    command_id = uuid.uuid4()
    return CommandContext(
        command_id=command_id,
        correlation_id=command_id,
        actor="service:entitlement-canary",
        scope=system_user_assignments.ASSIGNMENT_SCOPE,
        reason="verify entitlement reduction revokes live access",
        idempotency_key=f"entitlement-canary:{command_id}",
    )


def _live_session(user_id: uuid.UUID, *, token: str) -> AuthSession:
    return AuthSession(
        system_user_id=user_id,
        status=SessionStatus.active,
        token_hash=token,
        expires_at=datetime.now(UTC) + timedelta(hours=8),
    )


def _permission(db_session: Session, key: str) -> Permission:
    permission = Permission(key=key, is_active=True, is_ui_assignable=True)
    db_session.add(permission)
    db_session.flush()
    return permission


def _role_with(db_session: Session, name: str, *permissions: Permission) -> Role:
    role = Role(name=name, description=name, is_active=True)
    db_session.add(role)
    db_session.flush()
    for permission in permissions:
        db_session.add(RolePermission(role_id=role.id, permission_id=permission.id))
    db_session.flush()
    return role


@pytest.fixture()
def staff(db_session: Session):
    """A staff principal holding one role conferring one permission, with a session."""

    permission = _permission(db_session, "canary:act")
    role = _role_with(db_session, "canary_operator", permission)
    user = SystemUser(
        first_name="Canary",
        last_name="Operator",
        email=f"canary-{uuid.uuid4().hex}@dotmac.io",
        is_active=True,
    )
    db_session.add(user)
    db_session.flush()
    db_session.add(
        SystemUserRole(
            system_user_id=user.id,
            role_id=role.id,
            source=system_user_assignments.LOCAL_ROLE_SOURCE,
        )
    )
    session = _live_session(user.id, token=f"canary-{uuid.uuid4().hex}")
    db_session.add(session)
    db_session.flush()
    # Capture ids BEFORE the commit. Reading an ORM attribute afterwards
    # refreshes the expired instance, which opens a new transaction — and
    # `execute_owner_command` refuses a session that is already inside one.
    identifiers = {
        "user_id": user.id,
        "role_id": role.id,
        "permission_id": permission.id,
        "session_id": session.id,
    }
    db_session.commit()
    return identifiers


def _replace(db_session: Session, *, user_id, role_ids=(), permission_ids=()):
    return system_user_assignments.replace_system_user_assignments(
        db_session,
        system_user_assignments.ReplaceSystemUserAssignmentsCommand(
            context=_context(),
            user_id=user_id,
            role_ids=tuple(role_ids),
            direct_permission_ids=tuple(permission_ids),
        ),
    )


def test_unassigning_a_role_revokes_the_live_session(db_session, staff) -> None:
    _replace(db_session, user_id=staff["user_id"], role_ids=())
    db_session.commit()

    session = db_session.get(AuthSession, staff["session_id"])
    assert session.status is SessionStatus.revoked
    assert session.revoked_at is not None


def test_the_revoked_session_no_longer_satisfies_the_request_path(
    db_session, staff
) -> None:
    """The property that matters: the next request fails, not the next refresh.

    `require_user_auth` accepts a token only while its session row is active,
    unrevoked and unexpired. This asserts the exact predicate that dependency
    applies, so the canary fails if the revocation stops satisfying it.
    """

    _replace(db_session, user_id=staff["user_id"], role_ids=())
    db_session.commit()

    now = datetime.now(UTC)
    session = db_session.get(AuthSession, staff["session_id"])
    expires_at = session.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    still_accepted = (
        session.status == SessionStatus.active
        and session.revoked_at is None
        and expires_at > now
    )
    assert still_accepted is False


def test_a_widening_change_revokes_nothing(db_session, staff) -> None:
    """Being granted something is not a reason to be logged out."""

    extra = _permission(db_session, "canary:extra")
    extra_id = extra.id
    db_session.commit()

    _replace(
        db_session,
        user_id=staff["user_id"],
        role_ids=(staff["role_id"],),
        permission_ids=(extra_id,),
    )
    db_session.commit()

    session = db_session.get(AuthSession, staff["session_id"])
    assert session.status is SessionStatus.active
    assert (
        db_session.query(EventStore).filter_by(event_type=REDUCTION_EVENT).count() == 0
    )


def test_a_no_op_replace_revokes_nothing(db_session, staff) -> None:
    _replace(db_session, user_id=staff["user_id"], role_ids=(staff["role_id"],))
    db_session.commit()

    session = db_session.get(AuthSession, staff["session_id"])
    assert session.status is SessionStatus.active


def test_swapping_a_role_for_its_permissions_is_still_a_reduction(
    db_session, staff
) -> None:
    """Losing a role is a reduction even when the permissions are re-granted.

    Tempting to call this "equivalent" — the permission set is identical. It is
    not. Role membership is itself an authorization input: `require_role`
    checks role names directly, so a principal who keeps every permission but
    loses the role can no longer pass a role-gated route. Treating this as
    equivalent would leave a live session holding a role claim the database no
    longer backs.
    """

    _replace(
        db_session,
        user_id=staff["user_id"],
        role_ids=(),
        permission_ids=(staff["permission_id"],),
    )
    db_session.commit()

    session = db_session.get(AuthSession, staff["session_id"])
    assert session.status is SessionStatus.revoked


def test_a_role_swap_that_reduces_permissions_still_revokes(db_session, staff) -> None:
    """Role count unchanged, effective permissions smaller — still a reduction."""

    thinner = _role_with(db_session, "canary_thinner")
    thinner_id = thinner.id
    db_session.commit()

    _replace(db_session, user_id=staff["user_id"], role_ids=(thinner_id,))
    db_session.commit()

    session = db_session.get(AuthSession, staff["session_id"])
    assert session.status is SessionStatus.revoked


def test_rolling_back_preserves_both_the_assignment_and_the_session(
    db_session, staff
) -> None:
    """Atomicity: the helper joins the caller's transaction and never commits.

    Asserted against the helper rather than the command, because
    `execute_owner_command` commits before it returns — a rollback afterwards
    could not undo it, so testing there would prove nothing about atomicity.
    Here the revocation is rolled back with the caller's work, which is exactly
    the property a reducing owner depends on.
    """

    revoked = entitlement_revocation.revoke_for_entitlement_reduction(
        db_session,
        principal_type=entitlement_revocation.PRINCIPAL_SYSTEM_USER,
        principal_id=staff["user_id"],
        reason="atomicity_canary",
        correlation_id=str(uuid.uuid4()),
    )
    assert revoked == (str(staff["session_id"]),)

    db_session.rollback()

    session = db_session.get(AuthSession, staff["session_id"])
    assert session.status is SessionStatus.active
    assert session.revoked_at is None
    assert (
        db_session.query(SystemUserRole)
        .filter_by(system_user_id=staff["user_id"])
        .count()
        == 1
    )


def test_expired_and_already_revoked_sessions_are_left_alone(db_session, staff) -> None:
    """Only live sessions are touched, so revoked_at stays truthful."""

    user_id = staff["user_id"]
    expired = _live_session(user_id, token=f"expired-{uuid.uuid4().hex}")
    expired.expires_at = datetime.now(UTC) - timedelta(hours=1)
    already = _live_session(user_id, token=f"already-{uuid.uuid4().hex}")
    already.status = SessionStatus.revoked
    already.revoked_at = datetime.now(UTC) - timedelta(days=2)
    db_session.add_all((expired, already))
    db_session.flush()
    expired_id, already_id = expired.id, already.id
    original_revoked_at = already.revoked_at
    db_session.commit()

    _replace(db_session, user_id=user_id, role_ids=())
    db_session.commit()

    expired_after = db_session.get(AuthSession, expired_id)
    already_after = db_session.get(AuthSession, already_id)
    assert expired_after.status is SessionStatus.active
    stored = already_after.revoked_at
    if stored.tzinfo is None:
        stored = stored.replace(tzinfo=UTC)
    assert stored == original_revoked_at


def test_cache_invalidation_is_strict_and_runs_only_after_commit(
    db_session, staff, monkeypatch
) -> None:
    """Invalidating before commit would let a concurrent read repopulate it."""

    calls: list[tuple[str, str, bool]] = []

    def _record(principal_type: str, principal_id: str) -> int:
        session = db_session.get(AuthSession, staff["session_id"])
        calls.append(
            (principal_type, principal_id, session.status is SessionStatus.revoked)
        )
        return 1

    monkeypatch.setattr(
        entitlement_revocation.auth_cache, "invalidate_principal_strict", _record
    )

    entitlement_revocation.revoke_for_entitlement_reduction(
        db_session,
        principal_type=entitlement_revocation.PRINCIPAL_SYSTEM_USER,
        principal_id=staff["user_id"],
        reason="ordering_canary",
        correlation_id=str(uuid.uuid4()),
    )

    # Nothing may be invalidated while the reduction is uncommitted: a
    # concurrent read would repopulate the cache from rows that have not landed.
    assert calls == []

    # The invalidation is deferred, not skipped. Unit sessions run inside an
    # outer transaction, so SQLAlchemy's after_commit never fires here — assert
    # the callback was registered on the transaction, then drive it directly.
    registered = session_hooks._pop_transaction_callbacks(
        db_session, db_session.get_transaction()
    )
    assert len(registered) == 1

    registered[0](db_session)
    assert calls == [("system_user", str(staff["user_id"]), True)]


def test_a_failed_cache_invalidation_is_observable_and_does_not_restore_access(
    db_session, staff, monkeypatch
) -> None:
    """The database is the authority; a Redis failure must be loud, not fatal."""

    from app import metrics

    def _boom(principal_type: str, principal_id: str) -> int:
        raise RuntimeError("redis unavailable")

    monkeypatch.setattr(
        entitlement_revocation.auth_cache, "invalidate_principal_strict", _boom
    )
    before = metrics.ENTITLEMENT_REVOCATION_CACHE_FAILURES._value.get()

    _replace(db_session, user_id=staff["user_id"], role_ids=())
    db_session.commit()

    assert metrics.ENTITLEMENT_REVOCATION_CACHE_FAILURES._value.get() == before + 1
    session = db_session.get(AuthSession, staff["session_id"])
    assert session.status is SessionStatus.revoked
    assert (
        db_session.query(EventStore).filter_by(event_type=REDUCTION_EVENT).count() == 1
    )


def test_durable_projection_work_names_what_was_revoked(db_session, staff) -> None:
    _replace(db_session, user_id=staff["user_id"], role_ids=())
    db_session.commit()

    event = db_session.query(EventStore).filter_by(event_type=REDUCTION_EVENT).one()
    assert event.payload["principal_id"] == str(staff["user_id"])
    assert event.payload["reason"] == "system_user_assignments_reduced"
    assert event.payload["revoked_session_ids"] == [str(staff["session_id"])]


def test_an_unknown_principal_kind_fails_loudly(db_session, staff) -> None:
    """Revoking nothing for a principal the caller thought it cut off is the bug."""

    with pytest.raises(entitlement_revocation.UnknownPrincipalTypeError):
        entitlement_revocation.revoke_for_entitlement_reduction(
            db_session,
            principal_type="vendor_user",
            principal_id=staff["user_id"],
            reason="canary",
            correlation_id=str(uuid.uuid4()),
        )
