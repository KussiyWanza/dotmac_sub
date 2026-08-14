"""Revoking what a reduced entitlement would otherwise keep authorizing.

## The gap this closes

Deactivating a role was never the exposure. `auth.rbac_catalog` refuses to
deactivate an assigned role, and every authorization read already filters
inactive roles out, so `Role.is_active` cannot be the trigger for denial — by
the time it can change, nothing is assigned.

The real transition is **losing an entitlement you still hold a token for**: a
role unassigned from a principal, or a permission taken off a role that
principal holds. The issued JWT carries the old roles and scopes in its claims
and stays cryptographically valid until it expires. No cache invalidation can
reach inside a signed token, so "flush the claims cache" is not revocation — it
only stops *new* reads of stale rows.

## Why revoking the session is the whole mechanism

`app/services/auth_dependencies.py::require_user_auth` re-reads the
authoritative `sessions` row on **every** request and rejects the token unless
that row is still active, unrevoked and unexpired. Marking the session revoked
is therefore what makes the stale claims fail — immediately, on the next
request, with no dependency on Redis being reachable and no wait for token or
session refresh.

That ordering is what makes the cache work below an optimization rather than
the mechanism: denial is a committed database fact before any cache is touched.

## The contract for a reducing owner

An owner that reduces entitlements calls `revoke_for_entitlement_reduction`
inside its own transaction and does nothing else. The helper:

1. revokes the principal's live sessions in the caller's transaction, so the
   revocation commits or rolls back **with** the reduction and can never be
   half-applied;
2. emits durable projection work before commit, so what was revoked survives
   the process;
3. registers strict cache invalidation to run **after** commit — never before,
   or a concurrent request would repopulate the cache from rows the reduction
   had not yet committed;
4. keeps a cache failure observable without letting it preserve authorization.

On (4): the deferred-callback runner logs and swallows, so a failure there
would otherwise vanish. This module counts it on
`ENTITLEMENT_REVOCATION_CACHE_FAILURES` and logs a named event carrying the
principal. Authorization is already correct at that point — the sessions are
revoked in the database and the request path re-reads them — so a failed
invalidation costs a stale *read* of role names, never a stale grant.

## What this is not

It does not decide *whether* an entitlement was reduced. That judgement belongs
to the owner performing the reduction, which alone knows what the principal
could do before and after. Calling this on a widening or a no-op change would
log operators out for being granted something, which is not security.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.auth import Session as AuthSession
from app.models.auth import SessionStatus
from app.services import auth_cache
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.session_hooks import run_after_commit

logger = logging.getLogger(__name__)

PRINCIPAL_SYSTEM_USER = "system_user"
PRINCIPAL_SUBSCRIBER = "subscriber"
PRINCIPAL_RESELLER_USER = "reseller_user"

_PRINCIPAL_COLUMNS = {
    PRINCIPAL_SYSTEM_USER: AuthSession.system_user_id,
    PRINCIPAL_SUBSCRIBER: AuthSession.subscriber_id,
    PRINCIPAL_RESELLER_USER: AuthSession.reseller_user_id,
}


class UnknownPrincipalTypeError(ValueError):
    """A principal kind with no session column would revoke nothing.

    Raised rather than returning an empty result: silently revoking nothing for
    a principal the caller believed it had cut off is the exact failure this
    module exists to prevent.
    """


def _live_sessions(
    db: Session,
    *,
    principal_type: str,
    principal_id: UUID,
    now: datetime,
) -> tuple[AuthSession, ...]:
    column = _PRINCIPAL_COLUMNS.get(principal_type)
    if column is None:
        raise UnknownPrincipalTypeError(
            f"no session column for principal type {principal_type!r}"
        )
    return tuple(
        db.execute(
            select(AuthSession)
            .where(column == principal_id)
            .where(AuthSession.status == SessionStatus.active)
            .where(AuthSession.revoked_at.is_(None))
            .where(AuthSession.expires_at > now)
            .with_for_update()
        ).scalars()
    )


def revoke_for_entitlement_reduction(
    db: Session,
    *,
    principal_type: str,
    principal_id: UUID,
    reason: str,
    correlation_id: str,
    actor: str | None = None,
) -> tuple[str, ...]:
    """Revoke a principal's live sessions as part of the caller's transaction.

    Returns the revoked session ids in a stable order so the caller can record
    them in its own audit metadata.

    Call only on an actual reduction. Deliberately does not commit: the caller
    owns the transaction, which is the only way the revocation and the
    reduction can be one atomic fact.
    """

    now = datetime.now(UTC)
    sessions = _live_sessions(
        db,
        principal_type=principal_type,
        principal_id=principal_id,
        now=now,
    )
    if not sessions:
        return ()

    revoked_ids: list[str] = []
    for session in sessions:
        session.status = SessionStatus.revoked
        session.revoked_at = now
        revoked_ids.append(str(session.id))
    revoked_ids.sort()
    db.flush()

    principal_key = str(principal_id)
    emit_event(
        db,
        EventType.rbac_entitlement_reduction_revoked,
        {
            "schema_version": 1,
            "aggregate_type": principal_type,
            "aggregate_id": principal_key,
            "principal_type": principal_type,
            "principal_id": principal_key,
            "reason": reason,
            "correlation_id": correlation_id,
            "revoked_session_ids": revoked_ids,
            "revoked_at": now.isoformat(),
        },
        actor=actor,
    )

    def invalidate(_callback_db: Session) -> None:
        try:
            auth_cache.invalidate_principal_strict(principal_type, principal_key)
        except Exception:
            from app.metrics import ENTITLEMENT_REVOCATION_CACHE_FAILURES

            ENTITLEMENT_REVOCATION_CACHE_FAILURES.inc()
            logger.exception(
                "entitlement_revocation_cache_invalidation_failed",
                extra={
                    "principal_type": principal_type,
                    "principal_id": principal_key,
                    "correlation_id": correlation_id,
                    "revoked_session_count": len(revoked_ids),
                },
            )

    run_after_commit(db, invalidate)
    return tuple(revoked_ids)
