"""Canonical verified inbound receipt and consequence lifecycle."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, TypeVar
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models.integration_platform import (
    IntegrationCapabilityBinding,
    IntegrationInbox,
)
from app.services.integrations.delivery import payload_digest
from app.services.integrations.installations import quarantine_installation

# Matches IntegrationDelivery's outbound lease (app/services/integrations/
# delivery.py) so a claim on either half of this subsystem expires on the
# same cadence.
DEFAULT_LEASE_DURATION = timedelta(minutes=2)


def _as_aware_utc(value: datetime) -> datetime:
    """Normalize a possibly-naive datetime to UTC-aware.

    SQLite (the unit-test lane's engine) does not preserve tzinfo across a
    round trip through a `DateTime(timezone=True)` column, so a value just
    loaded from the ORM can come back naive even though it was always written
    as UTC. Comparing that directly against a fresh `datetime.now(UTC)` raises
    `TypeError: can't compare offset-naive and offset-aware datetimes` — this
    is the same normalization used throughout the codebase (e.g.
    `account_lifecycle.py`, `auth_flow.py`) for the identical reason.
    """

    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class InboxError(ValueError):
    """Raised when an inbound receipt violates identity or lifecycle rules."""


class ProviderEventIdentityCollision(InboxError):
    """A provider reused one event identity for different payload bytes."""


class InboxLeaseLost(InboxError):
    """A claimant tries to complete a receipt reclaimed out from under it.

    The lease clock is a heuristic (it can expire early under a slow-but-alive
    worker, or a clock skew). This is the actual safety mechanism: a claimant
    threads the `attempt_count` it observed at claim time through to
    completion, and completion refuses to apply if the row has moved on to a
    newer attempt (someone else reclaimed it). The caller's transaction is
    still open at that point, so raising here rolls back any consequence the
    stale claimant was about to commit.
    """


CommandResultT = TypeVar("CommandResultT")


def execute_command(
    db: Session,
    command: Callable[[], CommandResultT],
) -> CommandResultT:
    """Complete one inbox-owned unit of work."""

    try:
        result = command()
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise


def get_receipt(db: Session, *, receipt_id: UUID) -> IntegrationInbox:
    # FOR UPDATE: callers use this to read-then-mutate lifecycle state
    # (replay_receipt's lease check, fail_claimed_consequence's completion).
    # A plain `db.get` would let a concurrent claimant's read-then-write race
    # this one under READ COMMITTED — see the identical reasoning on
    # `receive_verified`'s own-row lookup below.
    receipt = db.scalars(
        select(IntegrationInbox)
        .where(IntegrationInbox.id == receipt_id)
        .with_for_update()
    ).one_or_none()
    if receipt is None:
        raise InboxError("integration inbox receipt not found")
    return receipt


def list_receipts(
    db: Session,
    *,
    state: str | None = None,
    capability_binding_id: UUID | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[IntegrationInbox]:
    query = select(IntegrationInbox)
    if state:
        normalized_state = state.strip().lower()
        if normalized_state not in {
            "verified",
            "processing",
            "processed",
            "retryable",
            "dead_letter",
        }:
            raise InboxError("invalid integration inbox state")
        query = query.where(IntegrationInbox.state == normalized_state)
    if capability_binding_id:
        query = query.where(
            IntegrationInbox.capability_binding_id == capability_binding_id
        )
    return list(
        db.scalars(
            query.order_by(IntegrationInbox.received_at.desc())
            .offset(offset)
            .limit(limit)
        ).all()
    )


def list_recent_receipts_for_capability(
    db: Session, *, capability_binding_id: UUID, limit: int = 20
) -> list[IntegrationInbox]:
    return list_receipts(
        db,
        capability_binding_id=capability_binding_id,
        limit=limit,
    )


def receive_verified(
    db: Session,
    *,
    capability_binding_id: UUID,
    provider_event_id: str,
    event_type: str,
    payload: dict[str, Any],
    headers: dict[str, str] | None = None,
    legacy_provider_event_id: str | None = None,
) -> tuple[IntegrationInbox, bool]:
    # The binding is the stable parent aggregate for receipt identity. Locking
    # it serializes the check/insert sequence so the database uniqueness rule
    # remains an arbiter instead of becoming a webhook-facing exception.
    binding = db.scalars(
        select(IntegrationCapabilityBinding)
        .where(IntegrationCapabilityBinding.id == capability_binding_id)
        .with_for_update()
    ).one_or_none()
    if binding is None:
        raise InboxError("capability binding not found")
    normalized_event_id = provider_event_id.strip()
    if not normalized_event_id:
        raise InboxError("provider event id is required")
    # `legacy_provider_event_id` is a narrow, auditable escape hatch (see
    # below) for exactly ONE retired identity string per event -- deliberately
    # NOT a collection a caller could grow into a parallel identity system.
    # Reject a malformed value here, at the single call site, rather than
    # letting it silently no-op (empty) or silently collide with the row it
    # is meant to be an alias FOR (identical to the current identity, which
    # would make the fallback check below a no-op that always resolves off
    # the primary lookup above, hiding a caller bug).
    normalized_legacy_id: str | None = None
    if legacy_provider_event_id is not None:
        normalized_legacy_id = legacy_provider_event_id.strip()
        if not normalized_legacy_id:
            raise InboxError("legacy provider event id, if supplied, must be non-empty")
        if len(normalized_legacy_id) > 240:
            raise InboxError(
                "legacy provider event id exceeds the provider_event_id column"
            )
        if normalized_legacy_id == normalized_event_id:
            raise InboxError(
                "legacy provider event id must differ from the current "
                "provider event id"
            )
    digest = payload_digest(payload)
    # FOR UPDATE: the binding lock above only serializes callers that are
    # BOTH still inside this function. A claimant that already committed its
    # claim and moved on to processing the receipt (a separate, later
    # transaction, holding a row lock on the IntegrationInbox row via
    # `get_receipt`/`lock_for_update`, but no binding lock) is not blocked by
    # the binding lock at all. Locking this row too makes a concurrent
    # redelivery's reclaim attempt block on that claimant's own row lock
    # instead of racing it with a non-locking read — the same protection the
    # sweeper already gets from `with_for_update(skip_locked=True)`.
    existing = (
        db.query(IntegrationInbox)
        .filter(
            IntegrationInbox.capability_binding_id == binding.id,
            IntegrationInbox.provider_event_id == normalized_event_id,
        )
        .with_for_update()
        .one_or_none()
    )
    if existing is not None:
        if existing.payload_digest != digest:
            quarantine_installation(
                db,
                installation_id=binding.installation_id,
                reason="provider_event_identity_collision",
                actor="integration.inbox",
            )
            raise ProviderEventIdentityCollision("provider event identity collision")
        return existing, False
    # No row under the current identity format. `legacy_provider_event_id` is
    # a caller-declared, generic escape hatch for a provider that changed its
    # identity CONSTRUCTION (not its underlying events) while it already had
    # live receipts under the old format -- the caller is asserting that this
    # ONE old key is inherently ambiguous (it may have been shared across
    # genuinely distinct events), so a digest MISMATCH there is expected and
    # harmless, never evidence of tampering. Only an EXACT digest match is
    # treated as "this is the same event, already recorded" (a redelivery
    # under the retired format); anything else falls through and is recorded
    # fresh under the new, non-ambiguous identity below. Deliberately bounded
    # to exactly one legacy identity per call (see the validation above) --
    # this is a narrow, auditable migration aid for one known retired format,
    # not an open-ended alternate identity namespace.
    if normalized_legacy_id is not None:
        legacy_existing = (
            db.query(IntegrationInbox)
            .filter(
                IntegrationInbox.capability_binding_id == binding.id,
                IntegrationInbox.provider_event_id == normalized_legacy_id,
            )
            .with_for_update()
            .one_or_none()
        )
        if legacy_existing is not None and legacy_existing.payload_digest == digest:
            return legacy_existing, False
    receipt = IntegrationInbox(
        installation_id=binding.installation_id,
        capability_binding_id=binding.id,
        provider_event_id=normalized_event_id,
        event_type=event_type.strip() or "unknown",
        payload_digest=digest,
        headers_json={
            str(key).lower(): str(value) for key, value in (headers or {}).items()
        },
        payload_json=payload,
        state="verified",
        attempt_count=0,
        consequence_json={},
    )
    db.add(receipt)
    db.flush()
    return receipt, True


def receive_and_claim_verified(
    db: Session,
    *,
    capability_binding_id: UUID,
    provider_event_id: str,
    event_type: str,
    payload: dict[str, Any],
    headers: dict[str, str] | None = None,
    now: datetime | None = None,
    lease_duration: timedelta = DEFAULT_LEASE_DURATION,
    legacy_provider_event_id: str | None = None,
) -> tuple[IntegrationInbox, bool]:
    """Persist a verified fact before any domain consequence runs."""

    try:
        receipt, _created = receive_verified(
            db,
            capability_binding_id=capability_binding_id,
            provider_event_id=provider_event_id,
            event_type=event_type,
            payload=payload,
            headers=headers,
            legacy_provider_event_id=legacy_provider_event_id,
        )
        should_process = claim_for_processing(
            receipt, now=now, lease_duration=lease_duration
        )
    except ProviderEventIdentityCollision:
        # Quarantine is the authoritative security consequence of an identity
        # collision and must survive the fail-closed rejection.
        db.commit()
        raise
    except Exception:
        db.rollback()
        raise
    db.commit()
    return receipt, should_process


def claim_for_processing(
    receipt: IntegrationInbox,
    *,
    now: datetime | None = None,
    lease_duration: timedelta = DEFAULT_LEASE_DURATION,
) -> bool:
    """Claim `receipt` for processing, reclaiming an expired lease if needed.

    Callers reach this only after acquiring the capability-binding lock AND
    a `FOR UPDATE` lock on this exact row in `receive_verified`. The binding
    lock alone is not enough: a claimant that already committed its claim and
    moved on to processing the receipt holds no binding lock while it works,
    only a row lock (taken separately via `get_receipt`/`lock_for_update` in
    `process_claimed_payment_webhook`) — the row-level lock here is what
    makes a concurrent reclaim block on that in-flight claimant instead of
    reading a stale snapshot and clobbering its eventual commit. Even so, a
    lease can still expire out from under a claimant that is genuinely still
    working (a live worker between transactions, not merely slow inside one);
    the `attempt_count` fence in `mark_processed`/`mark_failed` is the backstop
    for that case.
    """

    now = now or datetime.now(UTC)
    if receipt.state in {"processed", "dead_letter"}:
        # Provider redelivery is not an authorized replay. A terminal receipt
        # remains terminal and the webhook acknowledges the already-recorded
        # fact without attempting a second consequence.
        return False
    if receipt.state == "processing":
        live_lease = (
            receipt.lease_expires_at is not None
            and _as_aware_utc(receipt.lease_expires_at) > now
        )
        if live_lease:
            # A genuinely live claim. Refuse — this is not a reclaim.
            return False
        # Lease is missing or expired: the original claimant is presumed dead.
        # Fall through to the shared claim/reclaim mutation below.
    elif (
        receipt.state == "retryable"
        and receipt.error_code == "crm_customer_name_rejected"
    ):
        return False
    receipt.state = "processing"
    receipt.attempt_count += 1
    receipt.lease_expires_at = now + lease_duration
    receipt.error_code = None
    receipt.error_detail = None
    return True


def mark_processed(
    receipt: IntegrationInbox,
    *,
    consequence: dict[str, Any],
    claimed_attempt: int | None = None,
) -> IntegrationInbox:
    if claimed_attempt is not None and receipt.attempt_count != claimed_attempt:
        raise InboxLeaseLost(
            "integration inbox receipt was reclaimed before completion"
        )
    receipt.state = "processed"
    receipt.consequence_json = consequence
    receipt.processed_at = datetime.now(UTC)
    receipt.lease_expires_at = None
    receipt.error_code = None
    receipt.error_detail = None
    return receipt


def mark_failed(
    receipt: IntegrationInbox,
    *,
    error_code: str,
    error_detail: str | None = None,
    max_attempts: int = 10,
    claimed_attempt: int | None = None,
) -> IntegrationInbox:
    if claimed_attempt is not None and receipt.attempt_count != claimed_attempt:
        raise InboxLeaseLost(
            "integration inbox receipt was reclaimed before completion"
        )
    receipt.error_code = error_code[:120]
    receipt.error_detail = (error_detail or "")[:2000] or None
    receipt.lease_expires_at = None
    receipt.state = (
        "dead_letter" if receipt.attempt_count >= max(1, max_attempts) else "retryable"
    )
    return receipt


def complete_consequence(
    db: Session,
    *,
    receipt: IntegrationInbox,
    consequence: dict[str, Any],
    claimed_attempt: int | None = None,
) -> dict[str, Any]:
    """Commit one domain consequence with its canonical inbox evidence."""

    return execute_command(
        db,
        lambda: (
            mark_processed(
                receipt, consequence=consequence, claimed_attempt=claimed_attempt
            ).consequence_json
        ),
    )


def fail_consequence(
    db: Session,
    *,
    receipt: IntegrationInbox,
    error_code: str,
    error_detail: str | None = None,
    consequence: dict[str, Any] | None = None,
    max_attempts: int = 10,
    claimed_attempt: int | None = None,
) -> None:
    """Discard partial consequence writes, then record retry evidence."""

    receipt_id = receipt.id
    db.rollback()

    def operation() -> None:
        current = get_receipt(db, receipt_id=receipt_id)
        mark_failed(
            current,
            error_code=error_code,
            error_detail=error_detail,
            max_attempts=max_attempts,
            claimed_attempt=claimed_attempt,
        )
        if consequence is not None:
            current.consequence_json = consequence

    execute_command(db, operation)


def fail_claimed_consequence(
    db: Session,
    *,
    receipt_id: UUID,
    error_code: str,
    error_detail: str | None = None,
    max_attempts: int = 10,
    claimed_attempt: int | None = None,
) -> None:
    """Record failure after a separate consequence owner already rolled back."""

    def operation() -> None:
        receipt = get_receipt(db, receipt_id=receipt_id)
        mark_failed(
            receipt,
            error_code=error_code,
            error_detail=error_detail,
            max_attempts=max_attempts,
            claimed_attempt=claimed_attempt,
        )

    execute_command(db, operation)


def replay_receipt(
    db: Session, *, receipt_id: UUID, now: datetime | None = None
) -> IntegrationInbox:
    """Manually return a stuck receipt to `verified` for reprocessing.

    A live `processing` claim always refuses — this escape hatch must never
    race a genuinely running worker. A `processing` claim whose lease has
    already expired is treated the same as `retryable`/`dead_letter`: nothing
    is currently working it.
    """

    now = now or datetime.now(UTC)
    receipt = get_receipt(db, receipt_id=receipt_id)
    is_expired_processing = receipt.state == "processing" and (
        receipt.lease_expires_at is None
        or _as_aware_utc(receipt.lease_expires_at) <= now
    )
    if receipt.state not in {"retryable", "dead_letter"} and not is_expired_processing:
        raise InboxError("integration inbox receipt is not replayable")
    receipt.state = "verified"
    receipt.lease_expires_at = None
    receipt.error_code = None
    receipt.error_detail = None
    db.flush()
    return receipt


def reclaim_stale_claims(
    db: Session, *, now: datetime | None = None, grace: timedelta = timedelta(minutes=1)
) -> int:
    """Move every `processing` receipt whose lease expired more than `grace`
    ago to `retryable` for a fresh delivery attempt or manual replay.

    Deliberately lands on `retryable`, not `verified`: silently requeuing a
    receipt that a live provider will redeliver anyway risks looping forever
    on a poison payload. `retryable` still surfaces it, but only a real
    redelivery (inline reclaim) or an operator replay puts it back to work.
    """

    now = now or datetime.now(UTC)
    cutoff = now - grace
    stale = (
        db.query(IntegrationInbox)
        .filter(IntegrationInbox.state == "processing")
        .filter(
            or_(
                IntegrationInbox.lease_expires_at.is_(None),
                IntegrationInbox.lease_expires_at < cutoff,
            )
        )
        .with_for_update(skip_locked=True)
        .all()
    )
    for receipt in stale:
        receipt.state = "retryable"
        receipt.lease_expires_at = None
        receipt.error_code = "inbox_claim_lease_expired"
        receipt.error_detail = (
            "Processing claim lease expired without a completion; moved to "
            "retryable for redelivery or manual replay."
        )
    db.flush()
    return len(stale)
