"""Contracted password recovery owner and ephemeral-delivery tests."""

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
from app.services import credential_recovery
from app.services.auth_flow import hash_password, verify_password
from app.services.domain_errors import DomainError
from app.services.ephemeral_communication_actions import (
    EPHEMERAL_ACTION_METADATA_KEY,
    PASSWORD_RECOVERY_ACTION,
    materialize_email,
)
from app.services.owner_commands import CommandContext


def _context(reason: str = "verify credential recovery") -> CommandContext:
    command_id = uuid.uuid4()
    return CommandContext(
        command_id=command_id,
        correlation_id=command_id,
        actor="service:credential-recovery-test",
        scope=credential_recovery.CREDENTIAL_RECOVERY_SCOPE,
        reason=reason,
        idempotency_key=f"credential-recovery:{command_id}",
    )


def _credential(db_session, subscriber, *, email: str) -> UserCredential:
    subscriber.email = email
    credential = UserCredential(
        subscriber_id=subscriber.id,
        provider=AuthProvider.local,
        username=email,
        password_hash=hash_password("old-password"),
        is_active=True,
    )
    db_session.add(credential)
    db_session.commit()
    return credential


def test_request_persists_only_durable_non_secret_delivery_context(
    db_session, subscriber, monkeypatch
) -> None:
    email = "durable.recovery@example.com"
    _credential(db_session, subscriber, email=email)

    def reject_early_signing(*_args, **_kwargs):
        raise AssertionError("request and event expansion must not mint a bearer")

    monkeypatch.setattr(
        credential_recovery,
        "sign_context_token",
        reject_early_signing,
    )
    outcome = credential_recovery.request_password_recovery(
        db_session,
        credential_recovery.RequestPasswordRecoveryCommand(
            context=_context(),
            email=email,
            next_login_path="/portal/auth/login?next=/portal/dashboard",
        ),
    )

    assert outcome.accepted is True
    assert outcome.delivery_requested is True
    intent = db_session.query(CommunicationIntentRecord).one()
    notification = db_session.query(Notification).one()
    assert intent.event_type == "auth.password_recovery"
    assert intent.body is None
    assert notification.body is None
    assert notification.audience_type == "subscriber"
    assert notification.audience_id == subscriber.id
    envelope = notification.metadata_[EPHEMERAL_ACTION_METADATA_KEY]
    assert envelope["type"] == PASSWORD_RECOVERY_ACTION
    durable_payload = f"{intent.metadata_!r}{notification.metadata_!r}"
    assert email not in durable_payload
    assert "token" not in durable_payload.lower()
    assert len(envelope["context"]["email_sha256"]) == 64


def test_delivery_materializes_bearer_in_fragment_without_persisting_it(
    db_session, subscriber, monkeypatch
) -> None:
    email = "materialized.recovery@example.com"
    _credential(db_session, subscriber, email=email)
    credential_recovery.request_password_recovery(
        db_session,
        credential_recovery.RequestPasswordRecoveryCommand(
            context=_context(),
            email=email,
        ),
    )
    notification = db_session.query(Notification).one()
    rendered: list[dict[str, object]] = []

    def render(_db, **kwargs):
        rendered.append(kwargs)
        return SimpleNamespace(
            subject="Password reset request",
            body_html="<p>Reset</p>",
            body_text="Reset",
        )

    from app.services import email as email_service

    monkeypatch.setattr(email_service, "render_password_reset_email", render)
    content = materialize_email(db_session, notification)

    token = str(rendered[0]["reset_token"])
    assert token
    assert rendered[0]["token_in_fragment"] is True
    assert content.subject == "Password reset request"
    assert notification.body is None
    assert token not in str(notification.metadata_)


def test_completion_changes_credential_revokes_sessions_and_spends_token(
    db_session, subscriber, monkeypatch
) -> None:
    monkeypatch.setenv("JWT_SECRET", "credential-recovery-test-secret")
    email = "complete.recovery@example.com"
    credential = _credential(db_session, subscriber, email=email)
    session = AuthSession(
        subscriber_id=subscriber.id,
        status=SessionStatus.active,
        token_hash=hashlib.sha256(b"recovery-session").hexdigest(),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    db_session.add(session)
    db_session.commit()
    capability = credential_recovery.issue_exact_reset_capability(
        db_session,
        principal_type="subscriber",
        principal_id=subscriber.id,
    )
    assert capability is not None
    db_session.commit()
    projection_calls: list[tuple[str, str]] = []
    from app.services import customer_portal_session, reseller_portal
    from app.services.events.handlers import credential_session_projection

    monkeypatch.setattr(
        credential_session_projection.auth_cache,
        "invalidate_principal_strict",
        lambda principal_type, principal_id: projection_calls.append(
            ("auth", f"{principal_type}:{principal_id}")
        ),
    )
    monkeypatch.setattr(
        customer_portal_session,
        "revoke_customer_sessions_for_subscriber",
        lambda principal_id, db, require_durable: projection_calls.append(
            ("customer", principal_id)
        ),
    )
    monkeypatch.setattr(
        reseller_portal,
        "revoke_reseller_sessions_for_subscriber",
        lambda principal_id, db, require_durable: projection_calls.append(
            ("reseller", principal_id)
        ),
    )

    outcome = credential_recovery.complete_password_reset(
        db_session,
        credential_recovery.CompletePasswordResetCommand(
            context=_context("redeem recovery capability"),
            token=capability.token,
            new_password="Replacement-password1",
        ),
    )

    with pytest.raises(DomainError) as captured:
        credential_recovery.complete_password_reset(
            db_session,
            credential_recovery.CompletePasswordResetCommand(
                context=_context("reject capability replay"),
                token=capability.token,
                new_password="Another-replacement-password1",
            ),
        )
    assert captured.value.code == ("auth.credential_recovery.invalid_reset_capability")

    db_session.refresh(credential)
    db_session.refresh(session)
    assert verify_password("Replacement-password1", credential.password_hash)
    assert credential.must_change_password is False
    assert session.status == SessionStatus.revoked
    assert outcome.sessions_revoked == 1
    assert projection_calls == [
        ("auth", f"subscriber:{subscriber.id}"),
        ("customer", str(subscriber.id)),
        ("reseller", str(subscriber.id)),
    ]
    audit = (
        db_session.query(AuditEvent)
        .filter(AuditEvent.action == "auth.password_reset_completed")
        .one()
    )
    assert audit.metadata_["email_sha256"] == hashlib.sha256(email.encode()).hexdigest()
    assert email not in str(audit.metadata_)
    assert (
        db_session.query(EventStore)
        .filter(EventStore.event_type == "password_recovery.completed")
        .count()
        == 1
    )


def test_unknown_request_is_enumeration_safe_and_writes_no_delivery(
    db_session,
) -> None:
    outcome = credential_recovery.request_password_recovery(
        db_session,
        credential_recovery.RequestPasswordRecoveryCommand(
            context=_context(),
            email="missing.recovery@example.com",
        ),
    )

    assert outcome.accepted is True
    assert outcome.delivery_requested is False
    assert db_session.query(CommunicationIntentRecord).count() == 0
    assert db_session.query(EventStore).count() == 0


def test_invalid_password_rolls_back_without_reset_evidence(
    db_session, subscriber, monkeypatch
) -> None:
    monkeypatch.setenv("JWT_SECRET", "credential-recovery-test-secret")
    _credential(db_session, subscriber, email="invalid.password@example.com")
    capability = credential_recovery.issue_exact_reset_capability(
        db_session,
        principal_type="subscriber",
        principal_id=subscriber.id,
    )
    assert capability is not None
    db_session.commit()

    with pytest.raises(DomainError) as captured:
        credential_recovery.complete_password_reset(
            db_session,
            credential_recovery.CompletePasswordResetCommand(
                context=_context(),
                token=capability.token,
                new_password="short",
            ),
        )

    assert captured.value.code == "auth.credential_recovery.invalid_password"
    assert (
        db_session.query(AuditEvent)
        .filter(AuditEvent.action == "auth.password_reset_completed")
        .count()
        == 0
    )
    assert (
        db_session.query(EventStore)
        .filter(EventStore.event_type == "password_recovery.completed")
        .count()
        == 0
    )


def test_durable_session_projection_fails_for_event_retry_without_redis(
    monkeypatch,
) -> None:
    from app.services import session_store

    monkeypatch.setattr(session_store, "get_session_redis", lambda: None)
    with pytest.raises(RuntimeError, match="Durable session revocation store"):
        session_store.set_session_revocation_epoch(
            "credential-recovery-test",
            str(uuid.uuid4()),
            300,
            {},
            require_durable=True,
        )


def _system_user_credential(db_session, *, email: str):
    from app.models.system_user import SystemUser

    system_user = SystemUser(
        first_name="Admin", last_name="User", email=email, is_active=True
    )
    db_session.add(system_user)
    db_session.flush()
    credential = UserCredential(
        system_user_id=system_user.id,
        provider=AuthProvider.local,
        username=email,
        password_hash=hash_password("old-password"),
        is_active=True,
    )
    db_session.add(credential)
    db_session.commit()
    return system_user, credential


def test_password_min_length_for_applies_system_user_floor(db_session) -> None:
    from app.services.auth_flow import password_min_length_for

    assert password_min_length_for(db_session, "system_user") == 8
    assert password_min_length_for(db_session, "subscriber") == password_min_length_for(
        db_session, None
    )


def test_password_policy_requires_all_user_facing_requirements() -> None:
    from app.services.auth_flow import password_policy_violations

    assert password_policy_violations("abcdefgh", 8) == (
        "Password must include at least one uppercase letter.",
        "Password must include at least one number.",
        "Password must include at least one special character.",
    )
    assert password_policy_violations("Abcdef1!", 8) == ()


def test_system_user_reset_rejects_password_below_shared_minimum(
    db_session, monkeypatch
) -> None:
    monkeypatch.setenv("JWT_SECRET", "credential-recovery-test-secret")
    system_user, _ = _system_user_credential(
        db_session, email="admin.floor@example.com"
    )
    capability = credential_recovery.issue_exact_reset_capability(
        db_session, principal_type="system_user", principal_id=system_user.id
    )
    assert capability is not None
    db_session.commit()

    # Seven characters: below the shared minimum of eight.
    with pytest.raises(DomainError) as captured:
        credential_recovery.complete_password_reset(
            db_session,
            credential_recovery.CompletePasswordResetCommand(
                context=_context(), token=capability.token, new_password="Abcdefg!"
            ),
        )
    assert captured.value.code == "auth.credential_recovery.invalid_password"


def test_system_user_reset_accepts_password_at_shared_minimum(
    db_session, monkeypatch
) -> None:
    monkeypatch.setenv("JWT_SECRET", "credential-recovery-test-secret")
    system_user, credential = _system_user_credential(
        db_session, email="admin.ok@example.com"
    )
    capability = credential_recovery.issue_exact_reset_capability(
        db_session, principal_type="system_user", principal_id=system_user.id
    )
    db_session.commit()

    outcome = credential_recovery.complete_password_reset(
        db_session,
        credential_recovery.CompletePasswordResetCommand(
            context=_context(), token=capability.token, new_password="Abcdef1!"
        ),
    )
    assert outcome.principal_type == "system_user"
    db_session.refresh(credential)
    assert verify_password("Abcdef1!", credential.password_hash)
