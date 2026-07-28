"""`billing.contracts` — versioned customer billing terms (ADR 0007 Phase 1).

This owner turns an accepted commercial commitment, or a later authorized
service change, into one immutable :class:`BillingContractVersion` and its
lines. It is the single place that decides what a customer contracted to pay,
replacing billing mode, cadence, and price duplicated across account,
subscription, and catalog rows.

Phase 1 is expand-and-shadow. While the registry declares this owner's
migration state as ``shadowing``, every row it writes is
``BillingRecordAuthority.shadow`` and must produce no financial effect. The
authority flag is not a feature toggle: it is read from the executable manifest
in ``app/services/sot_relationships.py``, so promoting rows to authoritative
requires the registry change that records the passed cutover gate.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.billing_contract import (
    AccountingTreatment,
    BillingContract,
    BillingContractLine,
    BillingContractSourceKind,
    BillingContractVersion,
    BillingContractVersionStatus,
    BillingRecordAuthority,
    CadenceAlignment,
    ChargeComponent,
    CollectionTiming,
    EndOfMonthRule,
    IntervalUnit,
    ProrationPolicy,
    RateBasis,
)
from app.models.catalog import BillingCycle, BillingMode
from app.services.billing.cadence import BillingCadence
from app.services.domain_errors import DomainError
from app.services.events.owner_outputs import (
    OwnerOutputEnvelope,
    consume_owner_output,
    stage_owner_output,
)
from app.services.events.types import EventType
from app.services.locking import lock_for_update
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)
from app.services.sot_manifest import AuthorityMigrationState

OWNER = "billing.contracts"

_RECORD_VERSION_COMMAND = OwnerCommandDefinition(
    owner=OWNER,
    concern="versioned billing contract terms",
    name="record_billing_contract_version",
)
_SUPERSEDE_COMMAND = OwnerCommandDefinition(
    owner=OWNER,
    concern="billing contract version supersession",
    name="supersede_billing_contract_version",
)
_CONSUME_SALES_FUNDING_COMMAND = OwnerCommandDefinition(
    owner=OWNER,
    concern="versioned billing contract terms",
    name="consume_sales_funding_contracts",
)


class BillingContractError(DomainError):
    """Fail-closed billing-contract error."""


def _error(suffix: str, message: str, **details: object) -> BillingContractError:
    return BillingContractError(
        code=f"{OWNER}.{suffix}", message=message, details=dict(details)
    )


def _aware_utc(value: datetime | None) -> datetime | None:
    """Restore UTC tzinfo on instants read back from persistence.

    SQLite drops timezone metadata in tests; production PostgreSQL preserves
    the UTC offset the owner wrote.
    """

    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


def permitted_authority() -> BillingRecordAuthority:
    """Return the authority this owner may write, from the manifest state.

    Reading the declared migration state keeps the shadow/cutover boundary in
    the executable registry rather than in an ad-hoc readiness flag.
    """

    from app.services.sot_relationships import service_relationship

    contract = service_relationship(OWNER).contract
    if contract is None:  # pragma: no cover - manifest guarantees the contract
        raise _error(
            "command_contract_violation",
            "Billing contract owner has no typed manifest contract.",
        )
    if contract.migration.state in {
        AuthorityMigrationState.CUT_OVER,
        AuthorityMigrationState.COMPLETE,
    }:
        return BillingRecordAuthority.authoritative
    return BillingRecordAuthority.shadow


@dataclass(frozen=True)
class ContractLineInput:
    """One charge component of a contracted version."""

    charge_component: ChargeComponent
    description: str
    unit_price: Decimal
    currency: str
    accounting_treatment: AccountingTreatment
    quantity: Decimal = Decimal("1")
    component_key: str = ""
    is_finite: bool = False
    tax_treatment_code: str | None = None
    contract_line_key: UUID | None = None


@dataclass(frozen=True)
class RecordContractVersionCommand:
    """Typed request to record one immutable set of contracted terms."""

    account_id: UUID
    subscription_id: UUID
    source_kind: BillingContractSourceKind
    source_id: UUID
    starts_at: datetime
    contracted_price: Decimal
    currency: str
    cadence: BillingCadence
    lines: tuple[ContractLineInput, ...]
    source_version: int = 1
    ends_at: datetime | None = None
    payment_terms_days: int = 0
    tax_treatment_code: str | None = None
    tax_inclusive: bool = False
    discount_code: str | None = None
    discount_amount: Decimal | None = None


@dataclass(frozen=True)
class ContractVersionResult:
    """Outcome of recording a version, including replay detection."""

    contract_id: UUID
    version_id: UUID
    version: int
    authority: BillingRecordAuthority
    line_ids: tuple[UUID, ...]
    replayed: bool


@dataclass(frozen=True)
class SalesFundingContractSnapshot:
    """Exact legacy sale/subscription terms carried by the fulfilment output."""

    sales_order_line_id: UUID
    account_id: UUID
    subscription_id: UUID
    starts_at: datetime
    description: str
    quantity: Decimal
    unit_price: Decimal
    currency: str
    billing_cycle: BillingCycle
    billing_mode: BillingMode


_CYCLE_INTERVAL: dict[BillingCycle, tuple[IntervalUnit, int]] = {
    BillingCycle.daily: (IntervalUnit.day, 1),
    BillingCycle.weekly: (IntervalUnit.week, 1),
    BillingCycle.monthly: (IntervalUnit.month, 1),
    BillingCycle.quarterly: (IntervalUnit.month, 3),
    BillingCycle.annual: (IntervalUnit.year, 1),
}


def _sales_funding_command(
    snapshot: SalesFundingContractSnapshot,
) -> RecordContractVersionCommand:
    interval_unit, interval_count = _CYCLE_INTERVAL[snapshot.billing_cycle]
    currency = snapshot.currency.strip().upper()
    prepaid = snapshot.billing_mode is BillingMode.prepaid
    cadence = BillingCadence(
        rate_basis=RateBasis.fixed_per_service_period,
        rate_unit=interval_unit,
        rate_quantity=Decimal("1"),
        service_interval_unit=interval_unit,
        service_interval_count=interval_count,
        invoice_interval_unit=interval_unit,
        invoice_interval_count=interval_count,
        collection_timing=(
            CollectionTiming.advance if prepaid else CollectionTiming.arrears
        ),
        alignment=CadenceAlignment.contract_anniversary,
        timezone_name="Africa/Lagos",
        end_of_month_rule=EndOfMonthRule.clamp_to_month_end,
        proration_policy=ProrationPolicy.none,
    )
    return RecordContractVersionCommand(
        account_id=snapshot.account_id,
        subscription_id=snapshot.subscription_id,
        source_kind=BillingContractSourceKind.sales_order_line,
        source_id=snapshot.sales_order_line_id,
        starts_at=snapshot.starts_at,
        contracted_price=snapshot.unit_price,
        currency=currency,
        cadence=cadence,
        lines=(
            ContractLineInput(
                charge_component=ChargeComponent.recurring_service,
                component_key=str(snapshot.sales_order_line_id),
                description=snapshot.description,
                quantity=snapshot.quantity,
                unit_price=snapshot.unit_price,
                currency=currency,
                accounting_treatment=(
                    AccountingTreatment.prepaid_consumption
                    if prepaid
                    else AccountingTreatment.receivable
                ),
            ),
        ),
    )


def _validate(command: RecordContractVersionCommand) -> None:
    if command.starts_at.tzinfo is None:
        raise _error(
            "invalid_contract_terms",
            "Contract start must be timezone-aware.",
        )
    if command.ends_at is not None and command.ends_at <= command.starts_at:
        raise _error(
            "invalid_contract_terms",
            "Contract version must end after it starts.",
        )
    if command.contracted_price < 0:
        raise _error(
            "invalid_contract_terms",
            "Contracted price cannot be negative.",
        )
    if len(command.currency) != 3:
        raise _error(
            "invalid_contract_terms",
            "Currency must be a three-letter code.",
            currency=command.currency,
        )
    if not command.lines:
        raise _error(
            "invalid_contract_terms",
            "A contract version requires at least one line.",
        )
    for line in command.lines:
        if line.currency != command.currency:
            raise _error(
                "mixed_currency_contract",
                "Every contract line must use the contract currency.",
                line_currency=line.currency,
                contract_currency=command.currency,
            )
        if line.unit_price < 0 or line.quantity <= 0:
            raise _error(
                "invalid_contract_terms",
                "Line unit price must be non-negative and quantity positive.",
            )
    seen = {(line.charge_component, line.component_key) for line in command.lines}
    if len(seen) != len(command.lines):
        raise _error(
            "duplicate_contract_line",
            "Two lines share the same charge component and component key.",
        )


class BillingContracts:
    """Public command owner for versioned billing contract terms."""

    @staticmethod
    def record_version(
        db: Session,
        command: RecordContractVersionCommand,
        *,
        context: CommandContext,
    ) -> ContractVersionResult:
        """Record one immutable contract version in one owner transaction."""

        return execute_owner_command(
            db,
            definition=_RECORD_VERSION_COMMAND,
            context=context,
            operation=lambda: BillingContracts._record_version(
                db, command=command, context=context
            ),
        )

    @staticmethod
    def consume_sales_funding(
        db: Session,
        *,
        sales_order_id: UUID,
        snapshots: tuple[SalesFundingContractSnapshot, ...],
        event_id: UUID,
        context: CommandContext,
    ) -> tuple[ContractVersionResult, ...] | None:
        """Receipt fulfilment terms and emit the next shadow-pipeline output."""

        def _effect() -> tuple[ContractVersionResult, ...]:
            subscription_ids = [snapshot.subscription_id for snapshot in snapshots]
            if len(subscription_ids) != len(set(subscription_ids)):
                raise _error(
                    "duplicate_subscription_output",
                    "One fulfilment output repeats a subscription contract.",
                    sales_order_id=str(sales_order_id),
                )
            results = tuple(
                BillingContracts._record_version(
                    db,
                    command=_sales_funding_command(snapshot),
                    context=context,
                )
                for snapshot in snapshots
            )
            obligation_inputs: list[dict[str, object]] = []
            for result in results:
                lines = db.execute(
                    select(BillingContractLine).where(
                        BillingContractLine.id.in_(result.line_ids)
                    )
                ).scalars()
                for line in lines:
                    obligation_inputs.append(
                        {
                            "contract_version_id": str(result.version_id),
                            "contract_line_key": str(line.contract_line_key),
                            "period_index": 0,
                            "net_amount": str(line.quantity * line.unit_price),
                            "tax_amount": "0",
                        }
                    )
            stage_owner_output(
                db,
                OwnerOutputEnvelope(
                    event_type=EventType.custom,
                    producer_owner=OWNER,
                    source_kind="sales_order",
                    source_id=sales_order_id,
                ),
                {
                    "output": "billing.contracts.shadow_recorded",
                    "sales_order_id": str(sales_order_id),
                    "contracts": [
                        {
                            "contract_id": str(result.contract_id),
                            "contract_version_id": str(result.version_id),
                            "authority": result.authority.value,
                        }
                        for result in results
                    ],
                    "obligations": obligation_inputs,
                },
                context=context,
            )
            return results

        return execute_owner_command(
            db,
            definition=_CONSUME_SALES_FUNDING_COMMAND,
            context=context,
            operation=lambda: consume_owner_output(
                db,
                consumer=OWNER,
                event_id=event_id,
                event_type="sales.fulfillment.funding_applied",
                producer_owner="sales.fulfillment",
                context=context,
                operation=_effect,
            )[0],
        )

    @staticmethod
    def _record_version(
        db: Session,
        *,
        command: RecordContractVersionCommand,
        context: CommandContext,
    ) -> ContractVersionResult:
        _validate(command)
        if not context.idempotency_key:
            raise _error(
                "missing_idempotency_key",
                "Recording contract terms requires a business idempotency key.",
            )

        authority = permitted_authority()
        contract = BillingContracts._ensure_contract(
            db,
            account_id=command.account_id,
            subscription_id=command.subscription_id,
            authority=authority,
        )

        existing = db.execute(
            select(BillingContractVersion).where(
                BillingContractVersion.contract_id == contract.id,
                BillingContractVersion.idempotency_key == context.idempotency_key,
            )
        ).scalar_one_or_none()
        if existing is not None:
            # Named apart from the list built for a fresh version below: this
            # one is already the finished tuple read back off the replay.
            replayed_line_ids = tuple(
                db.execute(
                    select(BillingContractLine.id).where(
                        BillingContractLine.contract_version_id == existing.id
                    )
                )
                .scalars()
                .all()
            )
            return ContractVersionResult(
                contract_id=contract.id,
                version_id=existing.id,
                version=existing.version,
                authority=existing.authority,
                line_ids=replayed_line_ids,
                replayed=True,
            )

        current = BillingContracts._current_effective(db, contract_id=contract.id)
        current_starts_at = (
            _aware_utc(current.starts_at) if current is not None else None
        )
        if current_starts_at is not None and command.starts_at <= current_starts_at:
            # Equal starts would close the previous version into a zero-length
            # interval, which its own check constraint forbids.
            raise _error(
                "out_of_order_contract_version",
                "A new version must start after the current effective one.",
                current_starts_at=current_starts_at.isoformat(),
                requested_starts_at=command.starts_at.isoformat(),
            )

        next_version = (
            db.execute(
                select(BillingContractVersion.version)
                .where(BillingContractVersion.contract_id == contract.id)
                .order_by(BillingContractVersion.version.desc())
                .limit(1)
            ).scalar_one_or_none()
            or 0
        ) + 1

        if current is not None:
            # Close the previous version *before* inserting the new one. Both
            # rows would otherwise satisfy the "one open-ended effective
            # version per contract" partial unique index at the same instant.
            # The intervals stay contiguous and half-open: no gap, no overlap.
            current.ends_at = command.starts_at
            current.status = BillingContractVersionStatus.superseded
            current.superseded_at = command.starts_at
            db.flush()

        cadence = command.cadence
        version = BillingContractVersion(
            contract_id=contract.id,
            version=next_version,
            status=BillingContractVersionStatus.effective,
            authority=authority,
            account_id=command.account_id,
            subscription_id=command.subscription_id,
            source_kind=command.source_kind,
            source_id=command.source_id,
            source_version=command.source_version,
            starts_at=command.starts_at,
            ends_at=command.ends_at,
            contracted_price=command.contracted_price,
            currency=command.currency,
            rate_basis=cadence.rate_basis,
            rate_unit=cadence.rate_unit,
            rate_quantity=cadence.rate_quantity,
            service_interval_unit=cadence.service_interval_unit,
            service_interval_count=cadence.service_interval_count,
            invoice_interval_unit=cadence.invoice_interval_unit,
            invoice_interval_count=cadence.invoice_interval_count,
            collection_timing=cadence.collection_timing,
            alignment=cadence.alignment,
            anchor_day=cadence.anchor_day,
            end_of_month_rule=cadence.end_of_month_rule,
            timezone_name=cadence.timezone_name,
            proration_policy=cadence.proration_policy,
            payment_terms_days=command.payment_terms_days,
            tax_treatment_code=command.tax_treatment_code,
            tax_inclusive=command.tax_inclusive,
            discount_code=command.discount_code,
            discount_amount=command.discount_amount,
            supersedes_id=current.id if current is not None else None,
            actor=context.actor,
            reason=context.reason,
            command_id=context.command_id,
            correlation_id=context.correlation_id,
            idempotency_key=context.idempotency_key,
        )
        db.add(version)
        db.flush()

        line_ids: list[UUID] = []
        for line in command.lines:
            record = BillingContractLine(
                contract_version_id=version.id,
                contract_line_key=line.contract_line_key
                or BillingContracts._inherited_line_key(
                    db,
                    contract_id=contract.id,
                    charge_component=line.charge_component,
                    component_key=line.component_key,
                ),
                charge_component=line.charge_component,
                component_key=line.component_key,
                description=line.description,
                quantity=line.quantity,
                unit_price=line.unit_price,
                currency=line.currency,
                accounting_treatment=line.accounting_treatment,
                is_finite=line.is_finite,
                tax_treatment_code=line.tax_treatment_code,
            )
            db.add(record)
            db.flush()
            line_ids.append(record.id)

        return ContractVersionResult(
            contract_id=contract.id,
            version_id=version.id,
            version=version.version,
            authority=authority,
            line_ids=tuple(line_ids),
            replayed=False,
        )

    @staticmethod
    def _ensure_contract(
        db: Session,
        *,
        account_id: UUID,
        subscription_id: UUID,
        authority: BillingRecordAuthority,
    ) -> BillingContract:
        contract = db.execute(
            select(BillingContract)
            .where(BillingContract.subscription_id == subscription_id)
            .with_for_update()
        ).scalar_one_or_none()
        if contract is not None:
            if contract.account_id != account_id:
                raise _error(
                    "contract_account_mismatch",
                    "Subscription already has a contract under another account.",
                    contract_account_id=str(contract.account_id),
                    requested_account_id=str(account_id),
                )
            return contract

        contract = BillingContract(
            account_id=account_id,
            subscription_id=subscription_id,
            authority=authority,
        )
        db.add(contract)
        db.flush()
        # Re-take the row lock now that it exists, so a concurrent command
        # serialises behind this one rather than racing the version insert.
        lock_for_update(db, BillingContract, contract.id)
        return contract

    @staticmethod
    def _inherited_line_key(
        db: Session,
        *,
        contract_id: UUID,
        charge_component: ChargeComponent,
        component_key: str,
    ) -> UUID:
        """Reuse the lineage key so obligations survive a supersession."""

        existing = db.execute(
            select(BillingContractLine.contract_line_key)
            .join(
                BillingContractVersion,
                BillingContractLine.contract_version_id == BillingContractVersion.id,
            )
            .where(
                BillingContractVersion.contract_id == contract_id,
                BillingContractLine.charge_component == charge_component,
                BillingContractLine.component_key == component_key,
            )
            .order_by(BillingContractVersion.version.desc())
            .limit(1)
        ).scalar_one_or_none()
        return existing if existing is not None else uuid4()

    @staticmethod
    def _current_effective(
        db: Session, *, contract_id: UUID
    ) -> BillingContractVersion | None:
        return db.execute(
            select(BillingContractVersion)
            .where(
                BillingContractVersion.contract_id == contract_id,
                BillingContractVersion.status == BillingContractVersionStatus.effective,
                BillingContractVersion.ends_at.is_(None),
            )
            .with_for_update()
        ).scalar_one_or_none()

    @staticmethod
    def effective_version_at(
        db: Session, *, subscription_id: UUID, moment: datetime
    ) -> BillingContractVersion | None:
        """Return the one version effective for ``subscription_id`` at ``moment``.

        Read-only resolver. ADR 0007 invariant 1: at most one version is
        effective for a contract line at an instant, so this returns at most
        one row.
        """

        if moment.tzinfo is None:
            raise _error(
                "invalid_contract_terms",
                "Effective-version lookup requires a timezone-aware instant.",
            )
        return db.execute(
            select(BillingContractVersion)
            .join(
                BillingContract,
                BillingContractVersion.contract_id == BillingContract.id,
            )
            .where(
                BillingContract.subscription_id == subscription_id,
                BillingContractVersion.status.in_(
                    (
                        BillingContractVersionStatus.effective,
                        BillingContractVersionStatus.superseded,
                    )
                ),
                BillingContractVersion.starts_at <= moment,
                (BillingContractVersion.ends_at.is_(None))
                | (BillingContractVersion.ends_at > moment),
            )
            .order_by(BillingContractVersion.version.desc())
            .limit(1)
        ).scalar_one_or_none()

    @staticmethod
    def cadence_of(version: BillingContractVersion) -> BillingCadence:
        """Rebuild the typed cadence value object from a stored version."""

        return BillingCadence(
            rate_basis=RateBasis(version.rate_basis),
            rate_unit=IntervalUnit(version.rate_unit),
            rate_quantity=version.rate_quantity,
            service_interval_unit=IntervalUnit(version.service_interval_unit),
            service_interval_count=version.service_interval_count,
            invoice_interval_unit=IntervalUnit(version.invoice_interval_unit),
            invoice_interval_count=version.invoice_interval_count,
            collection_timing=CollectionTiming(version.collection_timing),
            alignment=CadenceAlignment(version.alignment),
            timezone_name=version.timezone_name,
            end_of_month_rule=EndOfMonthRule(version.end_of_month_rule),
            proration_policy=ProrationPolicy(version.proration_policy),
            anchor_day=version.anchor_day,
        )

    @staticmethod
    def cancel_version(
        db: Session,
        *,
        version_id: UUID,
        context: CommandContext,
    ) -> UUID:
        """Cancel a version the owner issued in error.

        A correction originates only from the owner of the wrong fact
        (ADR 0007 invariant 20); history is closed, never edited away.
        """

        return execute_owner_command(
            db,
            definition=_SUPERSEDE_COMMAND,
            context=context,
            operation=lambda: BillingContracts._cancel_version(
                db, version_id=version_id, context=context
            ),
        )

    @staticmethod
    def _cancel_version(
        db: Session, *, version_id: UUID, context: CommandContext
    ) -> UUID:
        version = lock_for_update(db, BillingContractVersion, version_id)
        if version is None:
            raise _error(
                "contract_version_not_found",
                "Contract version does not exist.",
                version_id=str(version_id),
            )
        if version.status is BillingContractVersionStatus.canceled:
            return version.id
        version.status = BillingContractVersionStatus.canceled
        version.superseded_at = version.superseded_at or version.starts_at
        version.reason = context.reason
        db.flush()
        return version.id
