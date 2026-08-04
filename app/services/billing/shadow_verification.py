"""Durable billing shadow-pipeline and cutover-verification evidence.

This owner records delivery completion and complete-cohort migration evidence.
It never repairs another owner and never changes billing authority. A run can
be approved only after every blocker count is zero; finance approval remains a
separate, explicit command. Phase 2 comparisons are migration evidence only:
they cannot request cross-owner repair or select which owner is correct.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.billing_contract import (
    BillingContract,
    BillingContractLine,
    BillingContractVersion,
    BillingContractVersionStatus,
    BillingObligation,
    BillingRecordAuthority,
    ChargeComponent,
    CollectionTiming,
    IntervalUnit,
    RateBasis,
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
from app.services.billing.contracts import BillingContracts
from app.services.common import round_money
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


def _object_dict(value: object) -> dict[str, object]:
    """Narrow a JSON object without trusting its persisted runtime shape."""

    if not isinstance(value, dict):
        return {}
    return {str(key): item for key, item in value.items()}


def _object_dict_rows(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [_object_dict(item) for item in value if isinstance(item, dict)]


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
_PHASE2_RUN_COMMAND = OwnerCommandDefinition(
    owner=OWNER,
    concern="phase cutover verification evidence",
    name="record_phase2_verification_run",
)
_PHASE3_FORWARD_RUN_COMMAND = OwnerCommandDefinition(
    owner=OWNER,
    concern="phase cutover verification evidence",
    name="record_phase3_forward_verification_run",
)
_PHASE3_OPENING_PREVIEW_COMMAND = OwnerCommandDefinition(
    owner=OWNER,
    concern="phase cutover verification evidence",
    name="record_phase3_opening_preview",
)
_PHASE3_PARITY_COMMAND = OwnerCommandDefinition(
    owner=OWNER,
    concern="phase cutover verification evidence",
    name="record_phase3_subledger_parity",
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


@dataclass(frozen=True)
class RecordPhase2VerificationCommand:
    """Exact Phase 2 cohort cutoff and immutable build identity."""

    cutoff_at: datetime
    observation_started_at: datetime
    observation_ended_at: datetime
    code_version: str
    database_schema_version: str
    cohort_name: str = "active_subscriptions"
    policy_version: str = "adr-0007-phase-2-v1"
    evidence_schema_version: int = 2


@dataclass(frozen=True)
class Phase2VerificationResult:
    """Stored Phase 2 parity/topology evidence and approval blockers."""

    run_id: UUID
    cohort_count: int
    covered_count: int
    expected_difference_count: int
    blocker_count: int
    replayed: bool


VerificationCommand = RecordPhase1VerificationCommand | RecordPhase2VerificationCommand


def _validate_run(command: VerificationCommand) -> None:
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


def _run_identity(
    command: VerificationCommand,
) -> tuple[str, int, str, datetime, datetime, datetime, str, str]:
    return (
        command.cohort_name,
        command.evidence_schema_version,
        command.policy_version,
        _utc(command.cutoff_at),
        _utc(command.observation_started_at),
        _utc(command.observation_ended_at),
        command.code_version,
        command.database_schema_version,
    )


def _mark(
    classification: dict[str, list[str]],
    category: str,
    subscription_id: UUID,
) -> None:
    value = str(subscription_id)
    if value not in classification[category]:
        classification[category].append(value)


def _topology_categories(
    obligations: list[BillingObligation],
) -> set[str]:
    """Classify persisted target-period topology without repairing it."""

    categories: set[str] = set()
    ordered = sorted(
        obligations,
        key=lambda row: (_utc(row.period_start), _utc(row.period_end), str(row.id)),
    )
    seen: set[tuple[datetime, datetime]] = set()
    for row in ordered:
        identity = (_utc(row.period_start), _utc(row.period_end))
        if identity in seen:
            categories.add("duplicate")
        seen.add(identity)
    for previous, current in zip(ordered, ordered[1:], strict=False):
        previous_end = _utc(previous.period_end)
        current_start = _utc(current.period_start)
        if previous_end < current_start:
            categories.add("gap")
        elif previous_end > current_start:
            categories.add("overlap")
    return categories


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
            if existing.phase != "phase_1":
                raise _error(
                    "idempotency_conflict",
                    "Verification idempotency key belongs to another phase.",
                    run_id=str(existing.id),
                )
            expected_identity = _run_identity(command)
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
            expected_difference_count=0,
            gap_count=0,
            overlap_count=0,
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
    def record_phase2_run(
        db: Session,
        command: RecordPhase2VerificationCommand,
        *,
        context: CommandContext,
    ) -> Phase2VerificationResult:
        """Record complete-cohort rating parity and obligation topology.

        This command stores immutable migration evidence. It does not invoke a
        financial writer, repair a contract/obligation/legacy source, or move
        authority. A mismatch therefore remains a blocker for the owner of the
        wrong fact to correct through its own command.
        """

        return execute_owner_command(
            db,
            definition=_PHASE2_RUN_COMMAND,
            context=context,
            operation=lambda: BillingShadowVerification._record_phase2_run(
                db,
                command=command,
                context=context,
            ),
        )

    @staticmethod
    def _record_phase2_run(
        db: Session,
        *,
        command: RecordPhase2VerificationCommand,
        context: CommandContext,
    ) -> Phase2VerificationResult:
        from app.services.billing.cadence import (
            CadenceError,
            period_containing,
        )
        from app.services.billing.obligations import (
            BillingObligationError,
            BillingObligations,
        )
        from app.services.billing.rating import (
            BillingRatingError,
            rate_line_period,
        )
        from app.services.billing_automation import (
            PostpaidChargePreviewError,
            RecurringChargeComponentKind,
            preview_postpaid_recurring_charge,
        )
        from app.services.prepaid_service_renewals import (
            PrepaidServiceRenewalError,
            preview_prepaid_recurring_charge,
        )

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
            if existing.phase != "phase_2":
                raise _error(
                    "idempotency_conflict",
                    "Verification idempotency key belongs to another phase.",
                    run_id=str(existing.id),
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
            if recorded_identity != _run_identity(command):
                raise _error(
                    "idempotency_conflict",
                    "Verification idempotency key was reused for another run identity.",
                    run_id=str(existing.id),
                )
            return BillingShadowVerification._phase2_result(
                existing,
                replayed=True,
            )

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
        subscription_ids = [subscription.id for subscription in subscriptions]
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
        version_ids: list[UUID] = []
        for contract, version in version_rows:
            versions_by_subscription[contract.subscription_id].append(version)
            version_ids.append(version.id)

        line_rows = (
            list(
                db.execute(
                    select(BillingContractLine)
                    .where(BillingContractLine.contract_version_id.in_(version_ids))
                    .order_by(
                        BillingContractLine.contract_version_id,
                        BillingContractLine.created_at,
                        BillingContractLine.id,
                    )
                ).scalars()
            )
            if version_ids
            else []
        )
        lines_by_version: dict[UUID, list[BillingContractLine]] = defaultdict(list)
        for line in line_rows:
            if not line.is_finite and line.charge_component in {
                ChargeComponent.recurring_service,
                ChargeComponent.addon,
            }:
                lines_by_version[line.contract_version_id].append(line)

        obligation_rows = (
            list(
                db.execute(
                    select(BillingObligation)
                    .where(BillingObligation.contract_version_id.in_(version_ids))
                    .order_by(
                        BillingObligation.contract_version_id,
                        BillingObligation.contract_line_key,
                        BillingObligation.period_start,
                        BillingObligation.id,
                    )
                    .with_for_update()
                ).scalars()
            )
            if version_ids
            else []
        )
        obligations_by_line: dict[tuple[UUID, UUID], list[BillingObligation]] = (
            defaultdict(list)
        )
        for obligation in obligation_rows:
            obligations_by_line[
                (obligation.contract_version_id, obligation.contract_line_key)
            ].append(obligation)

        classification: dict[str, list[str]] = {
            "covered": [],
            "expected_difference": [],
            "unresolved": [],
            "ambiguous": [],
            "unexpected_unlinked": [],
            "duplicate": [],
            "gap": [],
            "overlap": [],
            "shadow_variance": [],
        }
        source_rows: list[dict[str, object]] = []
        result_rows: list[dict[str, object]] = []
        legacy_totals: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
        target_totals: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
        classification_details: dict[str, list[str]] = defaultdict(list)

        def detail(subscription_id: UUID, reason: str) -> None:
            key = str(subscription_id)
            if reason not in classification_details[key]:
                classification_details[key].append(reason)

        blocker_categories = (
            "unresolved",
            "ambiguous",
            "unexpected_unlinked",
            "duplicate",
            "gap",
            "overlap",
            "shadow_variance",
        )

        for subscription in subscriptions:
            subscription_id = subscription.id
            versions = versions_by_subscription.get(subscription_id, [])
            source_row: dict[str, object] = {
                "subscription_id": str(subscription_id),
                "account_id": str(subscription.subscriber_id),
                "billing_mode": _enum_value(subscription.billing_mode),
                "billing_cycle": _enum_value(subscription.billing_cycle),
                "unit_price": (
                    str(subscription.unit_price)
                    if subscription.unit_price is not None
                    else None
                ),
                "start_at": (
                    _utc(subscription.start_at).isoformat()
                    if subscription.start_at is not None
                    else None
                ),
                "next_billing_at": (
                    _utc(subscription.next_billing_at).isoformat()
                    if subscription.next_billing_at is not None
                    else None
                ),
            }
            source_rows.append(source_row)
            if not versions:
                _mark(classification, "unexpected_unlinked", subscription_id)
                detail(subscription_id, "missing_effective_contract_version")
                continue
            if len(versions) > 1:
                _mark(classification, "ambiguous", subscription_id)
                _mark(classification, "duplicate", subscription_id)
                detail(subscription_id, "multiple_effective_contract_versions")
                continue

            version = versions[0]
            lines = lines_by_version.get(version.id, [])
            if not lines:
                _mark(classification, "unexpected_unlinked", subscription_id)
                detail(subscription_id, "missing_recurring_contract_line")
                continue
            line_keys = [line.contract_line_key for line in lines]
            if len(line_keys) != len(set(line_keys)):
                _mark(classification, "ambiguous", subscription_id)
                _mark(classification, "duplicate", subscription_id)
                detail(subscription_id, "duplicate_contract_line_key")
                continue

            legacy_period_start: datetime | None = None
            legacy_period_end: datetime | None = None
            legacy_currency: str | None = None
            legacy_gross: Decimal | None = None
            legacy_error: str | None = None
            legacy_component_rows: list[dict[str, object]] = []
            legacy_addon_component_keys: set[str] = set()
            legacy_preview_issues: list[dict[str, object]] = []
            prepaid_excluded_addon_keys: set[str] = set()
            if subscription.billing_mode is BillingMode.prepaid:
                try:
                    preview = preview_prepaid_recurring_charge(
                        db,
                        subscription_id=subscription_id,
                        as_of=command.cutoff_at,
                    )
                except PrepaidServiceRenewalError as exc:
                    legacy_error = exc.code
                else:
                    legacy_period_start = preview.period_start
                    legacy_period_end = preview.period_end
                    legacy_currency = preview.currency
                    legacy_gross = preview.gross_amount
                    prepaid_excluded_addon_keys = {
                        str(addon_id)
                        for addon_id in preview.excluded_recurring_addon_ids
                    }
                    legacy_addon_component_keys.update(prepaid_excluded_addon_keys)
                    legacy_component_rows.append(
                        {
                            "kind": "base_service",
                            "component_key": "base_service",
                            "gross_amount": str(preview.gross_amount),
                            "currency": preview.currency,
                        }
                    )
            else:
                try:
                    postpaid_preview = preview_postpaid_recurring_charge(
                        db,
                        subscription_id=subscription_id,
                        as_of=command.cutoff_at,
                    )
                except PostpaidChargePreviewError as exc:
                    legacy_error = exc.code
                else:
                    legacy_period_start = postpaid_preview.period_start
                    legacy_period_end = postpaid_preview.period_end
                    legacy_currency = postpaid_preview.currency
                    legacy_gross = postpaid_preview.gross_amount
                    legacy_component_rows = [
                        {
                            "kind": component.kind.value,
                            "component_key": component.component_key,
                            "subscription_add_on_id": (
                                str(component.subscription_add_on_id)
                                if component.subscription_add_on_id is not None
                                else None
                            ),
                            "add_on_id": (
                                str(component.add_on_id)
                                if component.add_on_id is not None
                                else None
                            ),
                            "quantity": str(component.quantity),
                            "unit_price": str(component.unit_price),
                            "net_amount": str(component.net_amount),
                            "tax_amount": str(component.tax_amount),
                            "gross_amount": str(component.gross_amount),
                            "currency": postpaid_preview.currency,
                        }
                        for component in postpaid_preview.components
                    ]
                    legacy_addon_component_keys = {
                        component.component_key
                        for component in postpaid_preview.components
                        if component.kind
                        is RecurringChargeComponentKind.recurring_addon
                    }
                    legacy_preview_issues = [
                        {
                            "kind": issue.kind.value,
                            "subscription_add_on_id": str(issue.subscription_add_on_id),
                            "add_on_id": str(issue.add_on_id),
                        }
                        for issue in postpaid_preview.issues
                    ]
                    legacy_addon_component_keys.update(
                        str(issue.subscription_add_on_id)
                        for issue in postpaid_preview.issues
                    )

            cadence = BillingContracts.cadence_of(version)
            new_cadence = any(
                (
                    version.rate_basis is not RateBasis.fixed_per_service_period,
                    version.service_interval_unit is not version.invoice_interval_unit,
                    version.service_interval_count != version.invoice_interval_count,
                    legacy_error
                    == "financial.prepaid_service_renewals.unsupported_cadence",
                )
            )
            if legacy_error is not None and not new_cadence:
                source_row["legacy_preview_error"] = legacy_error
                _mark(classification, "unresolved", subscription_id)
                detail(subscription_id, legacy_error)
                continue

            candidate_start = legacy_period_start
            if candidate_start is None:
                anchor = subscription.next_billing_at or subscription.start_at
                if anchor is None:
                    _mark(classification, "unresolved", subscription_id)
                    source_row["legacy_preview_error"] = legacy_error
                    detail(subscription_id, legacy_error or "missing_billing_anchor")
                    continue
                candidate_start = _utc(anchor)
            contract_start = _utc(version.starts_at)
            try:
                period_index, target_period = period_containing(
                    cadence=cadence,
                    contract_start=contract_start,
                    moment=_utc(candidate_start),
                )
            except CadenceError as exc:
                source_row["target_period_error"] = exc.code
                _mark(classification, "unresolved", subscription_id)
                detail(subscription_id, exc.code)
                continue

            target_addon_component_keys = {
                line.component_key
                for line in lines
                if line.charge_component is ChargeComponent.addon
            }
            missing_target_addons = sorted(
                legacy_addon_component_keys - target_addon_component_keys
            )
            stale_target_addons = sorted(
                target_addon_component_keys - legacy_addon_component_keys
            )
            if missing_target_addons or stale_target_addons:
                source_row["recurring_addon_identity"] = {
                    "current": sorted(legacy_addon_component_keys),
                    "target": sorted(target_addon_component_keys),
                }
                _mark(classification, "unexpected_unlinked", subscription_id)
                for component_key in missing_target_addons:
                    detail(
                        subscription_id,
                        f"missing_target_recurring_addon:{component_key}",
                    )
                for component_key in stale_target_addons:
                    detail(
                        subscription_id,
                        f"stale_target_recurring_addon:{component_key}",
                    )
                continue
            if legacy_preview_issues:
                source_row["legacy_preview_issues"] = legacy_preview_issues
                _mark(classification, "unresolved", subscription_id)
                for issue in legacy_preview_issues:
                    detail(
                        subscription_id,
                        f"postpaid_recurring_addon_{issue['kind']}",
                    )
                continue
            if prepaid_excluded_addon_keys:
                source_row["prepaid_excluded_recurring_addons"] = sorted(
                    prepaid_excluded_addon_keys
                )
                _mark(classification, "unresolved", subscription_id)
                detail(
                    subscription_id,
                    "current_prepaid_owner_excludes_recurring_addon",
                )
                continue

            target_currency: str | None = None
            target_gross = Decimal("0")
            target_line_rows: list[dict[str, object]] = []
            for line in lines:
                line_obligations = obligations_by_line.get(
                    (version.id, line.contract_line_key),
                    [],
                )
                for category in _topology_categories(line_obligations):
                    _mark(classification, category, subscription_id)
                    detail(subscription_id, f"obligation_{category}")
                matching = [
                    obligation
                    for obligation in line_obligations
                    if _utc(obligation.period_start) == target_period.starts_at
                    and _utc(obligation.period_end) == target_period.ends_at
                ]
                if not matching:
                    _mark(classification, "unexpected_unlinked", subscription_id)
                    detail(
                        subscription_id,
                        f"missing_obligation:{line.contract_line_key}",
                    )
                    continue
                if len(matching) > 1:
                    _mark(classification, "duplicate", subscription_id)
                    detail(
                        subscription_id,
                        f"duplicate_obligation:{line.contract_line_key}",
                    )
                    continue
                obligation = matching[0]
                try:
                    BillingObligations.replay_recorded_rating(obligation)
                except BillingObligationError as exc:
                    source_row["recorded_rating_error"] = exc.code
                    _mark(classification, "unresolved", subscription_id)
                    detail(subscription_id, exc.code)
                    continue
                try:
                    rated = rate_line_period(
                        db,
                        contract_version_id=version.id,
                        contract_line_key=line.contract_line_key,
                        period=target_period,
                    )
                except BillingRatingError as exc:
                    source_row["target_rating_error"] = exc.code
                    _mark(classification, "unresolved", subscription_id)
                    detail(subscription_id, exc.code)
                    continue
                if target_currency is None:
                    target_currency = rated.currency
                elif target_currency != rated.currency:
                    _mark(classification, "ambiguous", subscription_id)
                    detail(subscription_id, "mixed_target_currency")
                    continue
                if any(
                    (
                        obligation.authority is not BillingRecordAuthority.shadow,
                        obligation.net_amount != rated.net_amount,
                        obligation.tax_amount != rated.tax_amount,
                        obligation.gross_amount != rated.gross_amount,
                    )
                ):
                    _mark(classification, "shadow_variance", subscription_id)
                    detail(
                        subscription_id,
                        f"stored_obligation_rating_variance:{obligation.id}",
                    )
                target_gross += rated.gross_amount
                target_line_rows.append(
                    {
                        "line_key": str(line.contract_line_key),
                        "obligation_id": str(obligation.id),
                        "net_amount": str(rated.net_amount),
                        "tax_amount": str(rated.tax_amount),
                        "gross_amount": str(rated.gross_amount),
                        "currency": rated.currency,
                        "rating_input_fingerprint": (
                            obligation.rating_input_fingerprint
                        ),
                    }
                )

            result_rows.append(
                {
                    "subscription_id": str(subscription_id),
                    "version_id": str(version.id),
                    "period_index": period_index,
                    "period_start": target_period.starts_at.isoformat(),
                    "period_end": target_period.ends_at.isoformat(),
                    "target_gross": str(target_gross),
                    "target_currency": target_currency,
                    "legacy_period_start": (
                        legacy_period_start.isoformat()
                        if legacy_period_start is not None
                        else None
                    ),
                    "legacy_period_end": (
                        legacy_period_end.isoformat()
                        if legacy_period_end is not None
                        else None
                    ),
                    "legacy_gross": (
                        str(legacy_gross) if legacy_gross is not None else None
                    ),
                    "legacy_currency": legacy_currency,
                    "legacy_components": legacy_component_rows,
                    "new_cadence": new_cadence,
                    "lines": target_line_rows,
                }
            )
            if target_currency is not None:
                target_totals[target_currency] += target_gross
            if legacy_currency is not None and legacy_gross is not None:
                legacy_totals[legacy_currency] += legacy_gross

            already_blocked = any(
                str(subscription_id) in classification[category]
                for category in blocker_categories
            )
            if already_blocked:
                continue
            if new_cadence:
                _mark(classification, "expected_difference", subscription_id)
                detail(subscription_id, legacy_error or "new_composable_cadence")
                continue
            if (
                legacy_period_start is None
                or legacy_period_end is None
                or legacy_currency is None
                or legacy_gross is None
            ):
                _mark(classification, "unresolved", subscription_id)
                detail(subscription_id, "incomplete_legacy_preview")
                continue
            if any(
                (
                    target_period.starts_at != _utc(legacy_period_start),
                    target_period.ends_at != _utc(legacy_period_end),
                    target_currency != legacy_currency,
                    target_gross != legacy_gross,
                    version.authority is not BillingRecordAuthority.shadow,
                )
            ):
                _mark(classification, "shadow_variance", subscription_id)
                detail(subscription_id, "legacy_target_period_or_amount_variance")
                continue
            _mark(classification, "covered", subscription_id)

        all_currencies = sorted(set(legacy_totals) | set(target_totals))
        currency_totals = {
            currency: {
                "legacy": str(legacy_totals[currency]),
                "target": str(target_totals[currency]),
                "difference": str(target_totals[currency] - legacy_totals[currency]),
            }
            for currency in all_currencies
        }
        run = BillingCutoverVerificationRun(
            phase="phase_2",
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
            expected_difference_count=len(classification["expected_difference"]),
            gap_count=len(classification["gap"]),
            overlap_count=len(classification["overlap"]),
            source_fingerprint=_digest(source_rows),
            result_fingerprint=_digest(result_rows),
            currency_totals=currency_totals,
            cohort_classification={
                **classification,
                "_details": dict(sorted(classification_details.items())),
            },
            event_outcomes={
                "migration_evidence_only": True,
                "authority_moved": False,
                "repair_requested": False,
            },
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
        result = BillingShadowVerification._phase2_result(run, replayed=False)
        emit_event(
            db,
            EventType.billing_cutover_verification_recorded,
            {
                "run_id": str(run.id),
                "phase": run.phase,
                "cohort_count": run.cohort_count,
                "covered_count": run.covered_count,
                "expected_difference_count": run.expected_difference_count,
                "blocker_count": result.blocker_count,
                "source_fingerprint": run.source_fingerprint,
                "result_fingerprint": run.result_fingerprint,
            },
            actor=context.actor,
        )
        return result

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
                    run.gap_count,
                    run.overlap_count,
                )
            ),
            replayed=replayed,
        )

    @staticmethod
    def _phase2_result(
        run: BillingCutoverVerificationRun,
        *,
        replayed: bool,
    ) -> Phase2VerificationResult:
        return Phase2VerificationResult(
            run_id=run.id,
            cohort_count=run.cohort_count,
            covered_count=run.covered_count,
            expected_difference_count=run.expected_difference_count,
            blocker_count=sum(
                (
                    run.unresolved_count,
                    run.ambiguous_count,
                    run.unexpected_unlinked_count,
                    run.duplicate_count,
                    run.shadow_variance_count,
                    run.gap_count,
                    run.overlap_count,
                )
            ),
            replayed=replayed,
        )


__all__ = [
    "BillingShadowVerification",
    "BillingShadowVerificationError",
    "Phase1VerificationResult",
    "Phase2VerificationResult",
    "RecordPhase1VerificationCommand",
    "RecordPhase2VerificationCommand",
]


@dataclass(frozen=True)
class RecordPhase3ForwardVerificationCommand:
    """Exact Phase 3 forward-shadow cohort cutoff and build identity."""

    cutoff_at: datetime
    observation_started_at: datetime
    observation_ended_at: datetime
    code_version: str
    database_schema_version: str
    cohort_name: str = "prepaid_funding_candidates"
    policy_version: str = "adr-0007-phase-3-forward-v1"
    evidence_schema_version: int = 3


@dataclass(frozen=True)
class Phase3ForwardVerificationResult:
    """Durable forward-shadow evidence: debts separated, nothing invented."""

    run_id: UUID
    cohort_count: int
    opening_position_debt_count: int
    entitlement_evidence_debt_count: int
    posting_covered_count: int
    producer_not_owner_wrapped_count: int
    work_item_count: int
    source_fingerprint: str
    result_fingerprint: str
    replayed: bool


_OPENING_DEBT_PREFIX = "prepaid-funding:opening-debt:"
_ENTITLEMENT_DEBT_PREFIX = "prepaid-coverage:entitlement-debt:"
_DEBT_SLA_HOURS = 72


def _phase3_result(run: BillingCutoverVerificationRun, *, replayed: bool):
    classification: dict = dict(run.cohort_classification or {})
    details: dict = dict(classification.get("_details") or {})
    return Phase3ForwardVerificationResult(
        run_id=run.id,
        cohort_count=run.cohort_count,
        opening_position_debt_count=len(details.get("opening_position_debt", [])),
        entitlement_evidence_debt_count=len(
            details.get("entitlement_evidence_debt", {})
        ),
        posting_covered_count=run.covered_count,
        producer_not_owner_wrapped_count=run.unresolved_count,
        work_item_count=int(details.get("work_item_count", 0)),
        source_fingerprint=run.source_fingerprint,
        result_fingerprint=run.result_fingerprint,
        replayed=replayed,
    )


def record_phase3_forward_run(
    db: Session,
    command: RecordPhase3ForwardVerificationCommand,
    *,
    context: CommandContext,
) -> Phase3ForwardVerificationResult:
    """Record the forward-shadow posting-coverage and debt evidence.

    Evidence only: this run never manufactures postings for unwrapped
    producers, never repairs a legacy source, and never moves authority.
    Every debt row is durably classified and carried by an owned work item.
    """

    return execute_owner_command(
        db,
        definition=_PHASE3_FORWARD_RUN_COMMAND,
        context=context,
        operation=lambda: _record_phase3_forward_run(
            db, command=command, context=context
        ),
    )


def _record_phase3_forward_run(
    db: Session,
    *,
    command: RecordPhase3ForwardVerificationCommand,
    context: CommandContext,
) -> Phase3ForwardVerificationResult:
    from app.models.billing import (
        AccountAdjustment,
        Payment,
        PaymentAllocation,
        PaymentRefund,
        PaymentReversal,
        PaymentSettlement,
    )
    from app.models.customer_subledger import CustomerPostingGroup
    from app.models.network_monitoring import AlertSeverity
    from app.models.prepaid_funding import PrepaidOpeningFundingConsumption
    from app.services.observability import Finding, record_finding, resolve_findings
    from app.services.prepaid_enforcement_planner import (
        candidate_prepaid_funding_account_ids,
    )
    from app.services.prepaid_funding_reconstruction import (
        prepaid_funding_incomplete_source_account_ids,
    )
    from app.services.prepaid_threshold import resolve_prepaid_threshold_decisions

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
        if existing.phase != "phase_3_forward":
            raise _error(
                "idempotency_conflict",
                "Verification idempotency key belongs to another phase.",
                run_id=str(existing.id),
            )
        return _phase3_result(existing, replayed=True)

    cohort = sorted(candidate_prepaid_funding_account_ids(db), key=str)
    opening_source_debt = sorted(
        prepaid_funding_incomplete_source_account_ids(db, cohort), key=str
    )
    decisions = resolve_prepaid_threshold_decisions(db, cohort)
    entitlement_debt: dict[str, list[str]] = {}
    for account_id in cohort:
        decision = decisions.get(str(account_id))
        if decision is None:
            continue
        unresolved = [
            str(value) for value in decision.unresolved_projection_subscription_ids
        ]
        if unresolved:
            entitlement_debt[str(account_id)] = sorted(unresolved)

    window_start = command.observation_started_at
    window_end = command.observation_ended_at

    def _facts(model, kind, created_col):
        rows = db.execute(
            select(model.id).where(
                created_col >= window_start, created_col < window_end
            )
        ).scalars()
        return [(kind, row) for row in rows]

    facts: list[tuple[str, UUID]] = []
    facts += [
        ("payment", row)
        for row in db.execute(
            select(PaymentSettlement.payment_id)
            .join(Payment, Payment.id == PaymentSettlement.payment_id)
            .where(
                PaymentSettlement.created_at >= window_start,
                PaymentSettlement.created_at < window_end,
                Payment.account_id.is_not(None),
            )
        ).scalars()
    ]
    facts += _facts(
        PaymentAllocation, "payment_allocation", PaymentAllocation.created_at
    )
    facts += _facts(
        PrepaidOpeningFundingConsumption,
        "prepaid_opening_funding_consumption",
        PrepaidOpeningFundingConsumption.consumed_at,
    )
    facts += _facts(PaymentRefund, "payment_refund", PaymentRefund.created_at)
    facts += _facts(PaymentReversal, "payment_reversal", PaymentReversal.created_at)
    facts += _facts(
        AccountAdjustment, "account_adjustment", AccountAdjustment.created_at
    )

    fact_ids = [fact_id for _, fact_id in facts]
    grouped: set[tuple[str, UUID]] = set()
    if fact_ids:
        for group_kind, group_source_id in db.execute(
            select(
                CustomerPostingGroup.source_kind, CustomerPostingGroup.source_id
            ).where(CustomerPostingGroup.source_id.in_(fact_ids))
        ).all():
            grouped.add((str(group_kind), group_source_id))

    covered = sorted(
        f"{kind}:{fact_id}" for kind, fact_id in facts if (kind, fact_id) in grouped
    )
    unwrapped = sorted(
        f"{kind}:{fact_id}" for kind, fact_id in facts if (kind, fact_id) not in grouped
    )

    work_items = 0
    for account_id in opening_source_debt:
        record_finding(
            db,
            Finding(
                fingerprint=f"{_OPENING_DEBT_PREFIX}{account_id}",
                domain="prepaid_enforcement",
                source="billing.shadow_verification",
                severity=AlertSeverity.warning,
                title="Prepaid opening source batch is incomplete",
                summary=(
                    "The complete-history artifact has not yet materialized "
                    "this account's explicit history seed. Resolve the source "
                    "batch; never invent a per-account fallback."
                ),
                details={
                    "owner": "finance-billing",
                    "account_id": str(account_id),
                    "debt": "opening_position",
                    "sla_due_at": (
                        datetime.now(UTC) + timedelta(hours=_DEBT_SLA_HOURS)
                    ).isoformat(),
                },
            ),
        )
        work_items += 1
    resolve_findings(
        db,
        managed_prefix=_OPENING_DEBT_PREFIX,
        active_fingerprints={
            f"{_OPENING_DEBT_PREFIX}{account_id}" for account_id in opening_source_debt
        },
    )
    for debt_account, subscription_ids in sorted(entitlement_debt.items()):
        record_finding(
            db,
            Finding(
                fingerprint=f"{_ENTITLEMENT_DEBT_PREFIX}{debt_account}",
                domain="prepaid_enforcement",
                source="billing.shadow_verification",
                severity=AlertSeverity.warning,
                title="Prepaid coverage needs obligation/entitlement evidence",
                summary=(
                    "Paid service exists without a resolvable entitlement "
                    "projection: obligation/application evidence debt. Only "
                    "the coverage owner may create or correct entitlements."
                ),
                details={
                    "owner": "finance-billing",
                    "account_id": debt_account,
                    "debt": "entitlement_evidence",
                    "subscription_ids": subscription_ids,
                    "sla_due_at": (
                        datetime.now(UTC) + timedelta(hours=_DEBT_SLA_HOURS)
                    ).isoformat(),
                },
            ),
        )
        work_items += 1
    resolve_findings(
        db,
        managed_prefix=_ENTITLEMENT_DEBT_PREFIX,
        active_fingerprints={
            f"{_ENTITLEMENT_DEBT_PREFIX}{debt_account}"
            for debt_account in entitlement_debt
        },
    )

    classification = {
        "covered": covered,
        "unresolved": unwrapped,
        "ambiguous": [],
        "unexpected_unlinked": [],
        "duplicate": [],
        "shadow_variance": [],
        "expected_difference": sorted(
            {str(a) for a in opening_source_debt} | set(entitlement_debt)
        ),
        "gap": [],
        "overlap": [],
        "_details": {
            "opening_position_debt": [str(a) for a in opening_source_debt],
            "entitlement_evidence_debt": entitlement_debt,
            "producer_not_owner_wrapped": unwrapped,
            "work_item_count": work_items,
        },
    }
    source_rows = {
        "cohort": [str(a) for a in cohort],
        "facts": sorted(f"{kind}:{fact_id}" for kind, fact_id in facts),
        "window": [window_start.isoformat(), window_end.isoformat()],
        "policy_version": command.policy_version,
    }
    run = BillingCutoverVerificationRun(
        phase="phase_3_forward",
        cohort_name=command.cohort_name,
        evidence_schema_version=command.evidence_schema_version,
        policy_version=command.policy_version,
        cutoff_at=command.cutoff_at,
        observation_started_at=command.observation_started_at,
        observation_ended_at=command.observation_ended_at,
        cohort_count=len(cohort),
        covered_count=len(covered),
        unresolved_count=len(unwrapped),
        ambiguous_count=0,
        unexpected_unlinked_count=0,
        duplicate_count=0,
        shadow_variance_count=0,
        expected_difference_count=len(classification["expected_difference"]),
        gap_count=0,
        overlap_count=0,
        source_fingerprint=_digest(source_rows),
        result_fingerprint=_digest(classification),
        currency_totals={},
        cohort_classification=classification,
        event_outcomes={
            "migration_evidence_only": True,
            "authority_moved": False,
            "repair_requested": False,
            "postings_manufactured": False,
        },
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
            "expected_difference_count": run.expected_difference_count,
            "blocker_count": run.unresolved_count,
            "source_fingerprint": run.source_fingerprint,
            "result_fingerprint": run.result_fingerprint,
        },
        actor=context.actor,
    )
    return _phase3_result(run, replayed=False)


@dataclass(frozen=True)
class RecordPhase3OpeningPreviewCommand:
    """Exact cohort/currency snapshot proposed for reviewed opening capture."""

    cutoff_at: datetime
    code_version: str
    database_schema_version: str
    currency: str = "NGN"
    cohort_name: str = "prepaid_funding_candidates"
    policy_version: str = "adr-0007-phase-3-opening-v2-complete-history"
    evidence_schema_version: int = 5


@dataclass(frozen=True)
class Phase3OpeningPreviewResult:
    """Durable finance-review surface for exact opening residuals."""

    run_id: UUID
    cohort_count: int
    capture_eligible_count: int
    quarantined_count: int
    nonzero_opening_count: int
    source_fingerprint: str
    result_fingerprint: str
    replayed: bool


def _phase3_opening_result(
    run: BillingCutoverVerificationRun, *, replayed: bool
) -> Phase3OpeningPreviewResult:
    details = _object_dict((run.cohort_classification or {}).get("_details"))
    opening_rows = _object_dict_rows(details.get("opening_rows"))
    quarantined_accounts = details.get("quarantined_accounts")
    return Phase3OpeningPreviewResult(
        run_id=run.id,
        cohort_count=run.cohort_count,
        capture_eligible_count=len(opening_rows),
        quarantined_count=(
            len(quarantined_accounts) if isinstance(quarantined_accounts, list) else 0
        ),
        nonzero_opening_count=sum(
            Decimal(str(row["opening_delta"])) != Decimal("0") for row in opening_rows
        ),
        source_fingerprint=run.source_fingerprint,
        result_fingerprint=run.result_fingerprint,
        replayed=replayed,
    )


def record_phase3_opening_preview(
    db: Session,
    command: RecordPhase3OpeningPreviewCommand,
    *,
    context: CommandContext,
) -> Phase3OpeningPreviewResult:
    """Persist an immutable reviewed-opening proposal without writing money."""

    return execute_owner_command(
        db,
        definition=_PHASE3_OPENING_PREVIEW_COMMAND,
        context=context,
        operation=lambda: _record_phase3_opening_preview(
            db, command=command, context=context
        ),
    )


def _record_phase3_opening_preview(
    db: Session,
    *,
    command: RecordPhase3OpeningPreviewCommand,
    context: CommandContext,
) -> Phase3OpeningPreviewResult:
    from app.models.customer_subledger import CustomerSubledgerOpeningPosition
    from app.models.prepaid_funding import PrepaidFundingBaseline
    from app.services.billing.customer_subledger import resolve_positions
    from app.services.prepaid_enforcement_planner import (
        candidate_prepaid_funding_account_ids,
    )
    from app.services.prepaid_funding_reconstruction import (
        prepaid_funding_opening_required_account_ids,
        prepaid_funding_opening_source_incomplete_account_ids,
        preview_prepaid_opening_targets,
    )

    if not context.idempotency_key:
        raise _error(
            "missing_idempotency_key",
            "An opening-position preview requires an idempotency key.",
        )
    if command.cutoff_at.tzinfo is None:
        raise _error(
            "invalid_observation_window",
            "Opening-position cutoff must be timezone-aware.",
        )
    currency = command.currency.strip().upper()
    if len(currency) != 3 or not currency.isalpha():
        raise _error(
            "invalid_run_identity",
            "Opening-position currency must be a three-letter code.",
        )
    for field, value in (
        ("code_version", command.code_version),
        ("database_schema_version", command.database_schema_version),
        ("cohort_name", command.cohort_name),
        ("policy_version", command.policy_version),
    ):
        if not value.strip():
            raise _error(
                "invalid_run_identity",
                "Opening-position run identity fields cannot be empty.",
                field=field,
            )
    existing = db.scalar(
        select(BillingCutoverVerificationRun).where(
            BillingCutoverVerificationRun.idempotency_key == context.idempotency_key
        )
    )
    if existing is not None:
        if (
            existing.phase != "phase_3_opening_preview"
            or _utc(existing.cutoff_at) != _utc(command.cutoff_at)
            or existing.code_version != command.code_version
            or existing.database_schema_version != command.database_schema_version
            or existing.policy_version != command.policy_version
            or existing.cohort_name != command.cohort_name
            or str((existing.currency_totals or {}).get("currency")) != currency
        ):
            raise _error(
                "idempotency_conflict",
                "Opening preview idempotency key belongs to different evidence.",
                run_id=str(existing.id),
            )
        return _phase3_opening_result(existing, replayed=True)

    candidate_cohort = candidate_prepaid_funding_account_ids(db)
    cohort = tuple(
        sorted(
            prepaid_funding_opening_required_account_ids(db, candidate_cohort),
            key=str,
        )
    )
    incomplete_source = tuple(
        sorted(
            prepaid_funding_opening_source_incomplete_account_ids(
                db, cohort, currency=currency
            ),
            key=str,
        )
    )
    if incomplete_source:
        raise _error(
            "source_cohort_incomplete",
            (
                "Every migrated funding candidate requires reviewed history evidence; "
                "native-after-handoff accounts require exact native provenance."
            ),
            account_ids=[str(value) for value in incomplete_source],
        )
    existing_openings = {
        row.account_id: row
        for row in db.scalars(
            select(CustomerSubledgerOpeningPosition).where(
                CustomerSubledgerOpeningPosition.account_id.in_(cohort),
                CustomerSubledgerOpeningPosition.currency == currency,
            )
        ).all()
    }
    capture_ids = tuple(
        account_id for account_id in cohort if account_id not in existing_openings
    )

    opening_targets = preview_prepaid_opening_targets(
        db,
        capture_ids,
        currency=currency,
    )
    shadow = resolve_positions(
        db,
        account_ids=capture_ids,
        currency=currency,
        authority=BillingRecordAuthority.shadow,
    )
    baselines = {
        row.account_id: row
        for row in db.scalars(
            select(PrepaidFundingBaseline).where(
                PrepaidFundingBaseline.account_id.in_(capture_ids),
                PrepaidFundingBaseline.currency == currency,
                PrepaidFundingBaseline.is_active.is_(True),
            )
        ).all()
    }

    source_rows: list[dict[str, object]] = []
    result_rows: list[dict[str, object]] = []
    legacy_total = Decimal("0")
    shadow_total = Decimal("0")
    opening_total = Decimal("0")
    opening_positive = Decimal("0")
    opening_negative = Decimal("0")
    for account_id in capture_ids:
        baseline = baselines.get(account_id)
        position = shadow[account_id]
        shadow_value = round_money(
            position.unapplied_customer_credit + position.prepaid_funding_reserved
        )
        target = opening_targets[account_id]
        legacy_value = round_money(target.amount)
        delta = round_money(legacy_value - shadow_value)
        source: dict[str, object] = {
            "account_id": str(account_id),
            "currency": currency,
            "baseline_id": str(baseline.id) if baseline is not None else None,
            "baseline_amount": (
                str(baseline.amount) if baseline is not None else "0.00"
            ),
            "baseline_position_at": (
                _utc(baseline.position_at).isoformat() if baseline is not None else None
            ),
            "opening_target_origin": target.origin.value,
            "opening_target_source_position_at": (
                _utc(target.source_position_at).isoformat()
            ),
            "legacy_position": str(legacy_value),
            "shadow_lanes": {
                "unapplied_customer_credit": str(position.unapplied_customer_credit),
                "prepaid_funding_reserved": str(position.prepaid_funding_reserved),
                "prepaid_funding_consumed": str(position.prepaid_funding_consumed),
                "refunded_total": str(position.refunded_total),
                "adjustment_total": str(position.adjustment_total),
            },
        }
        row: dict[str, object] = {
            **source,
            "shadow_position_before": str(shadow_value),
            "opening_delta": str(delta),
            "evidence_fingerprint": _digest(source),
        }
        source_rows.append(source)
        result_rows.append(row)
        legacy_total += legacy_value
        shadow_total += shadow_value
        opening_total += delta
        if delta > 0:
            opening_positive += delta
        elif delta < 0:
            opening_negative += abs(delta)

    existing_evidence = [
        {
            "account_id": str(account_id),
            "currency": currency,
            "opening_id": str(opening.id),
            "legacy_position": str(opening.legacy_position),
            "occurred_at": _utc(opening.occurred_at).isoformat(),
            "evidence_fingerprint": opening.evidence_fingerprint,
        }
        for account_id, opening in sorted(
            existing_openings.items(), key=lambda item: str(item[0])
        )
    ]
    expected_differences = [
        str(row["account_id"])
        for row in result_rows
        if Decimal(str(row["opening_delta"])) != Decimal("0")
    ]
    result_contract = {
        "cohort": [str(account_id) for account_id in cohort],
        "existing_openings": existing_evidence,
        "opening_rows": result_rows,
    }
    classification = {
        "covered": [str(account_id) for account_id in cohort],
        "unresolved": [],
        "ambiguous": [],
        "unexpected_unlinked": [],
        "duplicate": [],
        "shadow_variance": [],
        "expected_difference": expected_differences,
        "gap": [],
        "overlap": [],
        "_details": {
            "opening_rows": result_rows,
            "opening_result_contract": result_contract,
            "existing_openings": existing_evidence,
            "post_cutover_native_accounts": sorted(
                str(account_id) for account_id in candidate_cohort - set(cohort)
            ),
            "quarantined_accounts": [],
            "postings_manufactured": False,
            "authority_moved": False,
        },
    }
    run = BillingCutoverVerificationRun(
        phase="phase_3_opening_preview",
        cohort_name=command.cohort_name,
        evidence_schema_version=command.evidence_schema_version,
        policy_version=command.policy_version,
        cutoff_at=_utc(command.cutoff_at),
        observation_started_at=_utc(command.cutoff_at),
        observation_ended_at=_utc(command.cutoff_at),
        cohort_count=len(cohort),
        covered_count=len(cohort),
        unresolved_count=0,
        ambiguous_count=0,
        unexpected_unlinked_count=0,
        duplicate_count=0,
        shadow_variance_count=0,
        expected_difference_count=len(expected_differences),
        gap_count=0,
        overlap_count=0,
        source_fingerprint=_digest(
            {
                "cohort": [str(account_id) for account_id in cohort],
                "existing_openings": existing_evidence,
                "new_opening_sources": source_rows,
            }
        ),
        result_fingerprint=_digest(result_contract),
        currency_totals={
            "currency": currency,
            "legacy_position": str(round_money(legacy_total)),
            "shadow_position_before": str(round_money(shadow_total)),
            "opening_delta": str(round_money(opening_total)),
            "opening_positive": str(round_money(opening_positive)),
            "opening_negative": str(round_money(opening_negative)),
        },
        cohort_classification=classification,
        event_outcomes={
            "migration_evidence_only": True,
            "authority_moved": False,
            "repair_requested": False,
            "postings_manufactured": False,
        },
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
            "capture_eligible_count": len(capture_ids),
            "existing_opening_count": len(existing_openings),
            "quarantined_count": 0,
            "source_fingerprint": run.source_fingerprint,
            "result_fingerprint": run.result_fingerprint,
            "authority_moved": False,
            "postings_manufactured": False,
        },
        actor=context.actor,
    )
    return _phase3_opening_result(run, replayed=False)


__all__ += [
    "Phase3ForwardVerificationResult",
    "Phase3OpeningPreviewResult",
    "RecordPhase3ForwardVerificationCommand",
    "RecordPhase3OpeningPreviewCommand",
    "record_phase3_forward_run",
    "record_phase3_opening_preview",
]


@dataclass(frozen=True)
class RecordPhase3SubledgerParityCommand:
    """Post-opening position parity and forward-coverage observation window."""

    cutoff_at: datetime
    observation_started_at: datetime
    observation_ended_at: datetime
    code_version: str
    database_schema_version: str
    currency: str = "NGN"
    cohort_name: str = "prepaid_funding_candidates"
    policy_version: str = "adr-0007-phase-3-parity-v2-complete-cohort"
    evidence_schema_version: int = 6


@dataclass(frozen=True)
class Phase3SubledgerParityResult:
    run_id: UUID
    cohort_count: int
    parity_count: int
    quarantined_count: int
    variance_count: int
    unwrapped_fact_count: int
    blocker_count: int
    source_fingerprint: str
    result_fingerprint: str
    replayed: bool


def _phase3_parity_result(
    run: BillingCutoverVerificationRun, *, replayed: bool
) -> Phase3SubledgerParityResult:
    details = _object_dict((run.cohort_classification or {}).get("_details"))
    quarantined_accounts = details.get("quarantined_accounts")
    return Phase3SubledgerParityResult(
        run_id=run.id,
        cohort_count=run.cohort_count,
        parity_count=run.covered_count,
        quarantined_count=(
            len(quarantined_accounts) if isinstance(quarantined_accounts, list) else 0
        ),
        variance_count=run.shadow_variance_count,
        unwrapped_fact_count=run.unresolved_count,
        blocker_count=sum(
            (
                run.unresolved_count,
                run.ambiguous_count,
                run.unexpected_unlinked_count,
                run.duplicate_count,
                run.shadow_variance_count,
                run.gap_count,
                run.overlap_count,
            )
        ),
        source_fingerprint=run.source_fingerprint,
        result_fingerprint=run.result_fingerprint,
        replayed=replayed,
    )


def record_phase3_subledger_parity(
    db: Session,
    command: RecordPhase3SubledgerParityCommand,
    *,
    context: CommandContext,
) -> Phase3SubledgerParityResult:
    """Record the exact post-opening cutover gate; never repair or cut over."""

    return execute_owner_command(
        db,
        definition=_PHASE3_PARITY_COMMAND,
        context=context,
        operation=lambda: _record_phase3_subledger_parity(
            db, command=command, context=context
        ),
    )


def _record_phase3_subledger_parity(
    db: Session,
    *,
    command: RecordPhase3SubledgerParityCommand,
    context: CommandContext,
) -> Phase3SubledgerParityResult:
    from collections import Counter

    from app.models.billing import (
        AccountAdjustment,
        Payment,
        PaymentAllocation,
        PaymentRefund,
        PaymentReversal,
        PaymentSettlement,
    )
    from app.models.customer_subledger import (
        CustomerPostingGroup,
        CustomerSubledgerOpeningPosition,
    )
    from app.models.prepaid_funding import PrepaidOpeningFundingConsumption
    from app.services.billing.customer_subledger import resolve_positions
    from app.services.prepaid_enforcement_planner import (
        candidate_prepaid_funding_account_ids,
    )
    from app.services.prepaid_funding_reconstruction import (
        prepaid_funding_incomplete_source_account_ids,
        prepaid_funding_opening_required_account_ids,
        verified_prepaid_funding_balances,
    )

    if not context.idempotency_key:
        raise _error(
            "missing_idempotency_key",
            "A subledger parity run requires an idempotency key.",
        )
    for field, value in (
        ("cutoff_at", command.cutoff_at),
        ("observation_started_at", command.observation_started_at),
        ("observation_ended_at", command.observation_ended_at),
    ):
        if value.tzinfo is None:
            raise _error(
                "invalid_observation_window",
                "Subledger parity timestamps must be timezone-aware.",
                field=field,
            )
    if not (
        command.observation_started_at
        <= command.observation_ended_at
        <= command.cutoff_at
    ):
        raise _error(
            "invalid_observation_window",
            "Parity observation window must end at or before cutoff.",
        )
    currency = command.currency.strip().upper()
    if len(currency) != 3 or not currency.isalpha():
        raise _error(
            "invalid_run_identity",
            "Subledger parity currency must be a three-letter code.",
        )
    existing = db.scalar(
        select(BillingCutoverVerificationRun).where(
            BillingCutoverVerificationRun.idempotency_key == context.idempotency_key
        )
    )
    if existing is not None:
        if (
            existing.phase != "phase_3_subledger_parity"
            or _utc(existing.cutoff_at) != _utc(command.cutoff_at)
            or _utc(existing.observation_started_at)
            != _utc(command.observation_started_at)
            or _utc(existing.observation_ended_at) != _utc(command.observation_ended_at)
            or existing.code_version != command.code_version
            or existing.database_schema_version != command.database_schema_version
            or str((existing.currency_totals or {}).get("currency")) != currency
        ):
            raise _error(
                "idempotency_conflict",
                "Parity idempotency key belongs to different evidence.",
                run_id=str(existing.id),
            )
        return _phase3_parity_result(existing, replayed=True)

    cohort = tuple(
        sorted(
            prepaid_funding_opening_required_account_ids(
                db, candidate_prepaid_funding_account_ids(db)
            ),
            key=str,
        )
    )
    incomplete_source = tuple(
        sorted(
            prepaid_funding_incomplete_source_account_ids(
                db, cohort, currency=currency
            ),
            key=str,
        )
    )
    if incomplete_source:
        raise _error(
            "source_cohort_incomplete",
            "Subledger parity requires a history-derived baseline for every candidate.",
            account_ids=[str(value) for value in incomplete_source],
        )
    eligible = cohort
    openings = list(
        db.scalars(
            select(CustomerSubledgerOpeningPosition).where(
                CustomerSubledgerOpeningPosition.account_id.in_(eligible),
                CustomerSubledgerOpeningPosition.currency == currency,
            )
        ).all()
    )
    opening_counts = Counter(row.account_id for row in openings)
    missing_opening = sorted(
        [account_id for account_id in eligible if opening_counts[account_id] == 0],
        key=str,
    )
    duplicate_opening = sorted(
        [account_id for account_id, count in opening_counts.items() if count > 1],
        key=str,
    )
    legacy = verified_prepaid_funding_balances(db, eligible, currency=currency)
    shadow = resolve_positions(
        db,
        account_ids=eligible,
        currency=currency,
        authority=BillingRecordAuthority.shadow,
    )
    position_rows: list[dict[str, object]] = []
    parity: list[str] = []
    variances: list[str] = []
    legacy_total = Decimal("0")
    shadow_total = Decimal("0")
    for account_id in eligible:
        position = shadow[account_id]
        target_total = round_money(
            position.unapplied_customer_credit + position.prepaid_funding_reserved
        )
        legacy_total_value = round_money(legacy[account_id])
        variance = round_money(target_total - legacy_total_value)
        row: dict[str, object] = {
            "account_id": str(account_id),
            "currency": currency,
            "legacy_customer_funding": str(legacy_total_value),
            "shadow_customer_funding": str(target_total),
            "variance": str(variance),
            "lanes": {
                "unapplied_customer_credit": str(position.unapplied_customer_credit),
                "prepaid_funding_reserved": str(position.prepaid_funding_reserved),
                "prepaid_funding_consumed": str(position.prepaid_funding_consumed),
                "refunded_total": str(position.refunded_total),
                "adjustment_total": str(position.adjustment_total),
            },
        }
        position_rows.append(row)
        if variance == Decimal("0"):
            parity.append(str(account_id))
        else:
            variances.append(str(account_id))
        legacy_total += legacy_total_value
        shadow_total += target_total

    window_start = _utc(command.observation_started_at)
    window_end = _utc(command.observation_ended_at)

    def _facts(model, kind: str, created_col):
        return [
            (kind, row)
            for row in db.scalars(
                select(model.id).where(
                    created_col >= window_start, created_col < window_end
                )
            ).all()
        ]

    facts: list[tuple[str, UUID]] = [
        ("payment", row)
        for row in db.scalars(
            select(PaymentSettlement.payment_id)
            .join(Payment, Payment.id == PaymentSettlement.payment_id)
            .where(
                PaymentSettlement.created_at >= window_start,
                PaymentSettlement.created_at < window_end,
                Payment.account_id.is_not(None),
            )
        ).all()
    ]
    facts += _facts(
        PaymentAllocation, "payment_allocation", PaymentAllocation.created_at
    )
    facts += _facts(
        PrepaidOpeningFundingConsumption,
        "prepaid_opening_funding_consumption",
        PrepaidOpeningFundingConsumption.consumed_at,
    )
    facts += _facts(PaymentRefund, "payment_refund", PaymentRefund.created_at)
    facts += _facts(PaymentReversal, "payment_reversal", PaymentReversal.created_at)
    facts += _facts(
        AccountAdjustment, "account_adjustment", AccountAdjustment.created_at
    )
    fact_ids = [fact_id for _, fact_id in facts]
    group_rows = (
        list(
            db.execute(
                select(
                    CustomerPostingGroup.source_kind,
                    CustomerPostingGroup.source_id,
                ).where(CustomerPostingGroup.source_id.in_(fact_ids))
            ).all()
        )
        if fact_ids
        else []
    )
    group_counts = Counter((str(kind), source_id) for kind, source_id in group_rows)
    covered_facts = sorted(
        f"{kind}:{fact_id}"
        for kind, fact_id in facts
        if group_counts[(kind, fact_id)] == 1
    )
    unwrapped_facts = sorted(
        f"{kind}:{fact_id}"
        for kind, fact_id in facts
        if group_counts[(kind, fact_id)] == 0
    )
    duplicate_facts = sorted(
        f"{kind}:{fact_id}"
        for kind, fact_id in facts
        if group_counts[(kind, fact_id)] > 1
    )

    classification = {
        "covered": parity,
        "unresolved": unwrapped_facts,
        "ambiguous": [],
        "unexpected_unlinked": [str(value) for value in missing_opening],
        "duplicate": [str(value) for value in duplicate_opening] + duplicate_facts,
        "shadow_variance": variances,
        "expected_difference": [],
        "gap": [],
        "overlap": [],
        "_details": {
            "position_rows": position_rows,
            "quarantined_accounts": [],
            "covered_facts": covered_facts,
            "producer_not_owner_wrapped": unwrapped_facts,
            "duplicate_fact_postings": duplicate_facts,
            "authority_moved": False,
            "repair_requested": False,
        },
    }
    source_rows = {
        "cohort": [str(value) for value in cohort],
        "opening_ids": sorted(str(row.id) for row in openings),
        "facts": sorted(f"{kind}:{fact_id}" for kind, fact_id in facts),
        "window": [window_start.isoformat(), window_end.isoformat()],
        "currency": currency,
    }
    run = BillingCutoverVerificationRun(
        phase="phase_3_subledger_parity",
        cohort_name=command.cohort_name,
        evidence_schema_version=command.evidence_schema_version,
        policy_version=command.policy_version,
        cutoff_at=_utc(command.cutoff_at),
        observation_started_at=window_start,
        observation_ended_at=window_end,
        cohort_count=len(cohort),
        covered_count=len(parity),
        unresolved_count=len(unwrapped_facts),
        ambiguous_count=0,
        unexpected_unlinked_count=len(missing_opening),
        duplicate_count=len(duplicate_opening) + len(duplicate_facts),
        shadow_variance_count=len(variances),
        expected_difference_count=0,
        gap_count=0,
        overlap_count=0,
        source_fingerprint=_digest(source_rows),
        result_fingerprint=_digest(classification),
        currency_totals={
            "currency": currency,
            "legacy_customer_funding": str(round_money(legacy_total)),
            "shadow_customer_funding": str(round_money(shadow_total)),
            "variance": str(round_money(shadow_total - legacy_total)),
        },
        cohort_classification=classification,
        event_outcomes={
            "migration_evidence_only": True,
            "authority_moved": False,
            "repair_requested": False,
            "postings_manufactured": False,
            "facts_covered": len(covered_facts),
            "facts_unwrapped": len(unwrapped_facts),
        },
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
            "parity_count": len(parity),
            "quarantined_count": 0,
            "variance_count": len(variances),
            "unwrapped_fact_count": len(unwrapped_facts),
            "source_fingerprint": run.source_fingerprint,
            "result_fingerprint": run.result_fingerprint,
            "authority_moved": False,
        },
        actor=context.actor,
    )
    return _phase3_parity_result(run, replayed=False)


__all__ += [
    "Phase3SubledgerParityResult",
    "RecordPhase3SubledgerParityCommand",
    "record_phase3_subledger_parity",
]
