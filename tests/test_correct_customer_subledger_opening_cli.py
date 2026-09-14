from __future__ import annotations

from uuid import uuid4

from app.models.rbac import Role, SystemUserRole
from app.models.system_user import SystemUser
from scripts.billing import correct_customer_subledger_opening as cli


def _user(db, *, active: bool = True) -> SystemUser:  # noqa: ANN001
    user = SystemUser(
        id=uuid4(),
        first_name="Opening",
        last_name="Reviewer",
        display_name="Opening Reviewer",
        email=f"opening-reviewer-{uuid4().hex}@example.test",
        is_active=active,
    )
    db.add(user)
    db.flush()
    return user


def test_opening_correction_permission_accepts_active_admin(db_session):
    user = _user(db_session)
    role = Role(name="admin", is_active=True)
    db_session.add(role)
    db_session.flush()
    db_session.add(SystemUserRole(system_user_id=user.id, role_id=role.id))
    db_session.flush()

    assert cli._permission_granted(db_session, user.id) is True


def test_opening_correction_permission_rejects_inactive_admin(db_session):
    user = _user(db_session, active=False)
    role = Role(name="admin", is_active=True)
    db_session.add(role)
    db_session.flush()
    db_session.add(SystemUserRole(system_user_id=user.id, role_id=role.id))
    db_session.flush()

    assert cli._permission_granted(db_session, user.id) is False
