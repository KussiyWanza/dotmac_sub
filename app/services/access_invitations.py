"""`auth.access_invitations` — the invitation lifecycle aggregate.

Records issued → accepted / expired / revoked evidence for the staff,
reseller, user, and subscriber invitation capabilities
(docs/designs/IDENTITY_ONBOARDING_CHAIN.md). The capability itself stays a
transport-time signed bearer whose redeem-time TTL checks remain fail
closed in the issuing domain — this aggregate adds the affirmative
lifecycle: reissue supersedes the prior issued invite, acceptance is
stamped by the credential owner's completed reset, and expiry becomes a
durable per-invitation timer whose fired trigger drives the receipted
expiry consumer. An invitation row is evidence, never an access grant.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.access_invitation import (
    AccessInvitation,
    AccessInvitationStatus,
)
from app.services.common import coerce_uuid
from app.services.events import EventType, emit_event
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
    owner_command_active,
)

logger = logging.getLogger(__name__)

OWNER = "auth.access_invitations"

_RECORD_DEFINITION = OwnerCommandDefinition(
    owner=OWNER,
    concern="access invitation lifecycle",
    name="record_invitation_issued",
)
_EXPIRY_DEFINITION = OwnerCommandDefinition(
    owner=OWNER,
    concern="access invitation lifecycle",
    name="consume_invitation_expiry",
)


def email_digest(email: str | None) -> str | None:
    normalized = (email or "").strip().lower()
    if not normalized:
        return None
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _issued_for_principal(
    db: Session, *, principal_type: str, principal_id: UUID, purpose: str | None = None
) -> list[AccessInvitation]:
    query = select(AccessInvitation).where(
        AccessInvitation.principal_type == principal_type,
        AccessInvitation.principal_id == principal_id,
        AccessInvitation.status == AccessInvitationStatus.issued.value,
    )
    if purpose is not None:
        query = query.where(AccessInvitation.purpose == purpose)
    return list(db.execute(query.order_by(AccessInvitation.issued_at)).scalars())


def record_issued(
    db: Session,
    *,
    principal_type: str,
    principal_id: UUID,
    purpose: str,
    email: str | None,
    ttl_minutes: int,
    source: str,
    context: CommandContext | None = None,
) -> AccessInvitation:
    """Record one issued invitation and stage its durable expiry timer.

    Participates in an active owner command, otherwise roots its own.
    Reissuing supersedes the prior issued invitation for the same principal
    and purpose (its timer is replaced by generation).
    """

    def _operation() -> AccessInvitation:
        now = datetime.now(UTC)
        expires_at = now + timedelta(minutes=max(1, int(ttl_minutes)))
        for stale in _issued_for_principal(
            db,
            principal_type=principal_type,
            principal_id=principal_id,
            purpose=purpose,
        ):
            stale.status = AccessInvitationStatus.revoked.value
            stale.revoked_at = now
            stale.revoked_reason = "superseded_by_reissue"
            from app.services.runtime_durable_timers import cancel_timer

            cancel_timer(
                db,
                owner=OWNER,
                entity_kind="access_invitation",
                entity_id=stale.id,
                purpose="invitation_expiry_due",
            )
        invitation = AccessInvitation(
            principal_type=principal_type,
            principal_id=principal_id,
            purpose=purpose,
            status=AccessInvitationStatus.issued.value,
            email_sha256=email_digest(email),
            issued_at=now,
            expires_at=expires_at,
            source=source,
        )
        db.add(invitation)
        db.flush()

        from app.services.runtime_durable_timers import (
            ScheduleTimerCommand,
            schedule_timer,
        )

        schedule_timer(
            db,
            ScheduleTimerCommand(
                owner=OWNER,
                entity_kind="access_invitation",
                entity_id=invitation.id,
                purpose="invitation_expiry_due",
                due_at=expires_at,
                output_event_type="auth.access_invitation_expiry_due",
            ),
            context=_context(
                context,
                scope=str(invitation.id),
                reason="invitation expiry deadline",
                idempotency_key=f"invite-expiry:{invitation.id}",
            ),
        )
        emit_event(
            db,
            EventType.access_invitation_issued,
            {
                "invitation_id": str(invitation.id),
                "principal_type": principal_type,
                "principal_id": str(principal_id),
                "purpose": purpose,
                "expires_at": expires_at.isoformat(),
                "source": source,
            },
            actor=OWNER,
        )
        return invitation

    if owner_command_active(db):
        return _operation()
    root_context = _context(
        context,
        scope=f"{principal_type}:{principal_id}",
        reason=f"record {purpose} issuance",
        idempotency_key=f"invite-issue:{principal_type}:{principal_id}",
    )
    if db.in_transaction():
        # The caller owns an open transaction (delivery-time materialization,
        # legacy admin sends). Root the command on a fresh session over the
        # same bind; under an external connection transaction the boundary
        # guard scopes it to a savepoint.
        from app.services.events.handlers.owner_session import owner_session

        with owner_session(db) as command_db:

            def _rebound() -> AccessInvitation:
                nonlocal db
                original, db = db, command_db
                try:
                    return _operation()
                finally:
                    db = original

            return execute_owner_command(
                command_db,
                definition=_RECORD_DEFINITION,
                context=root_context,
                operation=_rebound,
            )
    return execute_owner_command(
        db,
        definition=_RECORD_DEFINITION,
        context=root_context,
        operation=_operation,
    )


def mark_accepted(
    db: Session,
    *,
    principal_type: str,
    principal_id: UUID,
) -> int:
    """Stamp the principal's issued invitations accepted (flush-only).

    Called by the credential owner inside its completed-reset command. A
    pure password reset with no issued invitation is an exact no-op.
    """
    now = datetime.now(UTC)
    accepted = 0
    for invitation in _issued_for_principal(
        db, principal_type=principal_type, principal_id=principal_id
    ):
        invitation.status = AccessInvitationStatus.accepted.value
        invitation.accepted_at = now
        accepted += 1
        emit_event(
            db,
            EventType.access_invitation_accepted,
            {
                "invitation_id": str(invitation.id),
                "principal_type": principal_type,
                "principal_id": str(principal_id),
                "purpose": invitation.purpose,
            },
            actor=OWNER,
        )
    if accepted:
        db.flush()
    return accepted


def consume_invitation_expiry(
    db: Session,
    *,
    invitation_id,
    event_id,
    context: CommandContext,
) -> str | None:
    """Receipt one fired expiry timer into the expired transition.

    State-guarded: an accepted, revoked, or already-expired invitation
    makes a stale firing an exact no-op. Expiry is evidence only — the
    capability's redeem-time TTL check remains the fail-closed gate.
    """
    from app.services.events.owner_outputs import consume_owner_output

    def _effect() -> str:
        invitation = db.get(AccessInvitation, coerce_uuid(str(invitation_id)))
        if invitation is None:
            return "skipped_missing"
        if invitation.status != AccessInvitationStatus.issued.value:
            return "skipped_state"
        # No real-clock re-check: reissue replaces the timer by generation,
        # so a fired timer is always the invitation's current deadline; the
        # status guard covers acceptance and revocation races.
        invitation.status = AccessInvitationStatus.expired.value
        invitation.expired_at = datetime.now(UTC)
        emit_event(
            db,
            EventType.access_invitation_expired,
            {
                "invitation_id": str(invitation.id),
                "principal_type": invitation.principal_type,
                "principal_id": str(invitation.principal_id),
                "purpose": invitation.purpose,
            },
            actor=OWNER,
        )
        return "expired"

    return execute_owner_command(
        db,
        definition=_EXPIRY_DEFINITION,
        context=context,
        operation=lambda: consume_owner_output(
            db,
            consumer=OWNER,
            event_id=event_id,
            event_type="auth.access_invitation_expiry_due",
            producer_owner="runtime.durable_timers",
            context=context,
            operation=_effect,
        )[0],
    )


def _context(
    context: CommandContext | None,
    *,
    scope: str,
    reason: str,
    idempotency_key: str,
) -> CommandContext:
    if context is not None:
        return context
    return CommandContext.system(
        actor=OWNER,
        scope=scope,
        reason=reason,
        idempotency_key=idempotency_key,
    )
