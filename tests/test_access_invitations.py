"""Invitation lifecycle behavior (docs/designs/IDENTITY_ONBOARDING_CHAIN.md).

Issuance records the aggregate and stages its durable expiry timer;
reissue supersedes; a completed reset stamps acceptance; the fired expiry
timer drives the receipted consumer with state-guarded no-ops. The
capability's redeem-time TTL check remains the fail-closed gate.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.models.access_invitation import AccessInvitation, AccessInvitationStatus
from app.models.durable_timer import DurableTimer, TimerStatus
from app.models.owner_output import OwnerOutputReceipt
from app.services import access_invitations
from app.services.owner_commands import CommandContext
from app.services.runtime_durable_timers import fire_due_timers


def _fire(db, now):
    db.commit()
    fired = fire_due_timers(
        db,
        now=now,
        context=CommandContext.system(
            actor="pytest",
            scope="runtime.durable_timers:dispatch",
            reason="test fire",
            idempotency_key=f"test-fire:{uuid.uuid4()}",
        ),
    )
    db.commit()
    return fired


def test_issue_records_aggregate_and_expiry_timer(db_session):
    principal_id = uuid.uuid4()
    invitation = access_invitations.record_issued(
        db_session,
        principal_type="system_user",
        principal_id=principal_id,
        purpose="staff_invite",
        email="Admin@Example.com",
        ttl_minutes=1,
        source="pytest",
    )
    db_session.commit()

    assert invitation.status == AccessInvitationStatus.issued.value
    assert invitation.email_sha256 == access_invitations.email_digest(
        "admin@example.com"
    )
    timer = db_session.execute(
        select(DurableTimer).where(DurableTimer.purpose == "invitation_expiry_due")
    ).scalar_one()
    assert str(timer.entity_id) == str(invitation.id)

    fired = _fire(db_session, datetime.now(UTC) + timedelta(minutes=2))
    assert len(fired) == 1
    db_session.expire_all()
    row = db_session.get(AccessInvitation, invitation.id)
    assert row.status == AccessInvitationStatus.expired.value
    receipt = db_session.execute(
        select(OwnerOutputReceipt).where(
            OwnerOutputReceipt.consumer == "auth.access_invitations"
        )
    ).scalar_one()
    assert receipt.outcome.value == "succeeded"


def test_reissue_supersedes_and_acceptance_beats_stale_expiry(db_session):
    principal_id = uuid.uuid4()
    first = access_invitations.record_issued(
        db_session,
        principal_type="system_user",
        principal_id=principal_id,
        purpose="user_invite",
        email="a@example.com",
        ttl_minutes=1,
        source="pytest",
    )
    db_session.commit()
    second = access_invitations.record_issued(
        db_session,
        principal_type="system_user",
        principal_id=principal_id,
        purpose="user_invite",
        email="a@example.com",
        ttl_minutes=60,
        source="pytest",
    )
    db_session.commit()

    db_session.expire_all()
    assert (
        db_session.get(AccessInvitation, first.id).status
        == AccessInvitationStatus.revoked.value
    )
    # Reissue replaced the timer by generation: one current timer remains.
    timers = (
        db_session.execute(
            select(DurableTimer).where(
                DurableTimer.purpose == "invitation_expiry_due",
                DurableTimer.status == TimerStatus.scheduled,
            )
        )
        .scalars()
        .all()
    )
    assert len(timers) == 1

    accepted = access_invitations.mark_accepted(
        db_session, principal_type="system_user", principal_id=principal_id
    )
    db_session.commit()
    assert accepted == 1
    db_session.expire_all()
    assert (
        db_session.get(AccessInvitation, second.id).status
        == AccessInvitationStatus.accepted.value
    )

    # A stale expiry firing after acceptance is a state-guarded no-op.
    fired = _fire(db_session, datetime.now(UTC) + timedelta(hours=2))
    assert len(fired) == 1
    db_session.expire_all()
    assert (
        db_session.get(AccessInvitation, second.id).status
        == AccessInvitationStatus.accepted.value
    )


def test_pure_password_reset_is_a_noop_for_invitations(db_session):
    assert (
        access_invitations.mark_accepted(
            db_session,
            principal_type="system_user",
            principal_id=uuid.uuid4(),
        )
        == 0
    )
