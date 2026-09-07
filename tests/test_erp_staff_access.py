from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.models.audit import AuditEvent
from app.models.auth import AuthProvider, SessionStatus, UserCredential
from app.models.auth import Session as AuthSession
from app.models.erp_staff_access import (
    ErpStaffAccountStatusProjection,
    ErpStaffLeaveRestriction,
)
from app.models.rbac import Permission, SystemUserPermission
from app.models.subscriber import UserType
from app.models.system_user import SystemUser
from app.schemas.erp_staff_access_webhook import (
    ErpStaffAccountStatusEvent,
    ErpStaffLeaveRestrictionEvent,
)
from app.services import auth_dependencies, erp_staff_access
from app.services.domain_errors import DomainError
from app.services.integrations.backoffice_contracts import (
    ERP_STAFF_ACCESS_RECONCILE_CAPABILITY,
    ERP_STAFF_ACCESS_WEBHOOK_CAPABILITY,
)
from app.services.owner_commands import CommandContext


def _context(scope: str = ERP_STAFF_ACCESS_WEBHOOK_CAPABILITY) -> CommandContext:
    command_id = uuid4()
    return CommandContext(
        command_id=command_id,
        correlation_id=command_id,
        actor=f"service:{uuid4()}",
        scope=scope,
        reason="verify ERP staff access synchronization",
        idempotency_key=str(command_id),
    )


def _staff(db_session, *, active: bool = True) -> SystemUser:
    user = SystemUser(
        first_name="Staff",
        last_name="Access",
        display_name="Staff Access",
        email=f"staff-{uuid4().hex}@example.com",
        user_type=UserType.system_user,
        is_active=active,
    )
    db_session.add(user)
    db_session.commit()
    return user


def _leave_event(
    user: SystemUser,
    *,
    restriction_id: str = "leave-1",
    version: int,
    start: datetime,
    end: datetime,
    status: str = "active",
    event_id: str | None = None,
) -> ErpStaffLeaveRestrictionEvent:
    return ErpStaffLeaveRestrictionEvent(
        event_id=event_id or f"event-{restriction_id}-{version}",
        restriction_id=restriction_id,
        erp_employee_id=f"erp-employee-{user.id}",
        system_user_id=user.id,
        effective_from=start,
        effective_until=end,
        status=status,
        version=version,
        updated_at=start + timedelta(minutes=version),
    )


def _account_event(
    user: SystemUser,
    *,
    version: int,
    status: str,
    event_id: str | None = None,
) -> ErpStaffAccountStatusEvent:
    now = datetime(2026, 1, 10, 9, tzinfo=UTC) + timedelta(minutes=version)
    return ErpStaffAccountStatusEvent(
        event_id=event_id or f"account-{status}-{version}",
        erp_employee_id=f"erp-employee-{user.id}",
        system_user_id=user.id,
        account_status=status,
        version=version,
        updated_at=now,
        reason="ERP employee status",
    )


def _apply_leave(db_session, event: ErpStaffLeaveRestrictionEvent):
    if db_session.in_transaction():
        db_session.commit()
    return erp_staff_access.apply_staff_leave_restriction_event(
        db_session,
        erp_staff_access.ApplyLeaveRestrictionCommand(
            context=_context(),
            event=event,
            delivery_id=f"delivery-{event.event_id}",
        ),
    )


def _apply_account(db_session, event: ErpStaffAccountStatusEvent):
    if db_session.in_transaction():
        db_session.commit()
    return erp_staff_access.apply_staff_account_status_event(
        db_session,
        erp_staff_access.ApplyAccountStatusCommand(
            context=_context(),
            event=event,
            delivery_id=f"delivery-{event.event_id}",
        ),
    )


def test_leave_versions_shorten_extend_cancel_and_ignore_stale(db_session) -> None:
    user = _staff(db_session)
    jan10 = datetime(2026, 1, 10, tzinfo=UTC)
    jan15 = datetime(2026, 1, 15, tzinfo=UTC)
    jan16 = datetime(2026, 1, 16, tzinfo=UTC)
    jan20 = datetime(2026, 1, 20, tzinfo=UTC)
    jan25 = datetime(2026, 1, 25, tzinfo=UTC)

    assert _apply_leave(
        db_session,
        _leave_event(user, version=1, start=jan10, end=jan20),
    ).applied
    shortened = _apply_leave(
        db_session,
        _leave_event(user, version=2, start=jan10, end=jan15),
    )
    assert shortened.applied is True
    assert (
        erp_staff_access.active_leave_restriction(
            db_session, system_user_id=user.id, at=jan16
        )
        is None
    )

    extended = _apply_leave(
        db_session,
        _leave_event(user, version=3, start=jan10, end=jan25),
    )
    assert extended.applied is True
    assert (
        erp_staff_access.active_leave_restriction(
            db_session, system_user_id=user.id, at=jan16
        )
        is not None
    )

    stale = _apply_leave(
        db_session,
        _leave_event(user, version=1, start=jan10, end=jan20, event_id="old-event"),
    )
    assert stale.applied is False
    projection = db_session.query(ErpStaffLeaveRestriction).one()
    assert projection.version == 3
    assert projection.effective_until is not None
    persisted_until = projection.effective_until
    if persisted_until.tzinfo is None:
        persisted_until = persisted_until.replace(tzinfo=UTC)
    assert persisted_until == jan25

    cancelled = _apply_leave(
        db_session,
        _leave_event(user, version=4, start=jan10, end=jan25, status="cancelled"),
    )
    assert cancelled.applied is True
    assert (
        erp_staff_access.active_leave_restriction(
            db_session, system_user_id=user.id, at=jan16
        )
        is None
    )

    stale_after_cancel = _apply_leave(
        db_session,
        _leave_event(user, version=3, start=jan10, end=jan25, event_id="old-extend"),
    )
    assert stale_after_cancel.applied is False
    assert db_session.query(ErpStaffLeaveRestriction).one().status == "cancelled"


def test_duplicate_leave_event_is_noop_and_expiry_is_data_driven(db_session) -> None:
    user = _staff(db_session)
    start = datetime(2026, 1, 10, tzinfo=UTC)
    end = datetime(2026, 1, 15, tzinfo=UTC)
    event = _leave_event(user, version=2, start=start, end=end)

    assert _apply_leave(db_session, event).applied is True
    duplicate = _apply_leave(db_session, event)

    assert duplicate.applied is False
    assert duplicate.reason == "duplicate"
    assert (
        erp_staff_access.active_leave_restriction(
            db_session, system_user_id=user.id, at=start + timedelta(hours=1)
        )
        is not None
    )
    assert (
        erp_staff_access.active_leave_restriction(
            db_session, system_user_id=user.id, at=end
        )
        is None
    )


def test_missing_erp_to_selfcare_mapping_fails_closed(db_session) -> None:
    missing_user_id = uuid4()
    event = ErpStaffLeaveRestrictionEvent(
        event_id=f"event-missing-{missing_user_id}",
        restriction_id=f"leave-missing-{missing_user_id}",
        erp_employee_id=f"erp-employee-{missing_user_id}",
        system_user_id=missing_user_id,
        effective_from=datetime(2026, 1, 10, tzinfo=UTC),
        effective_until=datetime(2026, 1, 15, tzinfo=UTC),
        status="active",
        version=1,
        updated_at=datetime(2026, 1, 10, 9, tzinfo=UTC),
    )

    with pytest.raises(DomainError) as exc_info:
        _apply_leave(db_session, event)

    assert exc_info.value.code == "auth.erp_staff_access.mapping_not_found"
    assert db_session.query(ErpStaffLeaveRestriction).count() == 0


def test_permission_guard_blocks_mutations_but_preserves_reads_and_rbac(
    db_session,
) -> None:
    user = _staff(db_session)
    permission = Permission(key="support:ticket:write", is_active=True)
    db_session.add(permission)
    db_session.commit()
    db_session.add(
        SystemUserPermission(system_user_id=user.id, permission_id=permission.id)
    )
    db_session.commit()
    now = datetime.now(UTC)
    event = _leave_event(
        user,
        version=1,
        start=now - timedelta(days=1),
        end=now + timedelta(days=1),
    )
    _apply_leave(db_session, event)
    guard = auth_dependencies.require_permission("support:ticket:write")
    auth = {
        "principal_id": str(user.id),
        "principal_type": "system_user",
        "roles": [],
        "scopes": [],
    }

    assert (
        guard(
            request=SimpleNamespace(method="GET", headers={}, state=SimpleNamespace()),
            auth=auth,
            db=db_session,
        )
        is auth
    )
    with pytest.raises(HTTPException) as exc_info:
        guard(
            request=SimpleNamespace(method="POST", headers={}, state=SimpleNamespace()),
            auth=auth,
            db=db_session,
        )
    assert exc_info.value.status_code == 403
    assert (
        db_session.query(AuditEvent)
        .filter(AuditEvent.action == "auth.erp_staff_leave_write_denied")
        .count()
        == 1
    )

    unauthorized = {
        "principal_id": str(_staff(db_session).id),
        "principal_type": "system_user",
        "roles": [],
        "scopes": [],
    }
    with pytest.raises(HTTPException) as rbac_exc:
        guard(
            request=SimpleNamespace(method="POST", headers={}, state=SimpleNamespace()),
            auth=unauthorized,
            db=db_session,
        )
    assert rbac_exc.value.detail == "Forbidden"

    api_key_auth = {
        "principal_id": str(uuid4()),
        "principal_type": "api_key",
        "roles": [],
        "scopes": ["support:ticket:write"],
    }
    assert (
        guard(
            request=SimpleNamespace(method="POST", headers={}, state=SimpleNamespace()),
            auth=api_key_auth,
            db=db_session,
        )
        is api_key_auth
    )


def test_method_permission_preserves_post_read_search_routes(db_session) -> None:
    user = _staff(db_session)
    read_perm = Permission(key="support:ticket:read", is_active=True)
    write_perm = Permission(key="support:ticket:write", is_active=True)
    db_session.add_all([read_perm, write_perm])
    db_session.commit()
    db_session.add(
        SystemUserPermission(system_user_id=user.id, permission_id=read_perm.id)
    )
    db_session.commit()
    now = datetime.now(UTC)
    _apply_leave(
        db_session,
        _leave_event(
            user,
            version=1,
            start=now - timedelta(days=1),
            end=now + timedelta(days=1),
        ),
    )
    guard = auth_dependencies.require_method_permission(
        "support:ticket:read",
        "support:ticket:write",
        read_methods=("GET", "HEAD", "OPTIONS", "POST"),
    )
    auth = {
        "principal_id": str(user.id),
        "principal_type": "system_user",
        "roles": [],
        "scopes": [],
    }

    assert (
        guard(
            request=SimpleNamespace(method="POST", headers={}, state=SimpleNamespace()),
            auth=auth,
            db=db_session,
        )
        is auth
    )


def test_erp_inactive_revokes_access_and_erp_active_restores_only_erp_state(
    db_session,
) -> None:
    user = _staff(db_session)
    credential = UserCredential(
        system_user_id=user.id,
        provider=AuthProvider.local,
        username=user.email,
        password_hash="irrelevant-test-hash",
        is_active=True,
    )
    session = AuthSession(
        system_user_id=user.id,
        status=SessionStatus.active,
        token_hash=f"token-{uuid4()}",
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    db_session.add_all([credential, session])
    db_session.commit()

    inactive = _apply_account(
        db_session,
        _account_event(user, version=1, status="inactive"),
    )
    assert inactive.applied is True
    db_session.refresh(user)
    db_session.refresh(credential)
    db_session.refresh(session)
    assert user.is_active is False
    assert credential.is_active is False
    assert session.status is SessionStatus.revoked

    active = _apply_account(
        db_session,
        _account_event(user, version=2, status="active"),
    )
    assert active.applied is True
    db_session.refresh(user)
    db_session.refresh(credential)
    assert user.is_active is True
    assert credential.is_active is True

    already_inactive = _staff(db_session, active=False)
    _apply_account(
        db_session,
        _account_event(already_inactive, version=1, status="inactive"),
    )
    _apply_account(
        db_session,
        _account_event(already_inactive, version=2, status="active"),
    )
    db_session.refresh(already_inactive)
    projection = (
        db_session.query(ErpStaffAccountStatusProjection)
        .filter(ErpStaffAccountStatusProjection.system_user_id == already_inactive.id)
        .one()
    )
    assert already_inactive.is_active is False
    assert projection.erp_inactive_applied is False


def test_out_of_order_account_status_cannot_roll_back(db_session) -> None:
    user = _staff(db_session)

    assert (
        _apply_account(
            db_session,
            _account_event(user, version=2, status="active"),
        ).applied
        is True
    )
    stale = _apply_account(
        db_session,
        _account_event(user, version=1, status="inactive"),
    )

    db_session.refresh(user)
    assert stale.applied is False
    assert user.is_active is True
    assert db_session.query(ErpStaffAccountStatusProjection).one().version == 2


def test_reconciliation_repairs_missed_staff_access_events(db_session) -> None:
    user = _staff(db_session)
    leave = _leave_event(
        user,
        version=1,
        start=datetime(2026, 1, 10, tzinfo=UTC),
        end=datetime(2026, 1, 15, tzinfo=UTC),
    )
    account = _account_event(user, version=1, status="inactive")
    db_session.commit()

    outcome = erp_staff_access.reconcile_staff_access_snapshot(
        db_session,
        erp_staff_access.ReconcileStaffAccessSnapshotCommand(
            context=_context(ERP_STAFF_ACCESS_RECONCILE_CAPABILITY),
            leave_restrictions=(leave,),
            account_statuses=(account,),
        ),
    )

    assert outcome.applied == 2
    assert erp_staff_access.active_leave_restriction(
        db_session,
        system_user_id=user.id,
        at=datetime(2026, 1, 12, tzinfo=UTC),
    )
    db_session.refresh(user)
    assert user.is_active is False


def test_reconciliation_repairs_reactivated_inactive_account_without_new_version(
    db_session,
) -> None:
    user = _staff(db_session)
    account = _account_event(user, version=1, status="inactive")
    _apply_account(db_session, account)
    db_session.refresh(user)
    assert user.is_active is False

    user.is_active = True
    db_session.commit()

    outcome = erp_staff_access.reconcile_staff_access_snapshot(
        db_session,
        erp_staff_access.ReconcileStaffAccessSnapshotCommand(
            context=_context(ERP_STAFF_ACCESS_RECONCILE_CAPABILITY),
            account_statuses=(account,),
        ),
    )

    db_session.refresh(user)
    assert outcome.applied == 1
    assert user.is_active is False
