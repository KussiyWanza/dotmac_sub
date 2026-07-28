"""Durable Phase 1 shadow-pipeline and cutover-verification evidence.

This owner records delivery completion and complete-cohort migration evidence.
It never repairs another owner and never changes billing authority. A run can
be approved only after every blocker count is zero; finance approval remains a
separate, explicit command.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.billing_contract import (
    BillingContract,
    BillingContractVersion,
    BillingContractVersionStatus,
    BillingRecordAuthority,
    CollectionTiming,
    IntervalUnit,
)
from app.models.billing_shadow_verification import (
    BillingCutoverVerificationRun,
    BillingShadowDeliveryEvidence,
)
from app.models.catalog import (
    BillingCycle,
    BillingMode,
    Subscription,
    SubscriptionStatus,
)
from app.models.event_store import EventStore
from app.services.domain_errors import DomainError
from app.services.events import emit_event
from app.services.events.owner_outputs import consume_owner_output
from app.services.events.types import EventType
from app.services.locking import lock_for_update
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

OWNER = "billing.shadow_verification"

_DELIVERY_COMMAND = OwnerCommandDefinition(
    owner=OWNER,
    concern="shadow pipeline delivery evidence",
    name="consume_terminal_shadow_output",
)
_RUN_COMMAND = OwnerCommandDefinition(
    owner=OWNER,
    concern="phase cutover verification evidence",
    name="record_phase1_verification_run",
)
_APPROVAL_COMMAND = OwnerCommandDefinition(
    owner=OWNER,
    concern="phase cutover verification evidence",
    name="approve_phase1_verification_run",
)

_CYCLE_INTERVAL: dict[BillingCycle, tuple[IntervalUnit, int]] = {
    BillingCycle.daily: (IntervalUnit.day, 1),
    BillingCycle.weekly: (IntervalUnit.week, 1),
    BillingCycle.monthly: (IntervalUnit.month, 1),
    BillingCycle.quarterly: (IntervalUnit.month, 3),
    BillingCycle.annual: (IntervalUnit.year, 1),
}


class BillingShadowVerificationError(DomainError):
    """Fail-closed migration-evidence error."""


def _error(
    suffix: str,
    message: str,
    **details: object,
) -> BillingShadowVerificationError:
    return BillingShadowVerificationError(
        code=f"{OWNER}.{suffix}",
        message=message,
        details=dict(details),
    )


def _digest(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _enum_value(value: object) -> str | None:
    if value is None:
        return None
    resolved = getattr(value, "value", value)
    return str(resolved)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


@dataclass(frozen=True)
class RecordPhase1VerificationCommand:
    """Exact observation window and code/schema identity for one run."""

    cutoff_at: datetime
    observation_started_at: datetime
    observation_ended_at: datetime
    code_version: str
    database_schema_version: str
    cohort_name: str = "active_subscriptions"
    policy_version: str = "adr-0007-phase-1-v1"
    evidence_schema_version: int = 1


@dataclass(frozen=True)
class Phase1VerificationResult:
    """Stored run identity and its approval blockers."""

    run_id: UUID
    cohort_count: int
    covered_count: int
    blocker_count: int
    replayed: bool


def _validate_run(command: RecordPhase1VerificationCommand) -> None:
    for field, value in (
        ("cutoff_at", command.cutoff_at),
        ("observation_started_at", command.observation_started_at),
        ("observation_ended_at", command.observation_ended_at),
    ):
        if value.tzinfo is None:
            raise _error(
                "invalid_observation_window",
                "Verification timestamps must be timezone-aware.",
                field=field,
            )
    if not (
        command.observation_started_at
        <= command.observation_ended_at
        <= command.cutoff_at
    ):
        raise _error(
            "invalid_observation_window",
            "Observation window must end at or before the run cutoff.",
        )
    for field, text_value in (
        ("code_version", command.code_version),
        ("database_schema_version", command.database_schema_version),
        ("cohort_name", command.cohort_name),
        ("policy_version", command.policy_version),
    ):
        if not text_value.strip():
            raise _error(
                "invalid_run_identity",
                "Verification run identity fields cannot be empty.",
                field=field,
            )


class BillingShadowVerification:
    """Owner commands for shadow delivery, evidence runs, and approvals."""

    @staticmethod
    def consume_terminal_output(
        db: Session,
        *,
        sales_order_id: UUID,
        obligation_ids: tuple[UUID, ...],
        event_id: UUID,
        context: CommandContext,
    ) -> UUID | None:
        """Receipt the terminal obligation output with content-addressed evidence."""

        def _effect() -> UUID:
            evidence = BillingShadowDeliveryEvidence(
                sales_order_id=sales_order_id,
                terminal_event_id=event_id,
                obligation_count=len(obligation_ids),
                obligation_ids_sha256=_digest(
                    sorted(str(item) for item in obligation_ids)
                ),
                command_id=context.command_id,
                correlation_id=context.correlation_id,
            )
            db.add(evidence)
            db.flush()
            emit_event(
                db,
                EventType.billing_shadow_delivery_recorded,
                {
                    "evidence_id": str(evidence.id),
                    "sales_order_id": str(sales_order_id),
                    "terminal_event_id": str(event_id),
                    "obligation_count": evidence.obligation_count,
                    "obligation_ids_sha256": evidence.obligation_ids_sha256,
                },
                actor=context.actor,
            )
            return evidence.id

        return execute_owner_command(
            db,
            definition=_DELIVERY_COMMAND,
            context=context,
            operation=lambda: consume_owner_output(
                db,
                consumer=OWNER,
                event_id=event_id,
                event_type="billing.obligations.shadow_scheduled",
                producer_owner="billing.obligations",
                context=context,
                operation=_effect,
            )[0],
        )

    @staticmethod
    def record_phase1_run(
        db: Session,
        command: RecordPhase1VerificationCommand,
        *,
        context: CommandContext,
    ) -> Phase1VerificationResult:
        """Materialize one complete active-subscription cohort observation."""

        return execute_owner_command(
            db,
            definition=_RUN_COMMAND,
            context=context,
            operation=lambda: BillingShadowVerification._record_phase1_run(
                db,
                command=command,
                context=context,
            ),
        )

    @staticmethod
    def _record_phase1_run(
        db: Session,
        *,
        command: RecordPhase1VerificationCommand,
        context: CommandContext,
    ) -> Phase1VerificationResult:
        _validate_run(command)
        if not context.idempotency_key:
            raise _error(
                "missing_idempotency_key",
                "A verification run requires a business idempotency key.",
            )
        existing = db.execute(
            select(BillingCutoverVerificationRun).where(
                BillingCutoverVerificationRun.idempotency_key == context.idempotency_key
            )
        ).scalar_one_or_none()
        if existing is not None:
            expected_identity = (
                command.cohort_name,
                command.evidence_schema_version,
                command.policy_version,
                _utc(command.cutoff_at),
                _utc(command.observation_started_at),
                _utc(command.observation_ended_at),
                command.code_version,
                command.database_schema_version,
            )
            recorded_identity = (
                existing.cohort_name,
                existing.evidence_schema_version,
                existing.policy_version,
                _utc(existing.cutoff_at),
                _utc(existing.observation_started_at),
                _utc(existing.observation_ended_at),
                existing.code_version,
                existing.database_schema_version,
            )
            if recorded_identity != expected_identity:
                raise _error(
                    "idempotency_conflict",
                    "Verification idempotency key was reused for another run identity.",
                    run_id=str(existing.id),
                )
            return BillingShadowVerification._result(existing, replayed=True)

        subscriptions = list(
            db.execute(
                select(Subscription)
                .where(
                    Subscription.status == SubscriptionStatus.active,
                    Subscription.created_at <= command.cutoff_at,
                )
                .order_by(Subscription.id)
                .with_for_update()
            ).scalars()
        )
        subscription_ids = [item.id for item in subscriptions]
        version_rows = (
            db.execute(
                select(BillingContract, BillingContractVersion)
                .join(
                    BillingContractVersion,
                    BillingContractVersion.contract_id == BillingContract.id,
                )
                .where(
                    BillingContract.subscription_id.in_(subscription_ids),
                    BillingContractVersion.status.in_(
                        (
                            BillingContractVersionStatus.effective,
                            BillingContractVersionStatus.superseded,
                        )
                    ),
                    BillingContractVersion.starts_at <= command.cutoff_at,
                    (BillingContractVersion.ends_at.is_(None))
                    | (BillingContractVersion.ends_at > command.cutoff_at),
                )
                .order_by(
                    BillingContract.subscription_id,
                    BillingContractVersion.version,
                )
                .with_for_update()
            ).all()
            if subscription_ids
            else []
        )
        versions_by_subscription: dict[UUID, list[BillingContractVersion]] = (
            defaultdict(list)
        )
        for contract, version in version_rows:
            versions_by_subscription[contract.subscription_id].append(version)

        classification: dict[str, list[str]] = {
            "covered": [],
            "unresolved": [],
            "ambiguous": [],
            "unexpected_unlinked": [],
            "duplicate": [],
            "shadow_variance": [],
        }
        source_rows: list[dict[str, object]] = []
        result_rows: list[dict[str, object]] = []
        currency_totals: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))

        for subscription in subscriptions:
            source_rows.append(
                {
                    "subscription_id": str(subscription.id),
                    "account_id": str(subscription.subscriber_id),
                    "status": _enum_value(subscription.status),
                    "billing_mode": _enum_value(subscription.billing_mode),
                    "billing_cycle": _enum_value(subscription.billing_cycle),
                    "unit_price": (
                        str(subscription.unit_price)
                        if subscription.unit_price is not None
                        else None
                    ),
                }
            )
            versions = versions_by_subscription.get(subscription.id, [])
            if not versions:
                classification["unexpected_unlinked"].append(str(subscription.id))
                continue
            if len(versions) > 1:
                classification["ambiguous"].append(str(subscription.id))
                classification["duplicate"].append(str(subscription.id))
                continue
            version = versions[0]
            result_rows.append(
                {
                    "subscription_id": str(subscription.id),
                    "version_id": str(version.id),
                    "authority": version.authority.value,
                    "contracted_price": str(version.contracted_price),
                    "currency": version.currency,
                    "service_interval_unit": version.service_interval_unit.value,
                    "service_interval_count": version.service_interval_count,
                    "collection_timing": version.collection_timing.value,
                }
            )
            billing_cycle = subscription.billing_cycle
            billing_mode = subscription.billing_mode
            unit_price = subscription.unit_price
            if billing_cycle is None or billing_mode is None or unit_price is None:
                classification["unresolved"].append(str(subscription.id))
                continue
            unit, count = _CYCLE_INTERVAL[billing_cycle]
            expected_timing = (
                CollectionTiming.advance
                if billing_mode is BillingMode.prepaid
                else CollectionTiming.arrears
            )
            variance = any(
                (
                    version.authority is not BillingRecordAuthority.shadow,
                    version.service_interval_unit is not unit,
                    version.service_interval_count != count,
                    version.invoice_interval_unit is not unit,
                    version.invoice_interval_count != count,
                    version.collection_timing is not expected_timing,
                    version.contracted_price != unit_price,
                )
            )
            if variance:
                classification["shadow_variance"].append(str(subscription.id))
                continue
            classification["covered"].append(str(subscription.id))
            currency_totals[version.currency] += version.contracted_price

        observed_events = list(
            db.execute(
                select(EventStore).where(
                    EventStore.created_at >= command.observation_started_at,
                    EventStore.created_at <= command.observation_ended_at,
                )
            ).scalars()
        )
        event_outcomes: dict[str, dict[str, int]] = {}
        for output in (
            "sales.fulfillment.funding_applied",
            "billing.contracts.shadow_recorded",
            "billing.obligations.shadow_scheduled",
        ):
            matching = [
                event
                for event in observed_events
                if event.payload.get("output") == output
            ]
            event_outcomes[output] = {
                status: sum(event.status.value == status for event in matching)
                for status in ("pending", "processing", "completed", "failed")
            }

        run = BillingCutoverVerificationRun(
            phase="phase_1",
            cohort_name=command.cohort_name,
            evidence_schema_version=command.evidence_schema_version,
            policy_version=command.policy_version,
            cutoff_at=command.cutoff_at,
            observation_started_at=command.observation_started_at,
            observation_ended_at=command.observation_ended_at,
            cohort_count=len(subscriptions),
            covered_count=len(classification["covered"]),
            unresolved_count=len(classification["unresolved"]),
            ambiguous_count=len(classification["ambiguous"]),
            unexpected_unlinked_count=len(classification["unexpected_unlinked"]),
            duplicate_count=len(classification["duplicate"]),
            shadow_variance_count=len(classification["shadow_variance"]),
            source_fingerprint=_digest(source_rows),
            result_fingerprint=_digest(result_rows),
            currency_totals={
                currency: str(total)
                for currency, total in sorted(currency_totals.items())
            },
            cohort_classification=classification,
            event_outcomes=event_outcomes,
            code_version=command.code_version,
            database_schema_version=command.database_schema_version,
            idempotency_key=context.idempotency_key,
            command_id=context.command_id,
            correlation_id=context.correlation_id,
            actor=context.actor,
            reason=context.reason,
        )
        db.add(run)
        db.flush()
        emit_event(
            db,
            EventType.billing_cutover_verification_recorded,
            {
                "run_id": str(run.id),
                "phase": run.phase,
                "cohort_count": run.cohort_count,
                "covered_count": run.covered_count,
                "blocker_count": sum(
                    (
                        run.unresolved_count,
                        run.ambiguous_count,
                        run.unexpected_unlinked_count,
                        run.duplicate_count,
                        run.shadow_variance_count,
                    )
                ),
                "source_fingerprint": run.source_fingerprint,
                "result_fingerprint": run.result_fingerprint,
            },
            actor=context.actor,
        )
        return BillingShadowVerification._result(run, replayed=False)

    @staticmethod
    def approve_operator(
        db: Session,
        *,
        run_id: UUID,
        context: CommandContext,
        approved_at: datetime,
    ) -> UUID:
        return execute_owner_command(
            db,
            definition=_APPROVAL_COMMAND,
            context=context,
            operation=lambda: BillingShadowVerification._approve(
                db,
                run_id=run_id,
                context=context,
                approved_at=approved_at,
                finance=False,
            ),
        )

    @staticmethod
    def approve_finance(
        db: Session,
        *,
        run_id: UUID,
        context: CommandContext,
        approved_at: datetime,
    ) -> UUID:
        return execute_owner_command(
            db,
            definition=_APPROVAL_COMMAND,
            context=context,
            operation=lambda: BillingShadowVerification._approve(
                db,
                run_id=run_id,
                context=context,
                approved_at=approved_at,
                finance=True,
            ),
        )

    @staticmethod
    def _approve(
        db: Session,
        *,
        run_id: UUID,
        context: CommandContext,
        approved_at: datetime,
        finance: bool,
    ) -> UUID:
        if approved_at.tzinfo is None:
            raise _error(
                "invalid_approval",
                "Verification approval time must be timezone-aware.",
            )
        run = lock_for_update(db, BillingCutoverVerificationRun, run_id)
        if run is None:
            raise _error(
                "verification_run_not_found",
                "Billing verification run does not exist.",
                run_id=str(run_id),
            )
        if not run.blockers_are_zero:
            raise _error(
                "verification_blockers_present",
                "A run with non-zero blocker categories cannot be approved.",
                run_id=str(run.id),
            )
        previous_actor = (
            run.finance_approved_by if finance else run.operator_approved_by
        )
        previous_at = run.finance_approved_at if finance else run.operator_approved_at
        if previous_actor is not None or previous_at is not None:
            if (
                previous_actor == context.actor
                and previous_at is not None
                and _utc(previous_at) == _utc(approved_at)
            ):
                return run.id
            raise _error(
                "approval_already_recorded",
                "A different immutable approval is already recorded for this role.",
                run_id=str(run.id),
                approval="finance" if finance else "operator",
            )
        if finance:
            if run.operator_approved_at is None:
                raise _error(
                    "operator_approval_required",
                    "Finance approval requires prior operator approval.",
                    run_id=str(run.id),
                )
            run.finance_approved_by = context.actor
            run.finance_approved_at = approved_at
        else:
            run.operator_approved_by = context.actor
            run.operator_approved_at = approved_at
        db.flush()
        emit_event(
            db,
            EventType.billing_cutover_verification_approved,
            {
                "run_id": str(run.id),
                "approval": "finance" if finance else "operator",
                "approved_by": context.actor,
                "approved_at": approved_at.isoformat(),
            },
            actor=context.actor,
        )
        return run.id

    @staticmethod
    def _result(
        run: BillingCutoverVerificationRun,
        *,
        replayed: bool,
    ) -> Phase1VerificationResult:
        return Phase1VerificationResult(
            run_id=run.id,
            cohort_count=run.cohort_count,
            covered_count=run.covered_count,
            blocker_count=sum(
                (
                    run.unresolved_count,
                    run.ambiguous_count,
                    run.unexpected_unlinked_count,
                    run.duplicate_count,
                    run.shadow_variance_count,
                )
            ),
            replayed=replayed,
        )


__all__ = [
    "BillingShadowVerification",
    "BillingShadowVerificationError",
    "Phase1VerificationResult",
    "RecordPhase1VerificationCommand",
]
