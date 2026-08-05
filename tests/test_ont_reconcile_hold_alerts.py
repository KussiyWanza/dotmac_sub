"""Durable operational delivery for overdue ONT reconcile holds."""

from datetime import UTC, datetime, timedelta

from app.models.admin_alert import AdminAlert, AdminNotification
from app.models.network import (
    OntReconcileHold,
    OntReconcileHoldStatus,
    OntReconcileScope,
    OntUnit,
)
from app.models.network_monitoring import AlertSeverity, AlertStatus
from app.models.rbac import Role, SystemUserRole
from app.models.system_user import SystemUser
from app.tasks.ont_reconcile import _sync_overdue_reconcile_hold_alerts


def _admin(db) -> SystemUser:
    user = SystemUser(
        first_name="Network",
        last_name="Admin",
        email="ont-hold-alert@example.com",
    )
    role = Role(name="admin", is_active=True)
    db.add_all([user, role])
    db.flush()
    db.add(SystemUserRole(system_user_id=user.id, role_id=role.id))
    return user


def _overdue_hold(db) -> tuple[OntUnit, OntReconcileHold]:
    ont = OntUnit(serial_number="HWTC-HOLD-ALERT", is_active=True)
    db.add(ont)
    db.flush()
    hold = OntReconcileHold(
        ont_unit_id=ont.id,
        scope=OntReconcileScope.automatic_sweep,
        status=OntReconcileHoldStatus.active,
        reason_code="wan_intent_adjudication",
        explanation="WAN intent requires review.",
        actor="operator@dotmac",
        reviewer="reviewer@dotmac",
        idempotency_key="hold-alert-test",
        review_due_at=datetime.now(UTC) - timedelta(hours=2),
    )
    db.add(hold)
    db.commit()
    return ont, hold


def test_overdue_hold_alert_is_durable_idempotent_and_resolves(db_session):
    _admin(db_session)
    ont, hold = _overdue_hold(db_session)

    first = _sync_overdue_reconcile_hold_alerts(db_session)

    alert = db_session.query(AdminAlert).one()
    assert first.as_payload() == {
        "overdue": 1,
        "opened": 1,
        "escalated": 0,
        "updated": 0,
        "resolved": 0,
    }
    assert alert.severity is AlertSeverity.critical
    assert alert.status is AlertStatus.open
    assert alert.target_url == f"/admin/network/onts/{ont.id}"
    assert "actor" not in (alert.details or {})
    assert "reviewer" not in (alert.details or {})
    assert db_session.query(AdminNotification).count() == 1

    second = _sync_overdue_reconcile_hold_alerts(db_session)

    assert second.updated == 1
    assert db_session.query(AdminAlert).count() == 1
    assert db_session.query(AdminNotification).count() == 1

    hold.status = OntReconcileHoldStatus.released
    hold.released_at = datetime.now(UTC)
    hold.released_by = "operator@dotmac"
    hold.release_reason = "Review complete."
    db_session.commit()

    third = _sync_overdue_reconcile_hold_alerts(db_session)

    db_session.refresh(alert)
    assert third.overdue == 0
    assert third.resolved == 1
    assert alert.status is AlertStatus.resolved
