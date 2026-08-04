"""Finance-approved opening positions for ADR 0007 customer-subledger cutover.

The verifier records an immutable cohort proposal first. This migration owner
may capture only that exact, separately operator- and finance-approved result.
Each account/currency residual and its posting group share one owner command;
the complete cohort must be source-valid before capture. Existing immutable
openings are preserved while a later completion run adds only missing rows.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.billing_shadow_verification import BillingCutoverVerificationRun
from app.models.customer_subledger import (
    CustomerSubledgerAuthorityCutover,
    CustomerSubledgerOpeningPosition,
    PositionEffectKind,
    PostingCommandKind,
    PostingProducer,
    PostingSourceKind,
)
from app.services.billing.customer_subledger import (
    EffectInput,
    StagePostingGroupCommand,
    stage_posting_group,
)
from app.services.common import round_money
from app.services.domain_errors import DomainError
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.locking import lock_for_update
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)


def _object_dict(value: object) -> dict[str, object]:
    """Narrow persisted JSON before using it as command evidence."""

    if not isinstance(value, dict):
        return {}
    return {str(key): item for key, item in value.items()}


def _object_dict_rows(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [_object_dict(item) for item in value if isinstance(item, dict)]


OWNER = "financial.customer_subledger_opening_positions"
CONCERN = "reviewed customer-subledger opening-position capture"
_CAPTURE_COMMAND = OwnerCommandDefinition(
    owner=OWNER,
    concern=CONCERN,
    name="capture_customer_subledger_opening_positions",
)
_CUTOVER_COMMAND = OwnerCommandDefinition(
    owner=OWNER,
    concern="customer-subledger authority cutover activation",
    name="activate_customer_subledger_authority",
)


class CustomerSubledgerOpeningError(DomainError):
    """Fail-closed opening-position migration error."""


def _error(
    suffix: str, message: str, **details: object
) -> CustomerSubledgerOpeningError:
    return CustomerSubledgerOpeningError(
        code=f"{OWNER}.{suffix}", message=message, details=dict(details)
    )


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode(
            "utf-8"
        )
    ).hexdigest()


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class CaptureCustomerSubledgerOpeningsCommand:
    """Exact approved verifier result to materialize as opening groups."""

    context: CommandContext
    verification_run_id: UUID
    expected_result_fingerprint: str
    review_reference: str


@dataclass(frozen=True, slots=True)
class CustomerSubledgerOpeningCaptureResult:
    verification_run_id: UUID
    captured_count: int
    zero_count: int
    positive_total: Decimal
    negative_total: Decimal
    replayed: bool


@dataclass(frozen=True, slots=True)
class ActivateCustomerSubledgerAuthorityCommand:
    """Exact approved zero-blocker parity run authorising read/write cutover."""

    context: CommandContext
    verification_run_id: UUID
    expected_result_fingerprint: str
    review_reference: str


@dataclass(frozen=True, slots=True)
class CustomerSubledgerAuthorityResult:
    cutover_id: UUID
    verification_run_id: UUID
    cutover_at: datetime
    replayed: bool


def capture_customer_subledger_opening_positions(
    db: Session,
    command: CaptureCustomerSubledgerOpeningsCommand,
) -> CustomerSubledgerOpeningCaptureResult:
    """Capture exactly one approved opening residual per eligible account."""

    return execute_owner_command(
        db,
        definition=_CAPTURE_COMMAND,
        context=command.context,
        operation=lambda: _capture(db, command),
    )


def _capture(
    db: Session,
    command: CaptureCustomerSubledgerOpeningsCommand,
) -> CustomerSubledgerOpeningCaptureResult:
    if not command.context.idempotency_key:
        raise _error(
            "missing_idempotency_key",
            "Opening-position capture requires an idempotency key.",
        )
    expected = command.expected_result_fingerprint.strip().lower()
    if len(expected) != 64:
        raise _error(
            "invalid_result_fingerprint",
            "Opening-position result fingerprint must be a SHA-256 digest.",
        )
    reference = command.review_reference.strip()
    if not reference:
        raise _error(
            "missing_review_reference",
            "Opening-position capture requires a durable review reference.",
        )
    run = lock_for_update(
        db, BillingCutoverVerificationRun, command.verification_run_id
    )
    if run is None or run.phase != "phase_3_opening_preview":
        raise _error(
            "verification_run_not_found",
            "The approved Phase 3 opening preview does not exist.",
            run_id=str(command.verification_run_id),
        )
    if run.result_fingerprint != expected:
        raise _error(
            "stale_reviewed_preview",
            "The supplied result fingerprint is not the reviewed preview.",
            run_id=str(run.id),
        )
    if not run.approved:
        raise _error(
            "approval_required",
            "Opening capture requires operator and finance approval on a clean run.",
            run_id=str(run.id),
        )
    details = _object_dict((run.cohort_classification or {}).get("_details"))
    rows = _object_dict_rows(details.get("opening_rows"))
    result_contract = _object_dict(details.get("opening_result_contract"))
    fingerprint_payload: object = result_contract or rows
    if _digest(fingerprint_payload) != run.result_fingerprint:
        raise _error(
            "corrupt_reviewed_preview",
            "Stored opening evidence no longer matches its immutable fingerprint.",
            run_id=str(run.id),
        )
    currency = str((run.currency_totals or {}).get("currency") or "").upper()
    if len(currency) != 3:
        raise _error(
            "corrupt_reviewed_preview",
            "Stored opening preview has no valid currency.",
            run_id=str(run.id),
        )

    existing = list(
        db.scalars(
            select(CustomerSubledgerOpeningPosition).where(
                CustomerSubledgerOpeningPosition.verification_run_id == run.id
            )
        ).all()
    )
    if existing:
        expected_rows = {
            (UUID(str(row["account_id"])), str(row["evidence_fingerprint"]))
            for row in rows
        }
        recorded_rows = {(row.account_id, row.evidence_fingerprint) for row in existing}
        if recorded_rows != expected_rows:
            raise _error(
                "idempotency_conflict",
                "Recorded opening positions differ from the reviewed result.",
                run_id=str(run.id),
            )
        return _result(run.id, existing, replayed=True)

    account_ids = tuple(UUID(str(row["account_id"])) for row in rows)
    conflicting = list(
        db.scalars(
            select(CustomerSubledgerOpeningPosition).where(
                CustomerSubledgerOpeningPosition.account_id.in_(account_ids),
                CustomerSubledgerOpeningPosition.currency == currency,
            )
        ).all()
    )
    if conflicting:
        raise _error(
            "opening_position_already_captured",
            "An account already has an immutable opening position.",
            account_count=len(conflicting),
        )

    captured: list[CustomerSubledgerOpeningPosition] = []
    for payload in sorted(rows, key=lambda item: str(item["account_id"])):
        account_id = UUID(str(payload["account_id"]))
        legacy = round_money(Decimal(str(payload["legacy_position"])))
        shadow = round_money(Decimal(str(payload["shadow_position_before"])))
        delta = round_money(Decimal(str(payload["opening_delta"])))
        if delta != round_money(legacy - shadow):
            raise _error(
                "corrupt_reviewed_preview",
                "Opening residual no longer equals legacy minus shadow.",
                account_id=str(account_id),
            )
        evidence_fingerprint = str(payload["evidence_fingerprint"])
        if len(evidence_fingerprint) != 64:
            raise _error(
                "corrupt_reviewed_preview",
                "Opening row has an invalid evidence fingerprint.",
                account_id=str(account_id),
            )
        baseline_id = payload.get("baseline_id")
        opening = CustomerSubledgerOpeningPosition(
            verification_run_id=run.id,
            baseline_id=UUID(str(baseline_id)) if baseline_id else None,
            account_id=account_id,
            currency=currency,
            legacy_position=legacy,
            shadow_position_before=shadow,
            opening_delta=delta,
            evidence_fingerprint=evidence_fingerprint,
            review_reference=reference,
            captured_by=command.context.actor,
            command_id=command.context.command_id,
            correlation_id=command.context.correlation_id,
            occurred_at=_utc(run.cutoff_at),
        )
        db.add(opening)
        db.flush()
        effects: tuple[EffectInput, ...]
        if delta > 0:
            effects = (
                EffectInput(
                    effect=PositionEffectKind.customer_credit_created,
                    amount=delta,
                ),
            )
        elif delta < 0:
            effects = (
                EffectInput(
                    effect=PositionEffectKind.customer_credit_consumed,
                    amount=abs(delta),
                ),
            )
        else:
            effects = ()
        stage_posting_group(
            db,
            StagePostingGroupCommand(
                account_id=account_id,
                currency=currency,
                command_kind=PostingCommandKind.opening_position,
                producer_owner=PostingProducer.customer_subledger_opening_positions,
                source_kind=PostingSourceKind.customer_subledger_opening_position,
                source_id=opening.id,
                occurred_at=_utc(run.cutoff_at),
                effects=effects,
                idempotency_key=(
                    f"posting:customer_subledger_opening:{account_id}:{currency}"
                ),
            ),
            context=command.context,
        )
        captured.append(opening)

    captured_quarantine = details.get("quarantined_accounts")
    emit_event(
        db,
        EventType.customer_subledger_opening_positions_captured,
        {
            "verification_run_id": str(run.id),
            "result_fingerprint": run.result_fingerprint,
            "currency": currency,
            "captured_count": len(captured),
            "quarantined_count": (
                len(captured_quarantine) if isinstance(captured_quarantine, list) else 0
            ),
            "authority_moved": False,
        },
        actor=command.context.actor,
    )
    return _result(run.id, captured, replayed=False)


def _result(
    run_id: UUID,
    rows: list[CustomerSubledgerOpeningPosition],
    *,
    replayed: bool,
) -> CustomerSubledgerOpeningCaptureResult:
    positive = sum(
        (Decimal(row.opening_delta) for row in rows if row.opening_delta > 0),
        Decimal("0"),
    )
    negative = sum(
        (abs(Decimal(row.opening_delta)) for row in rows if row.opening_delta < 0),
        Decimal("0"),
    )
    return CustomerSubledgerOpeningCaptureResult(
        verification_run_id=run_id,
        captured_count=len(rows),
        zero_count=sum(Decimal(row.opening_delta) == 0 for row in rows),
        positive_total=round_money(positive),
        negative_total=round_money(negative),
        replayed=replayed,
    )


def activate_customer_subledger_authority(
    db: Session,
    command: ActivateCustomerSubledgerAuthorityCommand,
) -> CustomerSubledgerAuthorityResult:
    """Irreversibly activate subledger writes and default position reads."""

    return execute_owner_command(
        db,
        definition=_CUTOVER_COMMAND,
        context=command.context,
        operation=lambda: _activate_authority(db, command),
    )


def _activate_authority(
    db: Session,
    command: ActivateCustomerSubledgerAuthorityCommand,
) -> CustomerSubledgerAuthorityResult:
    if not command.context.idempotency_key:
        raise _error(
            "missing_idempotency_key",
            "Customer-subledger cutover requires an idempotency key.",
        )
    expected = command.expected_result_fingerprint.strip().lower()
    if len(expected) != 64:
        raise _error(
            "invalid_result_fingerprint",
            "Cutover result fingerprint must be a SHA-256 digest.",
        )
    reference = command.review_reference.strip()
    if not reference:
        raise _error(
            "missing_review_reference",
            "Customer-subledger cutover requires a durable review reference.",
        )
    existing = db.scalar(select(CustomerSubledgerAuthorityCutover).limit(1))
    if existing is not None:
        if (
            existing.verification_run_id == command.verification_run_id
            and existing.result_fingerprint == expected
        ):
            return CustomerSubledgerAuthorityResult(
                cutover_id=existing.id,
                verification_run_id=existing.verification_run_id,
                cutover_at=_utc(existing.cutover_at),
                replayed=True,
            )
        raise _error(
            "authority_already_activated",
            "Customer-subledger authority has one irreversible activation.",
            cutover_id=str(existing.id),
        )
    run = lock_for_update(
        db, BillingCutoverVerificationRun, command.verification_run_id
    )
    if run is None or run.phase != "phase_3_subledger_parity":
        raise _error(
            "verification_run_not_found",
            "The approved Phase 3 subledger parity run does not exist.",
            run_id=str(command.verification_run_id),
        )
    if run.result_fingerprint != expected:
        raise _error(
            "stale_reviewed_preview",
            "Cutover fingerprint is not the approved parity result.",
            run_id=str(run.id),
        )
    if not run.approved:
        raise _error(
            "approval_required",
            "Cutover requires operator and finance approval on a zero-blocker run.",
            run_id=str(run.id),
        )
    details = _object_dict((run.cohort_classification or {}).get("_details"))
    raw_quarantined = details.get("quarantined_accounts")
    quarantined = (
        {str(value) for value in raw_quarantined}
        if isinstance(raw_quarantined, list)
        else set()
    )
    if quarantined:
        raise _error(
            "source_cohort_incomplete",
            "Customer-subledger authority cannot activate with excluded accounts.",
            excluded_count=len(quarantined),
        )
    cutover = CustomerSubledgerAuthorityCutover(
        verification_run_id=run.id,
        result_fingerprint=run.result_fingerprint,
        review_reference=reference,
        activated_by=command.context.actor,
        command_id=command.context.command_id,
        correlation_id=command.context.correlation_id,
        cutover_at=datetime.now(UTC),
    )
    db.add(cutover)
    db.flush()
    emit_event(
        db,
        EventType.customer_subledger_authority_activated,
        {
            "cutover_id": str(cutover.id),
            "verification_run_id": str(run.id),
            "result_fingerprint": run.result_fingerprint,
            "cutover_at": cutover.cutover_at.isoformat(),
            "quarantined_count": len(quarantined),
            "authority_moved": True,
        },
        actor=command.context.actor,
    )
    return CustomerSubledgerAuthorityResult(
        cutover_id=cutover.id,
        verification_run_id=run.id,
        cutover_at=_utc(cutover.cutover_at),
        replayed=False,
    )


__all__ = [
    "ActivateCustomerSubledgerAuthorityCommand",
    "CaptureCustomerSubledgerOpeningsCommand",
    "CustomerSubledgerAuthorityResult",
    "CustomerSubledgerOpeningCaptureResult",
    "CustomerSubledgerOpeningError",
    "activate_customer_subledger_authority",
    "capture_customer_subledger_opening_positions",
]
