"""Atomic owner and durable-consequence tests for reseller onboarding."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.config import settings
from app.models.audit import AuditEvent
from app.models.auth import AuthProvider, UserCredential
from app.models.event_store import EventStore
from app.models.notification import CommunicationIntentRecord, Notification
from app.models.rbac import Role, SubscriberRole
from app.models.subscriber import Reseller, ResellerUser, Subscriber, UserType
from app.schemas.subscriber import ResellerCreate
from app.services import (
    auth_flow,
    credential_recovery,
    reseller_onboarding,
    subscriber_assignments,
)
from app.services.ephemeral_communication_actions import (
    EPHEMERAL_ACTION_METADATA_KEY,
    RESELLER_USER_INVITE_ACTION,
    materialize_email,
)
from app.services.owner_commands import CommandContext


def _contexts(
    key: str = "reseller-onboarding-test",
) -> tuple[CommandContext, CommandContext]:
    correlation_id = uuid.uuid4()
    owner = CommandContext(
        command_id=uuid.uuid4(),
        correlation_id=correlation_id,
        actor="user:reseller-onboarding-test",
        scope=reseller_onboarding.RESELLER_WRITE_SCOPE,
        reason="verify reseller onboarding semantics",
        idempotency_key=key,
    )
    assignment = CommandContext(
        command_id=uuid.uuid4(),
        correlation_id=correlation_id,
        actor=owner.actor,
        scope=subscriber_assignments.ASSIGNMENT_SCOPE,
        reason="verify correlated reseller role assignment",
        idempotency_key=f"{key}:assignment",
    )
    return owner, assignment


def _user(
    *,
    email: str = "reseller.owner@example.com",
    role_name: str | None = None,
    send_invite: bool = True,
) -> reseller_onboarding.ResellerPortalUserSpec:
    return reseller_onboarding.ResellerPortalUserSpec(
        first_name="Reseller",
        last_name="Owner",
        email=email,
        username=email,
        password="InitialSecret123!",  # noqa: S106 - fixture-only credential
        role_name=role_name,
        send_invite=send_invite,
    )


@pytest.fixture
def legacy_principal_mode():
    previous = settings.reseller_user_principal_enabled
    object.__setattr__(settings, "reseller_user_principal_enabled", False)
    yield
    object.__setattr__(settings, "reseller_user_principal_enabled", previous)


@pytest.fixture
def first_class_principal_mode():
    previous = settings.reseller_user_principal_enabled
    object.__setattr__(settings, "reseller_user_principal_enabled", True)
    yield
    object.__setattr__(settings, "reseller_user_principal_enabled", previous)


def test_create_commits_reseller_principal_grant_audit_events_and_invite_together(
    db_session,
    legacy_principal_mode,
) -> None:
    role = Role(name="reseller-owner", description="Reseller owner", is_active=True)
    db_session.add(role)
    db_session.flush()
    role_id = role.id
    role_name = role.name
    db_session.commit()
    owner, assignment = _contexts()

    result = reseller_onboarding.create_reseller(
        db_session,
        reseller_onboarding.CreateResellerCommand(
            context=owner,
            reseller=ResellerCreate(name="Atomic Reseller", code="ATOMIC-RSL"),
            portal_user=_user(role_name=role_name),
            assignment_context=assignment,
        ),
    )

    assert not db_session.in_transaction()
    assert result.principal_type == "subscriber"
    assert result.subscriber_id == result.principal_id
    reseller = db_session.get(Reseller, result.reseller_id)
    subscriber = db_session.get(Subscriber, result.subscriber_id)
    link = db_session.get(ResellerUser, result.reseller_user_id)
    credential = (
        db_session.query(UserCredential)
        .filter(UserCredential.subscriber_id == result.subscriber_id)
        .one()
    )
    grant = (
        db_session.query(SubscriberRole)
        .filter(SubscriberRole.subscriber_id == result.subscriber_id)
        .one()
    )
    invite = (
        db_session.query(Notification)
        .filter(Notification.event_type == "auth.reseller_user_invite")
        .one()
    )
    action = invite.metadata_[EPHEMERAL_ACTION_METADATA_KEY]

    assert reseller.name == "Atomic Reseller"
    assert subscriber.user_type == UserType.reseller
    assert subscriber.reseller_id == reseller.id
    assert link.subscriber_id == subscriber.id
    assert credential.provider == AuthProvider.local
    assert credential.must_change_password is True
    assert grant.role_id == role_id
    assert result.role_names == (role_name,)
    assert invite.audience_type == "subscriber"
    assert invite.audience_id == subscriber.id
    assert invite.body is None
    assert action["type"] == RESELLER_USER_INVITE_ACTION
    assert "token" not in str(action).lower()
    assert (
        db_session.query(AuditEvent)
        .filter(AuditEvent.action == "auth.reseller_onboarded")
        .count()
        == 1
    )
    event_types = {
        value
        for (value,) in db_session.query(EventStore.event_type)
        .filter(
            EventStore.event_type.in_(
                (
                    "reseller.created",
                    "reseller_user.provisioned",
                    "subscriber.created",
                )
            )
        )
        .all()
    }
    assert event_types == {
        "reseller.created",
        "reseller_user.provisioned",
        "subscriber.created",
    }


def test_late_audit_failure_rolls_back_every_onboarding_write(
    db_session,
    monkeypatch,
    legacy_principal_mode,
) -> None:
    owner, assignment = _contexts("late-failure")

    def fail_audit(*_args, **_kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(reseller_onboarding, "stage_audit_event", fail_audit)
    with pytest.raises(RuntimeError, match="audit unavailable"):
        reseller_onboarding.create_reseller(
            db_session,
            reseller_onboarding.CreateResellerCommand(
                context=owner,
                reseller=ResellerCreate(name="Rollback Reseller", code="ROLLBACK-RSL"),
                portal_user=_user(
                    email="rollback.reseller@example.com",
                    send_invite=True,
                ),
                assignment_context=assignment,
            ),
        )

    assert not db_session.in_transaction()
    assert db_session.query(Reseller).filter_by(code="ROLLBACK-RSL").count() == 0
    assert (
        db_session.query(Subscriber)
        .filter_by(email="rollback.reseller@example.com")
        .count()
        == 0
    )
    assert (
        db_session.query(UserCredential)
        .filter_by(username="rollback.reseller@example.com")
        .count()
        == 0
    )
    assert (
        db_session.query(EventStore)
        .filter(EventStore.event_type == "reseller_user.provisioned")
        .count()
        == 0
    )


def test_first_class_principal_has_no_subscriber_and_uses_internal_invite(
    db_session,
    first_class_principal_mode,
) -> None:
    owner, assignment = _contexts("first-class")
    result = reseller_onboarding.create_reseller(
        db_session,
        reseller_onboarding.CreateResellerCommand(
            context=owner,
            reseller=ResellerCreate(name="First Class Reseller", code="FIRST-RSL"),
            portal_user=_user(email="first.class.reseller@example.com"),
            assignment_context=assignment,
        ),
    )

    assert result.principal_type == "reseller_user"
    assert result.subscriber_id is None
    principal = db_session.get(ResellerUser, result.principal_id)
    credential = (
        db_session.query(UserCredential)
        .filter(UserCredential.reseller_user_id == result.principal_id)
        .one()
    )
    invite = (
        db_session.query(Notification)
        .filter(Notification.event_type == "auth.reseller_user_invite")
        .one()
    )
    assert principal.subscriber_id is None
    assert credential.subscriber_id is None
    assert invite.audience_type == "reseller_user"
    assert invite.audience_id == principal.id
    assert (
        db_session.query(Subscriber)
        .filter(Subscriber.email == "first.class.reseller@example.com")
        .count()
        == 0
    )


def test_invite_materializes_for_exact_reseller_principal(
    db_session,
    monkeypatch,
    first_class_principal_mode,
) -> None:
    owner, assignment = _contexts("materialize")
    result = reseller_onboarding.create_reseller(
        db_session,
        reseller_onboarding.CreateResellerCommand(
            context=owner,
            reseller=ResellerCreate(name="Invite Reseller", code="INVITE-RSL"),
            portal_user=_user(email="invite.reseller@example.com"),
            assignment_context=assignment,
        ),
    )
    notification = (
        db_session.query(Notification)
        .filter(Notification.event_type == "auth.reseller_user_invite")
        .one()
    )
    requested: list[tuple[str, uuid.UUID]] = []

    def exact_reset(_db, *, principal_type, principal_id, ttl_minutes=None):
        requested.append((principal_type, principal_id))
        return credential_recovery.PasswordResetCapability(
            token="in-memory-reseller-capability",
            email="reseller.owner@example.com",
            person_name="Reseller Owner",
            principal_type=principal_type,
            principal_id=principal_id,
            ttl_minutes=60,
        )

    def render_invite(_db, **kwargs):
        assert kwargs["reset_token"] == "in-memory-reseller-capability"
        assert kwargs["token_in_fragment"] is True
        assert kwargs["next_login_path"].startswith("/reseller/auth/login")
        return SimpleNamespace(
            subject="Reseller invite",
            body_html="<p>Invite</p>",
            body_text="Invite",
        )

    from app.services import email as email_service

    monkeypatch.setattr(
        reseller_onboarding.credential_recovery,
        "issue_exact_reset_capability",
        exact_reset,
    )
    monkeypatch.setattr(email_service, "render_user_invite_email", render_invite)

    content = materialize_email(db_session, notification)

    assert requested == [("reseller_user", result.principal_id)]
    assert content.subject == "Reseller invite"
    assert "in-memory-reseller-capability" not in str(notification.metadata_)


def test_identity_conflict_rolls_back_new_reseller(
    db_session,
    subscriber,
    legacy_principal_mode,
) -> None:
    subscriber.email = "existing.reseller@example.com"
    db_session.commit()
    owner, assignment = _contexts("identity-conflict")

    with pytest.raises(reseller_onboarding.ResellerOnboardingError) as captured:
        reseller_onboarding.create_reseller(
            db_session,
            reseller_onboarding.CreateResellerCommand(
                context=owner,
                reseller=ResellerCreate(name="Conflict Reseller", code="CONFLICT-RSL"),
                portal_user=_user(email=" Existing.Reseller@Example.com "),
                assignment_context=assignment,
            ),
        )

    assert captured.value.code == "auth.reseller_onboarding.identity_conflict"
    assert db_session.query(Reseller).filter_by(code="CONFLICT-RSL").count() == 0


def test_exact_reset_supports_first_class_reseller_user(
    db_session,
    monkeypatch,
    first_class_principal_mode,
) -> None:
    monkeypatch.setenv("JWT_SECRET", "reseller-reset-test-secret")
    owner, assignment = _contexts("exact-reset")
    result = reseller_onboarding.create_reseller(
        db_session,
        reseller_onboarding.CreateResellerCommand(
            context=owner,
            reseller=ResellerCreate(name="Reset Reseller", code="RESET-RSL"),
            portal_user=_user(
                email="reset.reseller@example.com",
                send_invite=False,
            ),
            assignment_context=assignment,
        ),
    )

    reset = auth_flow.request_principal_password_reset(
        db_session,
        principal_type="reseller_user",
        principal_id=result.principal_id,
        ttl_minutes=15,
    )
    assert reset is not None
    assert reset["principal_type"] == "reseller_user"
    auth_flow.reset_password(db_session, reset["token"], "ReplacementSecret123!")

    credential = (
        db_session.query(UserCredential)
        .filter(UserCredential.reseller_user_id == result.principal_id)
        .one()
    )
    assert credential.must_change_password is False
    assert auth_flow.verify_password("ReplacementSecret123!", credential.password_hash)


def test_first_class_role_assignment_fails_closed(
    db_session,
    first_class_principal_mode,
) -> None:
    role = Role(name="unsupported-reseller-role", is_active=True)
    db_session.add(role)
    db_session.flush()
    role_name = role.name
    db_session.commit()
    owner, assignment = _contexts("unsupported-role")

    with pytest.raises(reseller_onboarding.ResellerOnboardingError) as captured:
        reseller_onboarding.create_reseller(
            db_session,
            reseller_onboarding.CreateResellerCommand(
                context=owner,
                reseller=ResellerCreate(name="Role Gate Reseller", code="ROLE-GATE"),
                portal_user=_user(
                    email="role.gate@example.com",
                    role_name=role_name,
                ),
                assignment_context=assignment,
            ),
        )

    assert captured.value.code == "auth.reseller_onboarding.unsupported_role_target"
    assert db_session.query(Reseller).filter_by(code="ROLE-GATE").count() == 0


def test_first_class_invite_policy_disables_subscriber_role_assignment(
    first_class_principal_mode,
) -> None:
    policy = reseller_onboarding.get_reseller_portal_invite_policy()

    assert (
        policy.principal_type
        == reseller_onboarding.ResellerPortalPrincipalType.RESELLER_USER
    )
    assert policy.subscriber_role_assignment_supported is False


def test_legacy_invite_policy_supports_subscriber_role_assignment(
    legacy_principal_mode,
) -> None:
    policy = reseller_onboarding.get_reseller_portal_invite_policy()

    assert (
        policy.principal_type
        == reseller_onboarding.ResellerPortalPrincipalType.SUBSCRIBER
    )
    assert policy.subscriber_role_assignment_supported is True


def test_invite_intent_record_contains_no_capability(
    db_session,
    legacy_principal_mode,
) -> None:
    owner, assignment = _contexts("no-capability")
    reseller_onboarding.create_reseller(
        db_session,
        reseller_onboarding.CreateResellerCommand(
            context=owner,
            reseller=ResellerCreate(name="No Token Reseller", code="NO-TOKEN-RSL"),
            portal_user=_user(email="no.token.reseller@example.com"),
            assignment_context=assignment,
        ),
    )

    intent = (
        db_session.query(CommunicationIntentRecord)
        .filter(CommunicationIntentRecord.event_type == "auth.reseller_user_invite")
        .one()
    )
    assert "token" not in str(intent.metadata_).lower()
    assert intent.body is None
