"""Atomic owner and durable-consequence tests for staff provisioning."""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.models.audit import AuditEvent
from app.models.auth import AuthProvider, SessionStatus, UserCredential
from app.models.auth import Session as AuthSession
from app.models.event_store import EventStore
from app.models.notification import CommunicationIntentRecord, Notification
from app.models.party import Party, PartyType
from app.models.rbac import Role, SystemUserRole
from app.models.system_user import SystemUser
from app.services import auth_flow, credential_recovery, staff_provisioning
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


def _staff_command(
    email: str,
    *,
    key: str,
) -> staff_provisioning.ProvisionStaffAccountCommand:
    return staff_provisioning.ProvisionStaffAccountCommand(
        context=_context(key),
        email=email,
        first_name="Identity",
        last_name="Test",
        role_names=("staff",),
        send_invite=False,
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


def test_email_update_reconciles_disabled_credential_and_revokes_sessions(
    db_session,
) -> None:
    _role(db_session)
    result = staff_provisioning.provision_staff_account(
        db_session,
        _staff_command("disabled.identity@dotmac.io", key="disabled-identity"),
    )
    user = db_session.get(SystemUser, result.user_id)
    credential = (
        db_session.query(UserCredential)
        .filter(UserCredential.system_user_id == result.user_id)
        .one()
    )
    user.is_active = False
    credential.is_active = False
    active_session = AuthSession(
        system_user_id=user.id,
        status=SessionStatus.active,
        token_hash="staff-identity-active-session",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    db_session.add(active_session)
    db_session.commit()

    outcome = staff_provisioning.update_staff_identity(
        db_session,
        staff_provisioning.UpdateStaffIdentityCommand(
            context=_context("update-disabled-identity"),
            user_id=result.user_id,
            fields=frozenset({staff_provisioning.StaffIdentityField.email}),
            email="corrected.identity@dotmac.io",
        ),
    )

    db_session.refresh(user)
    db_session.refresh(credential)
    db_session.refresh(active_session)
    assert outcome.credential_reconciled is True
    assert outcome.credential_active is False
    assert outcome.revoked_sessions == 1
    assert user.email == "corrected.identity@dotmac.io"
    assert credential.username == "corrected.identity@dotmac.io"
    assert credential.is_active is False
    assert active_session.status == SessionStatus.revoked
    assert active_session.revoked_at is not None


def test_email_update_retires_old_active_login_identifier(db_session) -> None:
    _role(db_session)
    result = staff_provisioning.provision_staff_account(
        db_session,
        _staff_command("old.login@dotmac.io", key="active-login-identity"),
    )

    staff_provisioning.update_staff_identity(
        db_session,
        staff_provisioning.UpdateStaffIdentityCommand(
            context=_context("retire-old-login-identity"),
            user_id=result.user_id,
            fields=frozenset({staff_provisioning.StaffIdentityField.email}),
            email="new.login@dotmac.io",
        ),
    )

    old_credential = auth_flow._resolve_login_credential(
        db_session,
        provider=AuthProvider.local,
        identifier="old.login@dotmac.io",
    )
    new_credential = auth_flow._resolve_login_credential(
        db_session,
        provider=AuthProvider.local,
        identifier="new.login@dotmac.io",
    )
    assert old_credential is None
    assert new_credential is not None
    assert new_credential.system_user_id == result.user_id


def test_activation_reconciles_stale_disabled_credential(db_session) -> None:
    _role(db_session)
    result = staff_provisioning.provision_staff_account(
        db_session,
        _staff_command("activation.identity@dotmac.io", key="activation-identity"),
    )
    user = db_session.get(SystemUser, result.user_id)
    credential = (
        db_session.query(UserCredential)
        .filter(UserCredential.system_user_id == result.user_id)
        .one()
    )
    user.is_active = False
    credential.is_active = False
    credential.username = "old.activation@dotmac.io"
    db_session.commit()

    outcome = staff_provisioning.set_staff_account_active(
        db_session,
        staff_provisioning.SetStaffAccountActiveCommand(
            context=_context("activate-reconciled-identity"),
            user_id=result.user_id,
            is_active=True,
        ),
    )

    db_session.refresh(user)
    db_session.refresh(credential)
    assert outcome.changed is True
    assert user.is_active is True
    assert credential.is_active is True
    assert credential.username == user.email


def test_activation_recreates_missing_local_credential(db_session) -> None:
    _role(db_session)
    result = staff_provisioning.provision_staff_account(
        db_session,
        _staff_command("missing.identity@dotmac.io", key="missing-identity"),
    )
    user = db_session.get(SystemUser, result.user_id)
    credential = (
        db_session.query(UserCredential)
        .filter(UserCredential.system_user_id == result.user_id)
        .one()
    )
    user.is_active = False
    db_session.delete(credential)
    db_session.commit()

    outcome = staff_provisioning.set_staff_account_active(
        db_session,
        staff_provisioning.SetStaffAccountActiveCommand(
            context=_context("activate-missing-identity"),
            user_id=result.user_id,
            is_active=True,
        ),
    )

    replacement = (
        db_session.query(UserCredential)
        .filter(UserCredential.system_user_id == result.user_id)
        .one()
    )
    assert outcome.changed is True
    assert replacement.username == "missing.identity@dotmac.io"
    assert replacement.is_active is True
    assert replacement.must_change_password is True


def test_recovery_preparation_reconciles_nonblank_stale_username(db_session) -> None:
    _role(db_session)
    result = staff_provisioning.provision_staff_account(
        db_session,
        _staff_command("recovery.identity@dotmac.io", key="recovery-identity"),
    )
    credential = (
        db_session.query(UserCredential)
        .filter(UserCredential.system_user_id == result.user_id)
        .one()
    )
    credential.username = "old.recovery@dotmac.io"
    credential.is_active = False
    credential.must_change_password = False
    db_session.commit()

    outcome = staff_provisioning.prepare_staff_credential_recovery(
        db_session,
        staff_provisioning.PrepareStaffCredentialRecoveryCommand(
            context=_context("prepare-recovery-identity"),
            user_id=result.user_id,
        ),
    )

    db_session.refresh(credential)
    assert outcome.created is False
    assert outcome.changed is True
    assert credential.username == "recovery.identity@dotmac.io"
    assert credential.is_active is True
    assert credential.must_change_password is True


def test_identity_conflict_rolls_back_profile_and_credential(db_session) -> None:
    _role(db_session)
    first = staff_provisioning.provision_staff_account(
        db_session,
        _staff_command("first.identity@dotmac.io", key="first-identity"),
    )
    staff_provisioning.provision_staff_account(
        db_session,
        _staff_command("occupied.identity@dotmac.io", key="occupied-identity"),
    )

    with pytest.raises(staff_provisioning.StaffProvisioningError) as captured:
        staff_provisioning.update_staff_identity(
            db_session,
            staff_provisioning.UpdateStaffIdentityCommand(
                context=_context("conflicting-identity"),
                user_id=first.user_id,
                fields=frozenset({staff_provisioning.StaffIdentityField.email}),
                email="occupied.identity@dotmac.io",
            ),
        )

    assert captured.value.code == "auth.staff_provisioning.identity_conflict"
    assert not db_session.in_transaction()
    user = db_session.get(SystemUser, first.user_id)
    credential = (
        db_session.query(UserCredential)
        .filter(UserCredential.system_user_id == first.user_id)
        .one()
    )
    assert user.email == "first.identity@dotmac.io"
    assert credential.username == "first.identity@dotmac.io"


def test_drift_preview_reports_username_and_activation_mismatches(db_session) -> None:
    _role(db_session)
    result = staff_provisioning.provision_staff_account(
        db_session,
        _staff_command("drift.identity@dotmac.io", key="drift-identity"),
    )
    credential = (
        db_session.query(UserCredential)
        .filter(UserCredential.system_user_id == result.user_id)
        .one()
    )
    credential.username = "old.drift@dotmac.io"
    credential.is_active = False
    db_session.commit()

    drift = staff_provisioning.list_staff_login_identity_drift(db_session)
    issues = {item.issue for item in drift if item.user_id == result.user_id}

    assert issues == {
        staff_provisioning.StaffLoginIdentityIssue.username_mismatch,
        staff_provisioning.StaffLoginIdentityIssue.activation_mismatch,
    }
    assert all("@" not in item.email_sha256 for item in drift)


def test_reviewed_repair_fails_closed_on_stale_email_evidence(db_session) -> None:
    _role(db_session)
    result = staff_provisioning.provision_staff_account(
        db_session,
        _staff_command("repair.identity@dotmac.io", key="repair-identity"),
    )
    credential = (
        db_session.query(UserCredential)
        .filter(UserCredential.system_user_id == result.user_id)
        .one()
    )
    credential.username = "old.repair@dotmac.io"
    db_session.commit()

    with pytest.raises(staff_provisioning.StaffProvisioningError) as captured:
        staff_provisioning.reconcile_staff_login_identity(
            db_session,
            staff_provisioning.ReconcileStaffLoginIdentityCommand(
                context=_context("stale-reviewed-repair"),
                user_id=result.user_id,
                expected_email_sha256="0" * 64,
            ),
        )

    assert captured.value.code == "auth.staff_provisioning.stale_identity_evidence"
    assert not db_session.in_transaction()
    db_session.refresh(credential)
    assert credential.username == "old.repair@dotmac.io"


def test_reviewed_repair_recreates_missing_inactive_credential(db_session) -> None:
    _role(db_session)
    email = "missing.repair@dotmac.io"
    result = staff_provisioning.provision_staff_account(
        db_session,
        _staff_command(email, key="missing-repair"),
    )
    user = db_session.get(SystemUser, result.user_id)
    credential = (
        db_session.query(UserCredential)
        .filter(UserCredential.system_user_id == result.user_id)
        .one()
    )
    user.is_active = False
    db_session.delete(credential)
    db_session.commit()

    outcome = staff_provisioning.reconcile_staff_login_identity(
        db_session,
        staff_provisioning.ReconcileStaffLoginIdentityCommand(
            context=_context("repair-missing-inactive-identity"),
            user_id=result.user_id,
            expected_email_sha256=hashlib.sha256(email.encode()).hexdigest(),
        ),
    )

    replacement = db_session.get(UserCredential, outcome.credential_id)
    assert outcome.credential_created is True
    assert outcome.changed is True
    assert replacement is not None
    assert replacement.username == email
    assert replacement.is_active is False


def _radius_credential(db_session, user_id) -> UserCredential:
    """A second, non-local authentication mechanism on the same principal."""
    credential = UserCredential(
        system_user_id=user_id,
        provider=AuthProvider.radius,
        username="radius.identity@dotmac.io",
        password_hash="x" * 60,
        is_active=True,
    )
    db_session.add(credential)
    db_session.commit()
    return credential


def test_deactivation_closes_every_credential_mechanism_not_only_local(
    db_session,
) -> None:
    """RADIUS canary: deactivation is a statement about the PRINCIPAL.

    The sweep used to filter `provider == local`, so a RADIUS credential
    survived deactivation and kept an authentication path open on an account
    the operator believed was closed. Nothing in production exercised it, which
    is exactly why it needs a canary rather than a comment.
    """

    _role(db_session)
    result = staff_provisioning.provision_staff_account(
        db_session,
        _staff_command("radius.identity@dotmac.io", key="radius-identity"),
    )
    radius = _radius_credential(db_session, result.user_id)

    staff_provisioning.set_staff_account_active(
        db_session,
        staff_provisioning.SetStaffAccountActiveCommand(
            context=_context("deactivate-radius-identity"),
            user_id=result.user_id,
            is_active=False,
        ),
    )

    db_session.expire_all()
    remaining = (
        db_session.query(UserCredential)
        .filter(
            UserCredential.system_user_id == result.user_id,
            UserCredential.is_active.is_(True),
        )
        .all()
    )
    assert remaining == [], "a deactivated principal must hold no active credential"
    assert db_session.get(UserCredential, radius.id).is_active is False


def test_deactivation_remediates_a_principal_already_inactive(db_session) -> None:
    """Remediation must converge, not no-op on an unchanged `is_active`.

    Seven production principals were deactivated before this owner existed and
    kept credentials or unrevoked sessions. Re-invoking the owner is the repair
    path, so the credential sweep and session revocation must not be gated on
    the principal's active flag actually changing.
    """

    _role(db_session)
    result = staff_provisioning.provision_staff_account(
        db_session,
        _staff_command("drifted.identity@dotmac.io", key="drifted-identity"),
    )
    user = db_session.get(SystemUser, result.user_id)
    credential = (
        db_session.query(UserCredential)
        .filter(UserCredential.system_user_id == result.user_id)
        .one()
    )
    # The pre-owner state: principal already inactive, access left open.
    user.is_active = False
    credential.is_active = True
    session = AuthSession(
        system_user_id=result.user_id,
        status=SessionStatus.active,
        token_hash="drifted-identity-stale-session",
        # Expired on purpose: the seven drifted production principals carry
        # `status=active, revoked_at IS NULL` rows that are all past expiry, and
        # the sweep must revoke them on expiry-independent criteria.
        expires_at=datetime.now(UTC) - timedelta(days=1),
    )
    db_session.add(session)
    db_session.commit()

    outcome = staff_provisioning.set_staff_account_active(
        db_session,
        staff_provisioning.SetStaffAccountActiveCommand(
            context=_context("remediate-drifted-identity"),
            user_id=result.user_id,
            is_active=False,
        ),
    )

    db_session.expire_all()
    assert outcome.changed is True
    assert db_session.get(UserCredential, credential.id).is_active is False
    revoked = db_session.get(AuthSession, session.id)
    assert revoked.status == SessionStatus.revoked
    assert revoked.revoked_at is not None
