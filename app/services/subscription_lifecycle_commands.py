"""Canonical execution boundary for one subscription lifecycle command.

This service owns orchestration, locking, idempotent replay, and structured
outcomes. Account lifecycle, catalog, billing, scheduling, and RADIUS services
remain the owners of their respective mutations and side effects.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import NAMESPACE_URL, uuid5

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType
from app.models.catalog import Subscription, SubscriptionStatus
from app.models.enforcement_lock import EnforcementReason
from app.models.idempotency import IdempotencyKey
from app.services.audit_adapter import record_audit_event
from app.services.common import coerce_uuid
from app.services.owner_commands import CommandContext
from app.services.subscription_lifecycle import (
    ServiceChangeDeliveryMode,
    SubscriptionCommandKind,
    SubscriptionCommandOutcome,
    SubscriptionCommandOutcomeStatus,
    SubscriptionEffectiveTiming,
    SubscriptionLifecycleCommand,
    SubscriptionLifecycleError,
    SubscriptionLifecycleHeadConflict,
    SubscriptionLifecyclePreview,
    preview_subscription_command,
    resolve_subscription_lifecycle,
)

logger = logging.getLogger(__name__)

_IDEMPOTENCY_SCOPE = "subscription_lifecycle"


class SubscriptionCommandExecutionRejected(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class SubscriptionCommandBatchResult:
    """Structured aggregate for a set of independently committed commands."""

    changed_ids: tuple[str, ...]
    skipped_ids: tuple[str, ...]
    failed_ids: tuple[str, ...]
    outcomes: tuple[SubscriptionCommandOutcome, ...]

    @property
    def changed(self) -> int:
        return len(self.changed_ids)

    def as_dict(self) -> dict[str, object]:
        return {
            "changed": self.changed,
            "changed_ids": list(self.changed_ids),
            "skipped_ids": list(self.skipped_ids),
            "failed_ids": list(self.failed_ids),
            "outcomes": [
                {
                    "subscription_id": outcome.command.subscription_id,
                    "kind": outcome.command.kind.value,
                    "status": outcome.status.value,
                    "message": outcome.message,
                    "artifact_ids": list(outcome.artifact_ids),
                    "error_code": outcome.error_code,
                    "replayed": outcome.replayed,
                }
                for outcome in self.outcomes
            ],
        }


def execute_subscription_command(
    db: Session,
    command: SubscriptionLifecycleCommand,
    *,
    actor_id: str | None = None,
    actor_type: AuditActorType = AuditActorType.system,
    now: datetime | None = None,
) -> SubscriptionCommandOutcome:
    """Validate and execute one reviewed lifecycle command.

    The subscription row lock serializes review-head validation, idempotency
    reservation, and dispatch for a subscription. Replays are resolved before
    validating ``expected_head`` because a successfully applied first request
    necessarily changes that head.
    """
    effective_now = _aware_utc(now) or datetime.now(UTC)
    subscription = db.scalar(
        select(Subscription)
        .where(Subscription.id == coerce_uuid(command.subscription_id))
        .with_for_update()
    )
    if subscription is None:
        raise SubscriptionLifecycleError(
            f"Subscription {command.subscription_id} not found"
        )

    current = resolve_subscription_lifecycle(db, command.subscription_id)
    contract_rejection = _command_contract_rejection(command)
    if contract_rejection is not None:
        outcome = SubscriptionCommandOutcome(
            command=command,
            status=SubscriptionCommandOutcomeStatus.rejected,
            message=contract_rejection[1],
            previous_head=current.head,
            current_head=current.head,
            error_code=contract_rejection[0],
        )
        _record_outcome(db, outcome, actor_id=actor_id, actor_type=actor_type)
        return outcome

    prior = _find_idempotency_reservation(db, command)
    if prior is not None:
        outcome = _replay_outcome(db, command, current_head=current.head, prior=prior)
        if not outcome.replayed:
            _record_outcome(db, outcome, actor_id=actor_id, actor_type=actor_type)
        return outcome

    try:
        preview = preview_subscription_command(db, command, now=effective_now)
    except SubscriptionLifecycleHeadConflict as exc:
        outcome = SubscriptionCommandOutcome(
            command=command,
            status=SubscriptionCommandOutcomeStatus.superseded,
            message=str(exc),
            previous_head=current.head,
            current_head=current.head,
            error_code="subscription_head_changed",
        )
        _record_outcome(db, outcome, actor_id=actor_id, actor_type=actor_type)
        return outcome

    rejection = _execution_rejection(command, preview, now=effective_now)
    if rejection is not None:
        outcome = SubscriptionCommandOutcome(
            command=command,
            status=SubscriptionCommandOutcomeStatus.rejected,
            message=rejection[1],
            previous_head=current.head,
            current_head=current.head,
            error_code=rejection[0],
        )
        _record_outcome(db, outcome, actor_id=actor_id, actor_type=actor_type)
        return outcome

    _reserve_idempotency(db, subscription, command)
    try:
        status, artifact_ids, message = _dispatch_command(
            db,
            subscription,
            command,
            preview,
            actor_id=actor_id,
            actor_type=actor_type,
        )
        # Account lifecycle commands intentionally flush so callers can compose
        # them. Catalog and scheduler owners currently commit internally; this is
        # harmless in those paths and completes the transaction in the former.
        db.commit()
    except SubscriptionCommandExecutionRejected as exc:
        db.rollback()
        outcome = SubscriptionCommandOutcome(
            command=command,
            status=SubscriptionCommandOutcomeStatus.rejected,
            message=str(exc),
            previous_head=current.head,
            current_head=current.head,
            error_code=exc.code,
        )
        _record_outcome(db, outcome, actor_id=actor_id, actor_type=actor_type)
        return outcome
    except Exception as exc:
        db.rollback()
        logger.exception(
            "Subscription lifecycle command failed: subscription=%s kind=%s",
            command.subscription_id,
            command.kind.value,
        )
        outcome = SubscriptionCommandOutcome(
            command=command,
            status=SubscriptionCommandOutcomeStatus.failed,
            message=_exception_message(exc),
            previous_head=current.head,
            current_head=current.head,
            error_code="command_execution_failed",
        )
        _record_outcome(db, outcome, actor_id=actor_id, actor_type=actor_type)
        return outcome

    updated = resolve_subscription_lifecycle(db, command.subscription_id)
    outcome = SubscriptionCommandOutcome(
        command=command,
        status=status,
        message=message,
        previous_head=current.head,
        current_head=updated.head,
        artifact_ids=artifact_ids,
    )
    _record_outcome(db, outcome, actor_id=actor_id, actor_type=actor_type)
    return outcome


def confirm_subscription_service_change(
    db: Session,
    command: SubscriptionLifecycleCommand,
    *,
    actor_id: str | None = None,
    actor_type: AuditActorType = AuditActorType.system,
    now: datetime | None = None,
) -> SubscriptionCommandOutcome:
    """Apply or queue one confirmed service change by its delivery mode.

    Commercial-only changes use the immediate subscription command. Remote
    reprovisioning and field migrations persist the reviewed intent without
    changing the subscription first; their provisioning owners must later add
    verification evidence and request the final subscription command. Neither
    branch creates a support ticket. A work order belongs only to the field
    fulfillment branch.
    """
    if command.kind != SubscriptionCommandKind.change_plan:
        raise SubscriptionLifecycleError(
            "confirm_subscription_service_change requires a change_plan command"
        )

    effective_now = _aware_utc(now) or datetime.now(UTC)
    preview = preview_subscription_command(db, command, now=effective_now)
    delivery_mode = preview.delivery_mode
    if delivery_mode is None:
        raise SubscriptionLifecycleError(
            "Service-change delivery mode could not be resolved"
        )
    if delivery_mode == ServiceChangeDeliveryMode.commercial_only:
        return execute_subscription_command(
            db,
            command,
            actor_id=actor_id,
            actor_type=actor_type,
            now=effective_now,
        )

    rejection = _command_contract_rejection(command) or _execution_rejection(
        command, preview, now=effective_now
    )
    if rejection is not None:
        outcome = SubscriptionCommandOutcome(
            command=command,
            status=SubscriptionCommandOutcomeStatus.rejected,
            message=rejection[1],
            previous_head=preview.current.head,
            current_head=preview.current.head,
            error_code=rejection[0],
        )
        _record_outcome(db, outcome, actor_id=actor_id, actor_type=actor_type)
        return outcome

    details = preview.billing_impact.details or {}
    quote = details.get("quote")
    fingerprint = (
        str(quote.get("preview_fingerprint") or "").strip()
        if isinstance(quote, dict)
        else ""
    )
    if not fingerprint or not isinstance(quote, dict):
        raise SubscriptionLifecycleError(
            "The service-change financial preview is missing"
        )
    if command.expected_financial_fingerprint != fingerprint:
        outcome = SubscriptionCommandOutcome(
            command=command,
            status=SubscriptionCommandOutcomeStatus.superseded,
            message="Financial state changed after preview; preview again",
            previous_head=preview.current.head,
            current_head=preview.current.head,
            error_code="plan_change_financial_preview_stale",
        )
        _record_outcome(db, outcome, actor_id=actor_id, actor_type=actor_type)
        return outcome
    field_quote = preview.field_delivery_quote
    if field_quote is not None:
        if command.expected_field_quote_fingerprint != field_quote.fingerprint:
            outcome = SubscriptionCommandOutcome(
                command=command,
                status=SubscriptionCommandOutcomeStatus.superseded,
                message="Serviceability or relocation price changed; preview again",
                previous_head=preview.current.head,
                current_head=preview.current.head,
                error_code="field_delivery_preview_stale",
            )
            _record_outcome(db, outcome, actor_id=actor_id, actor_type=actor_type)
            return outcome
        if not field_quote.eligible:
            outcome = SubscriptionCommandOutcome(
                command=command,
                status=SubscriptionCommandOutcomeStatus.rejected,
                message="Target service address is not eligible for relocation",
                previous_head=preview.current.head,
                current_head=preview.current.head,
                error_code=(
                    field_quote.blocking_reason or "field_delivery_not_eligible"
                ),
            )
            _record_outcome(db, outcome, actor_id=actor_id, actor_type=actor_type)
            return outcome
    idempotency_key = str(command.idempotency_key or "").strip()
    if not idempotency_key:
        raise SubscriptionLifecycleError("Service-change idempotency key is required")

    from app.models.subscription_change import SubscriptionChangeRequest
    from app.services.subscription_changes import subscription_change_requests

    prior = db.scalar(
        select(SubscriptionChangeRequest).where(
            SubscriptionChangeRequest.confirmation_idempotency_key == idempotency_key
        )
    )
    request = subscription_change_requests.create(
        db,
        subscription_id=command.subscription_id,
        new_offer_id=str(command.target_offer_id),
        effective_date=preview.effective_at.date(),
        requested_by_person_id=actor_id,
        notes=command.reason,
        confirmation_preview_fingerprint=fingerprint,
        confirmation_idempotency_key=idempotency_key,
        confirmation_origin=command.source,
        confirmation_snapshot={
            **json.loads(json.dumps(quote, default=str)),
            "delivery_mode": delivery_mode.value,
            "delivery_state": (
                "awaiting_payment"
                if field_quote is not None and field_quote.fee_amount > Decimal("0.00")
                else "awaiting_verification"
            ),
            "field_delivery_quote": (
                json.loads(json.dumps(field_quote.as_dict(), default=str))
                if field_quote is not None
                else None
            ),
        },
        commit=False,
    )
    if field_quote is not None and prior is None:
        from app.schemas.qualification import ServiceQualificationRequest
        from app.services.qualification import (
            preview_service_qualification,
            record_service_qualification,
        )

        target_address_id = coerce_uuid(field_quote.target_service_address_id)
        qualification = record_service_qualification(
            db,
            preview_service_qualification(
                db,
                ServiceQualificationRequest(
                    address_id=target_address_id,
                    requested_tech=field_quote.access_type,
                    metadata_={
                        "purpose": "subscription_relocation_confirmation",
                        "subscription_change_request_id": str(request.id),
                    },
                ),
            ),
        )
        request.target_service_address_id = target_address_id
        request.service_qualification_id = qualification.id
        request.field_fee_offer_id = coerce_uuid(field_quote.fee_offer_id)
        request.field_fee_amount = field_quote.fee_amount
        request.field_fee_currency = field_quote.currency
        request.field_quote_fingerprint = field_quote.fingerprint
        from app.services.subscription_change_execution import stage_relocation_charge

        stage_relocation_charge(db, request)
        db.flush()
    elif delivery_mode == ServiceChangeDeliveryMode.remote_reprovision:
        from app.services.subscription_change_execution import (
            prepare_remote_reprovision,
        )

        if prior is None:
            prepare_remote_reprovision(db, request)
    else:
        from app.models.subscription_change import SubscriptionChangeExecutionState

        request.execution_state = SubscriptionChangeExecutionState.payment_settled
    db.commit()
    updated = resolve_subscription_lifecycle(db, command.subscription_id)
    message = (
        "Remote service change queued for provisioning verification"
        if delivery_mode == ServiceChangeDeliveryMode.remote_reprovision
        else (
            "Service relocation is awaiting its one-time field charge"
            if field_quote is not None and field_quote.fee_amount > Decimal("0.00")
            else "Service migration queued for field fulfillment"
        )
    )
    outcome = SubscriptionCommandOutcome(
        command=command,
        status=SubscriptionCommandOutcomeStatus.scheduled,
        message=message,
        previous_head=preview.current.head,
        current_head=updated.head,
        artifact_ids=(str(request.id),),
        replayed=prior is not None,
    )
    _record_outcome(db, outcome, actor_id=actor_id, actor_type=actor_type)
    return outcome


def execute_subscription_command_batch(
    db: Session,
    subscription_ids: Iterable[str],
    *,
    command_kind_by_status: Mapping[SubscriptionStatus, SubscriptionCommandKind],
    source: str,
    idempotency_key: str,
    actor_id: str | None = None,
    actor_type: AuditActorType = AuditActorType.system,
    reason: str | None = None,
    target_offer_id: str | None = None,
    effective_timing: SubscriptionEffectiveTiming = (
        SubscriptionEffectiveTiming.immediate
    ),
    effective_at: datetime | None = None,
) -> SubscriptionCommandBatchResult:
    """Execute a batch through the single-subscription command owner.

    The status-to-command map is the caller's explicit eligibility policy. Each
    item gets a fresh reviewed head and a deterministic child idempotency key;
    one rejected item does not roll back commands already committed for others.
    """
    operation_key = idempotency_key.strip()
    if not operation_key:
        raise ValueError("idempotency_key is required for a lifecycle batch")

    normalized_ids = tuple(
        dict.fromkeys(
            subscription_id
            for raw_id in subscription_ids
            if (subscription_id := str(raw_id).strip())
        )
    )
    changed_ids: list[str] = []
    skipped_ids: list[str] = []
    failed_ids: list[str] = []
    outcomes: list[SubscriptionCommandOutcome] = []

    for subscription_id in normalized_ids:
        try:
            snapshot = resolve_subscription_lifecycle(db, subscription_id)
            status = SubscriptionStatus(snapshot.state.status)
            kind = command_kind_by_status.get(status)
            if kind is None:
                skipped_ids.append(subscription_id)
                continue
            command = SubscriptionLifecycleCommand(
                subscription_id=subscription_id,
                kind=kind,
                source=source,
                effective_timing=effective_timing,
                effective_at=effective_at,
                target_offer_id=target_offer_id,
                reason=reason,
                expected_head=snapshot.head,
                idempotency_key=f"{operation_key}:{subscription_id}",
            )
            outcome = execute_subscription_command(
                db,
                command,
                actor_id=actor_id,
                actor_type=actor_type,
            )
        except Exception:
            logger.exception(
                "Subscription lifecycle batch item failed before an outcome: "
                "subscription=%s source=%s",
                subscription_id,
                source,
            )
            failed_ids.append(subscription_id)
            continue

        outcomes.append(outcome)
        if outcome.status in {
            SubscriptionCommandOutcomeStatus.applied,
            SubscriptionCommandOutcomeStatus.scheduled,
        }:
            changed_ids.append(subscription_id)
        elif outcome.status == SubscriptionCommandOutcomeStatus.failed:
            failed_ids.append(subscription_id)
        else:
            skipped_ids.append(subscription_id)

    return SubscriptionCommandBatchResult(
        changed_ids=tuple(changed_ids),
        skipped_ids=tuple(skipped_ids),
        failed_ids=tuple(failed_ids),
        outcomes=tuple(outcomes),
    )


def _command_contract_rejection(
    command: SubscriptionLifecycleCommand,
) -> tuple[str, str] | None:
    if command.expected_head is None:
        return (
            "expected_head_required",
            "A reviewed subscription head is required before execution",
        )
    if command.idempotency_key is None:
        return (
            "idempotency_key_required",
            "An idempotency key is required before execution",
        )
    if (
        command.kind
        in {
            SubscriptionCommandKind.vacation_hold,
            SubscriptionCommandKind.vacation_resume,
        }
        and command.effective_timing != SubscriptionEffectiveTiming.immediate
    ):
        return (
            "vacation_command_must_be_immediate",
            "Vacation hold commands must execute immediately; auto-resume uses the "
            "customer-hold expiry task",
        )
    return None


def _execution_rejection(
    command: SubscriptionLifecycleCommand,
    preview: SubscriptionLifecyclePreview,
    *,
    now: datetime,
) -> tuple[str, str] | None:
    if not preview.eligible:
        reason = preview.eligibility_reasons[0]
        return reason, "Command is not eligible: " + ", ".join(
            preview.eligibility_reasons
        )
    if command.kind == SubscriptionCommandKind.renew:
        return (
            "renewal_execution_is_billing_owned",
            "Renewals must be executed by the billing cycle or payment settlement owner",
        )
    explicit_effective_at = _aware_utc(command.effective_at)
    if (
        command.effective_timing == SubscriptionEffectiveTiming.immediate
        and explicit_effective_at is not None
        and explicit_effective_at > now
    ):
        return (
            "future_effective_at_requires_scheduling",
            "A future effective time cannot be executed as an immediate command",
        )
    return None


def _dispatch_command(
    db: Session,
    subscription: Subscription,
    command: SubscriptionLifecycleCommand,
    preview: SubscriptionLifecyclePreview,
    *,
    actor_id: str | None,
    actor_type: AuditActorType,
) -> tuple[SubscriptionCommandOutcomeStatus, tuple[str, ...], str]:
    if command.kind == SubscriptionCommandKind.change_plan:
        return _dispatch_plan_change(
            db,
            subscription,
            command,
            preview,
            actor_id=actor_id,
        )
    if command.effective_timing != SubscriptionEffectiveTiming.immediate:
        from app.services.subscription_lifecycle_schedules import (
            schedule_subscription_status_command,
        )

        schedule = schedule_subscription_status_command(
            db,
            command,
            preview,
            actor_id=actor_id,
            actor_type=actor_type,
        )
        return (
            SubscriptionCommandOutcomeStatus.scheduled,
            (str(schedule.id),),
            f"Subscription {command.kind.value} command scheduled for "
            f"{schedule.effective_at.isoformat()}",
        )

    from app.services.account_lifecycle import (
        suspend_subscription,
        transition_subscription_status,
    )

    reason = command.reason or f"{command.kind.value} lifecycle command"
    evidence_context = _lifecycle_evidence_context(
        command,
        actor_id=actor_id,
        reason=reason,
    )
    if command.kind == SubscriptionCommandKind.suspend:
        lock = suspend_subscription(
            db,
            command.subscription_id,
            reason=EnforcementReason.admin,
            source=command.source,
            notes=reason,
            evidence_context=evidence_context,
            evidence_effective_at=preview.effective_at,
        )
        return (
            SubscriptionCommandOutcomeStatus.applied,
            (str(lock.id),),
            "Subscription suspension command applied",
        )

    if command.kind == SubscriptionCommandKind.vacation_hold:
        lock = suspend_subscription(
            db,
            command.subscription_id,
            reason=EnforcementReason.customer_hold,
            source=command.source,
            notes=reason,
            evidence_context=evidence_context,
            evidence_effective_at=preview.effective_at,
        )
        assert command.vacation_hold_days is not None
        lock.resume_at = preview.effective_at + timedelta(
            days=command.vacation_hold_days
        )
        db.flush()
        return (
            SubscriptionCommandOutcomeStatus.applied,
            (str(lock.id),),
            "Vacation hold applied",
        )

    if command.kind == SubscriptionCommandKind.vacation_resume:
        from app.models.enforcement_lock import EnforcementLock
        from app.services.account_lifecycle import restore_subscription

        vacation_lock = db.scalar(
            select(EnforcementLock).where(
                EnforcementLock.subscription_id == subscription.id,
                EnforcementLock.reason == EnforcementReason.customer_hold,
                EnforcementLock.is_active.is_(True),
            )
        )
        if vacation_lock is None:
            raise SubscriptionCommandExecutionRejected(
                "active_customer_hold_missing",
                "No active customer vacation hold exists",
            )
        trigger = "admin" if command.source.startswith("admin:") else "customer"
        restored = restore_subscription(
            db,
            command.subscription_id,
            trigger=trigger,
            resolved_by=command.source,
            reason=EnforcementReason.customer_hold,
            notes=reason,
            evidence_context=evidence_context,
            evidence_effective_at=preview.effective_at,
        )
        return (
            SubscriptionCommandOutcomeStatus.applied,
            (str(vacation_lock.id),),
            (
                "Vacation hold cleared and service restored"
                if restored
                else "Vacation hold cleared; another access restriction remains"
            ),
        )
    target_status = {
        SubscriptionCommandKind.activate: SubscriptionStatus.active,
        SubscriptionCommandKind.restore: SubscriptionStatus.active,
        SubscriptionCommandKind.disable: SubscriptionStatus.disabled,
        SubscriptionCommandKind.cancel: SubscriptionStatus.canceled,
        SubscriptionCommandKind.expire: SubscriptionStatus.expired,
    }.get(command.kind)
    if target_status is None:
        raise SubscriptionCommandExecutionRejected(
            "unsupported_subscription_command",
            f"Unsupported subscription command {command.kind.value}",
        )
    changed = transition_subscription_status(
        db,
        command.subscription_id,
        target_status,
        reason=reason,
        source=command.source,
        evidence_context=evidence_context,
        evidence_effective_at=preview.effective_at,
    )
    if not changed:
        raise SubscriptionCommandExecutionRejected(
            f"{command.kind.value}_not_applied",
            f"Subscription {command.kind.value} command was not applied",
        )
    return (
        SubscriptionCommandOutcomeStatus.applied,
        (),
        f"Subscription {command.kind.value} command applied",
    )


def _lifecycle_evidence_context(
    command: SubscriptionLifecycleCommand,
    *,
    actor_id: str | None,
    reason: str,
) -> CommandContext:
    """Derive stable evidence identity from the command replay contract."""

    if command.idempotency_key is None:
        raise SubscriptionCommandExecutionRejected(
            "idempotency_key_required",
            "Lifecycle evidence requires the command idempotency key",
        )
    command_id = uuid5(
        NAMESPACE_URL,
        "dotmac:subscription-lifecycle:"
        f"{command.subscription_id}:{command.idempotency_key}",
    )
    return CommandContext.system(
        command_id=command_id,
        correlation_id=command_id,
        actor=actor_id or command.source,
        scope=f"subscription:{command.subscription_id}",
        reason=reason,
        idempotency_key=command.idempotency_key,
    )


def _dispatch_plan_change(
    db: Session,
    subscription: Subscription,
    command: SubscriptionLifecycleCommand,
    preview: SubscriptionLifecyclePreview,
    *,
    actor_id: str | None,
) -> tuple[SubscriptionCommandOutcomeStatus, tuple[str, ...], str]:
    target_offer_id = str(command.target_offer_id)
    if command.effective_timing == SubscriptionEffectiveTiming.immediate:
        from app.services.subscription_changes import subscription_change_requests

        billing_impact = preview.billing_impact
        details = billing_impact.details if billing_impact is not None else None
        quote = details.get("quote") if isinstance(details, dict) else None
        fingerprint = (
            str(quote.get("preview_fingerprint") or "").strip()
            if isinstance(quote, dict)
            else ""
        )
        if not fingerprint:
            raise SubscriptionCommandExecutionRejected(
                "plan_change_preview_missing",
                "The financial plan-change preview is missing its fingerprint",
            )
        if not isinstance(quote, dict):
            raise SubscriptionCommandExecutionRejected(
                "plan_change_preview_missing",
                "The financial plan-change preview is missing its quote",
            )
        if (
            command.expected_financial_fingerprint is not None
            and command.expected_financial_fingerprint != fingerprint
        ):
            raise SubscriptionCommandExecutionRejected(
                "plan_change_financial_preview_stale",
                "Financial state changed after preview; preview again",
            )
        request = subscription_change_requests.confirm_immediate(
            db,
            subscription_id=str(subscription.id),
            new_offer_id=target_offer_id,
            preview_fingerprint=fingerprint,
            preview_effective_at=datetime.fromisoformat(
                str(quote["preview_effective_at"])
            ),
            idempotency_key=(
                command.idempotency_key or subscription_command_fingerprint(command)
            ),
            confirmation_origin=command.source,
            confirmation_snapshot=json.loads(json.dumps(quote, default=str)),
            requested_by_person_id=actor_id,
            actor_id=actor_id,
            notes=command.reason or "Confirmed by subscription lifecycle command",
        )
        return (
            SubscriptionCommandOutcomeStatus.applied,
            (str(request.id),),
            "Subscription plan changed",
        )

    from app.services.subscription_changes import subscription_change_requests

    request = subscription_change_requests.schedule(
        db,
        subscription_id=str(subscription.id),
        new_offer_id=target_offer_id,
        effective_date=preview.effective_at.date(),
        requested_by_person_id=None,
        notes=command.reason or f"Scheduled by {command.source}",
    )
    return (
        SubscriptionCommandOutcomeStatus.scheduled,
        (str(request.id),),
        f"Subscription plan change scheduled for {request.effective_date.isoformat()}",
    )


def _find_idempotency_reservation(
    db: Session,
    command: SubscriptionLifecycleCommand,
) -> IdempotencyKey | None:
    key = subscription_command_idempotency_key(command)
    if key is None:
        return None
    return db.scalar(
        select(IdempotencyKey).where(
            IdempotencyKey.scope == _IDEMPOTENCY_SCOPE,
            IdempotencyKey.key == key,
        )
    )


def _reserve_idempotency(
    db: Session,
    subscription: Subscription,
    command: SubscriptionLifecycleCommand,
) -> None:
    key = subscription_command_idempotency_key(command)
    if key is None:
        return
    db.add(
        IdempotencyKey(
            scope=_IDEMPOTENCY_SCOPE,
            key=key,
            account_id=subscription.subscriber_id,
            ref_id=subscription_command_fingerprint(command),
        )
    )
    db.flush()


def _replay_outcome(
    db: Session,
    command: SubscriptionLifecycleCommand,
    *,
    current_head: str,
    prior: IdempotencyKey,
) -> SubscriptionCommandOutcome:
    fingerprint = subscription_command_fingerprint(command)
    if prior.ref_id != fingerprint:
        return SubscriptionCommandOutcome(
            command=command,
            status=SubscriptionCommandOutcomeStatus.rejected,
            message="Idempotency key was already used for a different command",
            previous_head=current_head,
            current_head=current_head,
            error_code="idempotency_key_conflict",
        )
    snapshot = resolve_subscription_lifecycle(db, command.subscription_id)
    artifact_ids: tuple[str, ...] = ()
    if command.kind == SubscriptionCommandKind.change_plan:
        if command.effective_timing == SubscriptionEffectiveTiming.immediate:
            from app.models.subscription_change import SubscriptionChangeRequest

            request = db.scalar(
                select(SubscriptionChangeRequest).where(
                    SubscriptionChangeRequest.confirmation_idempotency_key
                    == command.idempotency_key
                )
            )
            if request is not None:
                artifact_ids = (str(request.id),)
        elif (
            snapshot.pending_change is not None
            and snapshot.pending_change.target_offer_id == str(command.target_offer_id)
        ):
            artifact_ids = (snapshot.pending_change.request_id,)
    elif command.kind in {
        SubscriptionCommandKind.vacation_hold,
        SubscriptionCommandKind.vacation_resume,
    }:
        from app.models.enforcement_lock import EnforcementLock

        lock: EnforcementLock | None = None
        if command.kind == SubscriptionCommandKind.vacation_resume:
            raw_lock_id = str(command.idempotency_key or "").rsplit(":", 1)[-1]
            try:
                lock = db.get(EnforcementLock, coerce_uuid(raw_lock_id))
            except (TypeError, ValueError):
                lock = None
        else:
            lock = db.scalar(
                select(EnforcementLock)
                .where(
                    EnforcementLock.subscription_id
                    == coerce_uuid(command.subscription_id),
                    EnforcementLock.reason == EnforcementReason.customer_hold,
                    EnforcementLock.source == command.source,
                )
                .order_by(EnforcementLock.created_at.desc())
            )
        if (
            lock is not None
            and lock.subscription_id == coerce_uuid(command.subscription_id)
            and lock.reason == EnforcementReason.customer_hold
        ):
            artifact_ids = (str(lock.id),)
    elif command.effective_timing != SubscriptionEffectiveTiming.immediate:
        from app.models.subscription_lifecycle_schedule import (
            SubscriptionLifecycleSchedule,
        )

        schedule = db.scalar(
            select(SubscriptionLifecycleSchedule).where(
                SubscriptionLifecycleSchedule.subscription_id
                == coerce_uuid(command.subscription_id),
                SubscriptionLifecycleSchedule.idempotency_key
                == subscription_command_idempotency_key(command),
            )
        )
        if schedule is not None:
            artifact_ids = (str(schedule.id),)
    return SubscriptionCommandOutcome(
        command=command,
        status=SubscriptionCommandOutcomeStatus.skipped,
        message="Idempotent replay; command was not executed again",
        previous_head=command.expected_head or current_head,
        current_head=current_head,
        artifact_ids=artifact_ids,
        error_code="idempotent_replay",
        replayed=True,
    )


def subscription_command_idempotency_key(
    command: SubscriptionLifecycleCommand,
) -> str | None:
    if command.idempotency_key is None:
        return None
    source = f"{command.subscription_id}|{command.idempotency_key}"
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def subscription_command_fingerprint(command: SubscriptionLifecycleCommand) -> str:
    effective_at = _aware_utc(command.effective_at)
    payload = {
        "subscription_id": command.subscription_id,
        "kind": command.kind.value,
        "source": command.source,
        "effective_timing": command.effective_timing.value,
        "effective_at": effective_at.isoformat() if effective_at is not None else None,
        "target_offer_id": command.target_offer_id,
        "target_service_address_id": command.target_service_address_id,
        "reason": command.reason,
        "expected_head": command.expected_head,
        "expected_financial_fingerprint": command.expected_financial_fingerprint,
        "expected_field_quote_fingerprint": command.expected_field_quote_fingerprint,
        "vacation_hold_days": command.vacation_hold_days,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _record_outcome(
    db: Session,
    outcome: SubscriptionCommandOutcome,
    *,
    actor_id: str | None,
    actor_type: AuditActorType,
) -> None:
    try:
        record_audit_event(
            db,
            action=f"subscription_{outcome.command.kind.value}",
            entity_type="subscription_lifecycle_command",
            entity_id=outcome.command.subscription_id,
            actor_type=actor_type,
            actor_id=actor_id,
            request_id=_audit_request_id(outcome.command.idempotency_key),
            is_success=outcome.status
            in {
                SubscriptionCommandOutcomeStatus.applied,
                SubscriptionCommandOutcomeStatus.scheduled,
                SubscriptionCommandOutcomeStatus.skipped,
            },
            status_code=(
                200
                if outcome.status
                in {
                    SubscriptionCommandOutcomeStatus.applied,
                    SubscriptionCommandOutcomeStatus.scheduled,
                    SubscriptionCommandOutcomeStatus.skipped,
                }
                else 409
                if outcome.status
                in {
                    SubscriptionCommandOutcomeStatus.rejected,
                    SubscriptionCommandOutcomeStatus.superseded,
                }
                else 500
            ),
            metadata={
                "kind": outcome.command.kind.value,
                "source": outcome.command.source,
                "status": outcome.status.value,
                "previous_head": outcome.previous_head,
                "current_head": outcome.current_head,
                "artifact_ids": list(outcome.artifact_ids),
                "error_code": outcome.error_code,
                "replayed": outcome.replayed,
            },
        )
    except Exception:
        db.rollback()
        logger.exception(
            "Failed to record lifecycle command audit: subscription=%s kind=%s",
            outcome.command.subscription_id,
            outcome.command.kind.value,
        )


def _exception_message(exc: Exception) -> str:
    if isinstance(exc, HTTPException):
        return str(exc.detail)
    return str(exc) or type(exc).__name__


def _audit_request_id(idempotency_key: str | None) -> str | None:
    if idempotency_key is None or len(idempotency_key) <= 120:
        return idempotency_key
    digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _aware_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


__all__ = [
    "SubscriptionCommandBatchResult",
    "SubscriptionCommandExecutionRejected",
    "confirm_subscription_service_change",
    "execute_subscription_command_batch",
    "execute_subscription_command",
]
