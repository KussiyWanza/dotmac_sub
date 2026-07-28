"""Atomic owner and durable-consequence tests for staff provisioning."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.models.audit import AuditEvent
from app.models.auth import AuthProvider, UserCredential
from app.models.event_store import EventStore
from app.models.notification import CommunicationIntentRecord, Notification
from app.models.party import Party, PartyType
from app.models.rbac import Role, SystemUserRole
from app.models.system_user import SystemUser
from app.services import credential_recovery, staff_provisioning
from app.services.ephemeral_communication_actions import (
    EPHEMERAL_ACTION_METADATA_KEY,
    STAFF_ACCOUNT_INVITE_ACTION,
    materialize_email,
)
from app.services.owner_commands import CommandContext


def _context(key: str = "staff-owner-test") -> CommandContext:
    command_id = uuid.uuid4()
    return CommandContext(
        command_id=command_id,
        correlation_id=command_id,
        actor="api_key:erp-hr-test",
        scope=staff_provisioning.STAFF_ASSIGN_SCOPE,
        reason="verify staff owner semantics",
        idempotency_key=key,
    )


def _role(db_session, name: str = "staff") -> Role:
    role = db_session.query(Role).filter(Role.name == name).one_or_none()
    if role is None:
        role = Role(name=name, description=f"{name} role")
        db_session.add(role)
    db_session.commit()
    return role


def _command(
    *, send_invite: bool = False
) -> staff_provisioning.ProvisionStaffAccountCommand:
    return staff_provisioning.ProvisionStaffAccountCommand(
        context=_context(),
        email="owner.test@dotmac.io",
        first_name="Owner",
        last_name="Test",
        role_names=("staff",),
        send_invite=send_invite,
    )


def test_provision_commits_identity_grant_audit_and_event_together(db_session) -> None:
    _role(db_session)

    result = staff_provisioning.provision_staff_account(db_session, _command())

    assert result.created is True
    assert result.role_names == ("staff",)
    assert not db_session.in_transaction()
    user = db_session.get(SystemUser, result.user_id)
    credential = (
        db_session.query(UserCredential)
        .filter(UserCredential.system_user_id == result.user_id)
        .one()
    )
    grant = (
        db_session.query(SystemUserRole)
        .filter(SystemUserRole.system_user_id == result.user_id)
        .one()
    )
    audit = (
        db_session.query(AuditEvent)
        .filter(AuditEvent.entity_id == str(result.user_id))
        .one()
    )
    event = (
        db_session.query(EventStore)
        .filter(EventStore.event_type == "staff_account.provisioned")
        .one()
    )

    assert user.email == "owner.test@dotmac.io"
    assert result.person_party_id is not None
    assert user.person_party_id == result.person_party_id
    person = db_session.get(Party, result.person_party_id)
    assert person is not None
    assert person.party_type == PartyType.person.value
    assert person.display_name == "Owner Test"
    assert user.party_binding_source == "auth.staff_provisioning:erp_hr"
    assert user.party_binding_reason == "verify staff owner semantics"
    assert credential.provider == AuthProvider.local
    assert credential.must_change_password is True
    assert credential.password_hash
    assert grant.source == staff_provisioning.ERP_HR_ROLE_SOURCE
    assert audit.action == "auth.staff_account_provisioned"
    assert event.payload["user_id"] == str(result.user_id)
    assert event.payload["email_sha256"]
    assert "email" not in event.payload
    assert "token" not in event.payload


def test_late_failure_rolls_back_every_staff_write(db_session, monkeypatch) -> None:
    _role(db_session)

    def fail_audit(*_args, **_kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(staff_provisioning, "stage_audit_event", fail_audit)

    with pytest.raises(RuntimeError, match="audit unavailable"):
        staff_provisioning.provision_staff_account(db_session, _command())

    assert not db_session.in_transaction()
    assert (
        db_session.query(SystemUser)
        .filter(SystemUser.email == "owner.test@dotmac.io")
        .count()
        == 0
    )
    assert db_session.query(UserCredential).count() == 0
    assert db_session.query(SystemUserRole).count() == 0
    assert db_session.query(Party).count() == 0
    assert db_session.query(AuditEvent).count() == 0
    assert db_session.query(EventStore).count() == 0


def test_invite_is_deduplicated_and_contains_no_persisted_capability(
    db_session,
) -> None:
    _role(db_session)

    result = staff_provisioning.provision_staff_account(
        db_session, _command(send_invite=True)
    )

    intent = db_session.query(CommunicationIntentRecord).one()
    notification = db_session.query(Notification).one()
    action = notification.metadata_[EPHEMERAL_ACTION_METADATA_KEY]
    assert result.invite_requested is True
    assert intent.dedupe_key is not None
    assert notification.audience_type == "system_user"
    assert notification.audience_id == result.user_id
    assert notification.body is None
    assert action["type"] == STAFF_ACCOUNT_INVITE_ACTION
    assert set(action["context"]) == {"user_id", "email_sha256"}
    assert "token" not in str(intent.metadata_).lower()
    assert "token" not in str(notification.metadata_).lower()


def test_invite_capability_is_materialized_for_exact_principal(
    db_session, monkeypatch
) -> None:
    _role(db_session)
    result = staff_provisioning.provision_staff_account(
        db_session, _command(send_invite=True)
    )
    notification = db_session.query(Notification).one()
    requested: list[uuid.UUID] = []
    rendered: list[dict[str, object]] = []

    def exact_reset(_db, *, principal_type, principal_id, ttl_minutes=None):
        assert principal_type == "system_user"
        user_id = principal_id
        requested.append(user_id)
        return credential_recovery.PasswordResetCapability(
            token="in-memory-capability",
            email="materialized.staff@example.com",
            person_name="Materialized Staff",
            principal_type=principal_type,
            principal_id=principal_id,
            ttl_minutes=60,
        )

    def render_invite(_db, **kwargs):
        rendered.append(kwargs)
        return SimpleNamespace(
            subject="Staff invite",
            body_html="<p>Invite</p>",
            body_text="Invite",
        )

    from app.services import email as email_service

    monkeypatch.setattr(
        staff_provisioning.credential_recovery,
        "issue_exact_reset_capability",
        exact_reset,
    )
    monkeypatch.setattr(email_service, "render_user_invite_email", render_invite)

    content = materialize_email(db_session, notification)

    assert requested == [result.user_id]
    assert rendered[0]["reset_token"] == "in-memory-capability"
    assert rendered[0]["token_in_fragment"] is True
    assert content.subject == "Staff invite"
    assert notification.body is None
    assert "in-memory-capability" not in str(notification.metadata_)


def test_unknown_role_rolls_back_identity_bootstrap(db_session) -> None:
    command = _command()

    with pytest.raises(staff_provisioning.UnknownRoleError) as captured:
        staff_provisioning.provision_staff_account(db_session, command)

    assert captured.value.code == "auth.staff_provisioning.unknown_roles"
    assert not db_session.in_transaction()
    assert (
        db_session.query(SystemUser).filter(SystemUser.email == command.email).count()
        == 0
    )
    assert (
        db_session.query(UserCredential)
        .join(SystemUser, SystemUser.id == UserCredential.system_user_id)
        .filter(SystemUser.email == command.email)
        .count()
        == 0
    )


def test_local_admin_create_uses_same_atomic_provisioning_boundary(
    db_session,
) -> None:
    role = Role(name="local-staff", description="Local staff")
    db_session.add(role)
    db_session.flush()
    role_id = role.id
    db_session.commit()

    result = staff_provisioning.create_local_staff_account(
        db_session,
        staff_provisioning.CreateLocalStaffAccountCommand(
            context=_context("local-admin-create"),
            email="local.staff@dotmac.io",
            first_name="Local",
            last_name="Staff",
            role_id=role_id,
            send_invite=False,
        ),
    )

    grant = (
        db_session.query(SystemUserRole)
        .filter(SystemUserRole.system_user_id == result.user_id)
        .one()
    )
    assert result.created is True
    assert result.person_party_id is not None
    assert result.role_names == ("local-staff",)
    assert grant.source == "local"
    user = db_session.get(SystemUser, result.user_id)
    assert user is not None
    assert user.person_party_id == result.person_party_id
    assert user.party_binding_source == "auth.staff_provisioning:local"
    assert (
        db_session.query(AuditEvent)
        .filter(AuditEvent.entity_id == str(result.user_id))
        .one()
        .action
        == "auth.staff_account_provisioned"
    )
    assert (
        db_session.query(EventStore)
        .filter(EventStore.event_type == "staff_account.provisioned")
        .count()
        == 1
    )
