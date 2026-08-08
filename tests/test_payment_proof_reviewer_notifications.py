from __future__ import annotations

import uuid
from types import SimpleNamespace

from app.models.admin_alert import AdminAlert, AdminNotification
from app.models.network_monitoring import AlertStatus
from app.models.notification import (
    Notification,
)
from app.models.operational_escalation import (
    OperationalDeliveryStatus,
    OperationalEntityType,
    OperationalEscalationDelivery,
    OperationalEscalationEvent,
    OperationalEscalationStatus,
)
from app.models.rbac import (
    Permission,
    Role,
    RolePermission,
    SystemUserPermission,
    SystemUserRole,
)
from app.models.subscriber import Subscriber
from app.models.system_user import SystemUser
from app.services import (
    operational_escalation,
    payment_proofs,
    staff_notifications,
    web_admin_notifications,
)
from app.services.db_session_adapter import db_session_adapter
from app.services.owner_commands import CommandContext
from app.web.admin.notifications import notification_inbox_open


def _system_user(
    db_session,
    *,
    email: str,
    phone: str | None = None,
) -> SystemUser:
    user = SystemUser(
        first_name="Proof",
        last_name="Reviewer",
        email=email,
        phone=phone,
        is_active=True,
    )
    db_session.add(user)
    db_session.flush()
    return user


def _permission(db_session, key: str) -> Permission:
    permission = db_session.query(Permission).filter(Permission.key == key).first()
    if permission is None:
        permission = Permission(key=key, is_active=True)
        db_session.add(permission)
        db_session.flush()
    return permission


def _grant_role(db_session, user: SystemUser, permission_key: str) -> None:
    permission = _permission(db_session, permission_key)
    role = Role(name=f"proof-review-{uuid.uuid4().hex}", is_active=True)
    db_session.add(role)
    db_session.flush()
    db_session.add_all(
        [
            RolePermission(role_id=role.id, permission_id=permission.id),
            SystemUserRole(system_user_id=user.id, role_id=role.id),
        ]
    )


def _grant_direct(db_session, user: SystemUser, permission_key: str) -> None:
    permission = _permission(db_session, permission_key)
    db_session.add(
        SystemUserPermission(
            system_user_id=user.id,
            permission_id=permission.id,
        )
    )


def _admin_role(db_session, user: SystemUser) -> None:
    role = db_session.query(Role).filter(Role.name == "admin").first()
    if role is None:
        # Recipient resolution treats the canonical role name as the global grant.
        role = Role(name="admin", is_active=True)
        db_session.add(role)
        db_session.flush()
    db_session.add(SystemUserRole(system_user_id=user.id, role_id=role.id))


def _subscriber(db_session) -> Subscriber:
    subscriber = Subscriber(
        first_name="Bank",
        last_name="Transfer",
        email=f"proof-{uuid.uuid4().hex}@example.com",
    )
    db_session.add(subscriber)
    db_session.commit()
    return subscriber


def _submit(db_session, subscriber: Subscriber) -> dict:
    subscriber_id = str(subscriber.id)
    db_session_adapter.release_read_transaction(db_session)
    return payment_proofs.submit_proof(
        db_session,
        subscriber_id,
        context=CommandContext.system(
            actor=f"test:subscriber:{subscriber_id}",
            scope=payment_proofs.SUBMISSION_SCOPE,
            reason="Reviewer-notification submission behavior test",
        ),
        submitted_by=subscriber_id,
        amount="7500.00",
        currency="NGN",
        bank_name="ZENITH BANK",
        reference=f"REVIEW-{uuid.uuid4().hex[:10]}",
        file_path="uploads/payment_proofs/reviewer-notification.png",
    ).to_dict()


def _review_context(action: str) -> CommandContext:
    return CommandContext.system(
        actor="test:payment-proof-reviewer",
        scope=payment_proofs.REVIEW_SCOPE,
        reason=f"Reviewer-notification {action} behavior test",
    )


def _sla_policy(db_session, *, delay_minutes: int = 12) -> None:
    operational_escalation.create_policy(
        db_session,
        name=f"Payment proof review L1 {uuid.uuid4().hex}",
        entity_type=OperationalEntityType.payment_proof,
        trigger="payment_proof.review_requested",
        level=1,
        channels=["email", "whatsapp"],
        unresolved_after_seconds=delay_minutes * 60,
        cooldown_seconds=0,
    )
    db_session.commit()


def test_permission_recipient_resolution_includes_exact_wildcard_and_admin(
    db_session,
) -> None:
    exact = _system_user(db_session, email="exact-reviewer@example.com")
    wildcard = _system_user(db_session, email="wildcard-reviewer@example.com")
    admin = _system_user(db_session, email="admin-reviewer@example.com")
    unrelated = _system_user(db_session, email="unrelated@example.com")
    _grant_role(db_session, exact, "billing:proof:verify")
    _grant_direct(db_session, wildcard, "billing:*")
    _admin_role(db_session, admin)
    _grant_role(db_session, unrelated, "billing:invoice:read")
    db_session.commit()

    users = staff_notifications.system_users_with_permission(
        db_session, "billing:proof:verify"
    )

    assert {user.id for user in users} == {exact.id, wildcard.id, admin.id}


def test_submit_places_confirmation_in_inbox_without_inventing_an_sla(
    db_session,
) -> None:
    reviewer = _system_user(
        db_session,
        email="finance-reviewer@example.com",
        phone="+2348035550101",
    )
    unrelated = _system_user(db_session, email="other-staff@example.com")
    _grant_role(db_session, reviewer, "billing:proof:verify")
    _grant_role(db_session, unrelated, "billing:invoice:read")
    db_session.commit()
    subscriber = _subscriber(db_session)

    proof = _submit(db_session, subscriber)

    alert = db_session.query(AdminAlert).one()
    assert alert.fingerprint == f"payment-proof-review:{proof['id']}"
    assert alert.status == AlertStatus.open
    assert alert.target_url == f"/admin/billing/payment-proofs/{proof['id']}"

    inbox = db_session.query(AdminNotification).one()
    assert inbox.system_user_id == reviewer.id
    assert inbox.read_at is None
    assert inbox.target_url == alert.target_url

    assert db_session.query(OperationalEscalationEvent).count() == 0
    assert db_session.query(OperationalEscalationDelivery).count() == 0
    assert db_session.query(Notification).count() == 0


def test_configured_sla_controls_delay_and_external_channels(db_session) -> None:
    reviewer = _system_user(
        db_session,
        email="sla-reviewer@example.com",
        phone="+2348035550199",
    )
    _grant_role(db_session, reviewer, "billing:proof:verify")
    _sla_policy(db_session, delay_minutes=17)
    proof = _submit(db_session, _subscriber(db_session))

    event = db_session.query(OperationalEscalationEvent).one()
    deliveries = db_session.query(OperationalEscalationDelivery).all()

    assert event.entity_type == OperationalEntityType.payment_proof
    assert event.entity_id == proof["id"]
    assert event.trigger == "payment_proof.review_requested"
    assert event.status == OperationalEscalationStatus.open
    assert {item.channel for item in deliveries} == {"email", "whatsapp"}
    assert {item.recipient_id for item in deliveries} == {str(reviewer.id)}
    assert all(
        item.delivery_status == OperationalDeliveryStatus.pending for item in deliveries
    )
    assert all(item.cooldown_until is not None for item in deliveries)
    assert all(
        16 * 60 <= (item.cooldown_until - item.created_at).total_seconds() <= 18 * 60
        for item in deliveries
    )
    assert (
        f"/admin/billing/payment-proofs/{proof['id']}"
        in (event.metadata_ or {})["body"]
    )


def test_review_resolution_closes_inbox_and_suppresses_configured_escalations(
    db_session,
) -> None:
    reviewer = _system_user(
        db_session,
        email="resolution-reviewer@example.com",
        phone="+2348035550102",
    )
    _grant_role(db_session, reviewer, "billing:proof:verify")
    _sla_policy(db_session, delay_minutes=15)
    db_session.commit()
    subscriber = _subscriber(db_session)
    proof = _submit(db_session, subscriber)

    reviewer_id = str(reviewer.id)
    db_session_adapter.release_read_transaction(db_session)
    payment_proofs.verify_proof(
        db_session,
        proof["id"],
        context=_review_context("verify"),
        verified_by=reviewer_id,
        auto_allocate=False,
    )

    alert = db_session.query(AdminAlert).one()
    inbox = db_session.query(AdminNotification).one()
    event = db_session.query(OperationalEscalationEvent).one()
    deliveries = db_session.query(OperationalEscalationDelivery).all()
    assert alert.status == AlertStatus.resolved
    assert alert.resolved_at is not None
    assert inbox.read_at is not None
    assert deliveries
    assert event.status == OperationalEscalationStatus.canceled
    assert all(
        item.delivery_status == OperationalDeliveryStatus.suppressed
        for item in deliveries
    )
    assert all(
        item.error_message == "review_completed_before_escalation"
        for item in deliveries
    )


def test_rejection_also_closes_the_shared_review_request(db_session) -> None:
    reviewer = _system_user(
        db_session,
        email="rejection-reviewer@example.com",
        phone="+2348035550103",
    )
    _grant_role(db_session, reviewer, "billing:proof:verify")
    db_session.commit()
    proof = _submit(db_session, _subscriber(db_session))

    reviewer_id = str(reviewer.id)
    db_session_adapter.release_read_transaction(db_session)
    payment_proofs.reject_proof(
        db_session,
        proof["id"],
        context=_review_context("reject"),
        verified_by=reviewer_id,
        review_notes="Transfer is not present on the receiving statement",
    )

    alert = db_session.query(AdminAlert).one()
    inbox = db_session.query(AdminNotification).one()
    assert alert.status == AlertStatus.resolved
    assert inbox.read_at is not None


def test_notification_menu_renders_confirmation_in_staff_inbox(
    db_session, monkeypatch
) -> None:
    reviewer = _system_user(db_session, email="menu-reviewer@example.com")
    _grant_role(db_session, reviewer, "billing:proof:verify")
    db_session.commit()
    proof = _submit(db_session, _subscriber(db_session))
    notification = db_session.query(AdminNotification).one()
    monkeypatch.setattr(
        web_admin_notifications.web_admin_service,
        "get_current_user",
        lambda _request: {
            "id": str(reviewer.id),
            "principal_type": "system_user",
            "email": reviewer.email,
            "subscriber_id": "",
        },
    )

    response = web_admin_notifications.notifications_menu(SimpleNamespace(), db_session)
    body = response.body.decode()

    # The unified bell renders one heading for every source, so the former
    # "Staff inbox" section label is now "Notifications" (#2109).
    assert "Notifications" in body
    assert "Bank transfer receipt needs confirmation" in body
    assert f"/admin/notifications/inbox/{notification.id}/open" in body
    assert f"/admin/billing/payment-proofs/{proof['id']}" not in body


def test_inbox_open_is_scoped_to_the_assigned_system_user(db_session) -> None:
    reviewer = _system_user(db_session, email="assigned-reviewer@example.com")
    other = _system_user(db_session, email="different-reviewer@example.com")
    _grant_role(db_session, reviewer, "billing:proof:verify")
    db_session.commit()
    proof = _submit(db_session, _subscriber(db_session))
    notification = db_session.query(AdminNotification).one()

    denied = notification_inbox_open(
        notification.id,
        db_session,
        {"principal_id": str(other.id), "principal_type": "system_user"},
    )
    db_session.refresh(notification)
    assert denied.headers["location"] == "/admin"
    assert notification.read_at is None

    allowed = notification_inbox_open(
        notification.id,
        db_session,
        {"principal_id": str(reviewer.id), "principal_type": "system_user"},
    )
    db_session.refresh(notification)
    assert allowed.headers["location"] == (
        f"/admin/billing/payment-proofs/{proof['id']}"
    )
    assert notification.read_at is not None


def test_submission_survives_when_no_reviewer_is_configured(db_session) -> None:
    proof = _submit(db_session, _subscriber(db_session))

    assert proof["status"] == "submitted"
    assert db_session.query(AdminAlert).count() == 1
    assert db_session.query(AdminNotification).count() == 0
    assert db_session.query(Notification).count() == 0
